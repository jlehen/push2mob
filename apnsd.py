#!/usr/bin/env python
# vim: ts=4:sw=4:et
#
# Copyright (C) 2012 Jeremie Le Hen <jeremie@le-hen.org>
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import ConfigParser
import Queue
import base64
import datetime
import getopt
import json
import logging
import math
import os
import pycurl
import random
import re
import socket
import sqlite3
import select
import ssl
import struct
import sys
import threading
import time
import zmq

DEVTOKLEN = 32
CONFIGFILE = 'apnsd.conf'

def usage():
    print """Usage: apnsd.py [options]
Options:
  -c    Change configuration file (defaults to "./apnsd.conf")
  -h    Show this help message"""

def now():

        return time.time()

def hexdump(buf, chunklen = 16):
        l = chunklen
        while len(buf) > 0:
            b = buf[:l]
            buf = buf[l:]
            s = 3 * l - 1
            #b = b.ljust(l, '\000')
            fmt = "%-" + str(s) + "s%s%s"
            print fmt % (' '.join("%02x" % ord(c) for c in b),
                ' ', ''.join(['.', c][c.isalnum()] for c in b))


class Locker:
    def __init__(self, lock):
        self.l = lock
    def __enter__(self):
        self.l.acquire()
        return self.l
    def __exit__(self, type, value, traceback):
        self.l.release()


class OrderedPersistentQueue:
    """
    This is an ordered persistent queue (!).  Every object stored in this
    queue is associated with an ordering provided as a real number .  Objects
    are returned in order and are removed from database upon acknowledgement.
    Upon startup, all returned but unacknowledged are reset.  It completely
    relies on SQLite.
    """

    _NEGLIGIBLEWAIT = 0.2

    def __init__(self, sqlite, tablename):
        self.cv = threading.Condition()
        self.table = tablename
        self.sqlcon = sqlite3.connect(sqlite, check_same_thread = False)
        self.sqlcon.isolation_level = None
        self.sqlcur = self.sqlcon.cursor()
        cur = self.sqlcur
        cur.execute(
            """CREATE TABLE IF NOT EXISTS %s (
            inuse INTEGER NOT NULL DEFAULT 0,
            ordering REAL NOT NULL,
            data BLOB);""" % tablename)
        cur.execute(
            """CREATE INDEX IF NOT EXISTS ordering ON %s (
            inuse, ordering)""" % tablename)
        cur.execute(
            """UPDATE %s SET inuse = 0""" % tablename)

    def _put(self, ordering, item):
        """
        Actually record in the database.
        This must be called with self.cv locked.
        """

        self.sqlcur.execute(
            """INSERT INTO %s (ordering, data)
            VALUES(?, ?)""" % self.table, (ordering, str(item)))

    def _pick(self):
        """
        Lookup next item in the database.  Returns a tuple containing
        (rowid, ordering, item)
        This must be called with self.cv locked.
        """

        while True:
            self.sqlcur.execute(
                """SELECT rowid, ordering, data from %s
                WHERE inuse = 0
                ORDER BY ordering LIMIT 1;""" % self.table)
            r = self.sqlcur.fetchone()
            if r is None:
                self.cv.wait()
                continue
            return (r[0], r[1], eval(r[2]))

    def _grab(self, r):
        """
        Actually record the item as being in use in the database.
        The argument is the tuple returned by _pick().
        This must be called with self.cv locked.
        """

        self.sqlcur.execute(
            """UPDATE %s SET inuse = 1
            WHERE rowid = ?;""" % self.table, (r[0], ))

    def _ack(self, r):
        """
        Actually delete the item from the database.
        The argument is the tuple returned by _pick().
        This must be called with self.cv locked.
        """

        self.sqlcur.execute(
            """DELETE FROM %s WHERE rowid = ?;""" % self.table,
            (r[0], ))

    def _reorder(self, r, ordering):
        """
        Reorder an item and mark it as unused.
        The first argument is the tuple returned by _pick().
        This must be called with self.cv locked.
        """

        self.sqlcur.execute(
            """UPDATE %s SET ordering = ?, inuse = 0
            WHERE rowid = ?""" % self.table, (ordering, r[0]))

    def _qsize(self):
        """
        Actually reckon the number of elements in the queue.
        """

        self.sqlcur.execute("SELECT COUNT(*) FROM %s" % self.table)
        r = self.sqlcur.fetchone()
        return r[0]

    def put(self, ordering, item):
        """
        Put the item in the queue with the given ordering.
        """

        with Locker(self.cv):
            self._put(ordering, item)
            self.cv.notify()

    def get(self):
        """
        Return the next item in an ordered fashion.
        If nothing is available right now, it waits.
        The result is a tuple with (rowid, ordering, item).
        """

        with Locker(self.cv):
            r = self._pick()
            self._grab(r)
            return r

    def ack(self, t):
        """
        Delete item from the queue.  The argument is the tuple returned
        by the get() method.
        """

        with Locker(self.cv):
            self._ack(t)

    def reorder(self, t, ordering):
        """
        Reorder item in the queue with the given ordering.  The first argument
        is the tuple returned by the get() method.
        """

        with Locker(self.cv):
            self._reorder(t, ordering)

    def qsize(self):
        """
        Return the number of elements in the queue.
        """

        return self._qsize()



class ChronologicalPersistentQueue(OrderedPersistentQueue):
    """
    This is a chronological persistent queue (!).  Every object stored in
    this queue is associated with a timestamp from the Epoch.  Objects
    are returned chronologically in a timely fashion and are removed from
    database upon acknowledgement.  Upon started, all returned but
    unacknowledged are reset.
    """

    def __init__(self, sqlite, tablename):
        OrderedPersistentQueue.__init__(self, sqlite, tablename)

    def put(self, when, item):
        """
        Put the item in the queue with the given timestamp.
        """

        with Locker(self.cv):
            self._put(when, item)
            timedelta = when - now()
            if timedelta < self.__class__._NEGLIGIBLEWAIT:
                self.cv.notify()

    def put_now(self, item):
        """
        Put the item in the queue with the timestamp set to current time.
        """

        self.put(now(), item)

    def get(self):
        """
        Return the next item chronologically in a timely fashion.
        If nothing is available right now, it waits.
        The result is a tuple with (rowid, timestamp, item).
        """

        with Locker(self.cv):
            r = self._pick()
            while True:
                timedelta = r[1] - now()
                if timedelta >= self.__class__._NEGLIGIBLEWAIT:
                    self.cv.wait(timedelta)
                    continue
                break
            self._grab(r)
            return r


class PersistentQueue(Queue.Queue):
    """
    This class has the same interface as threading.Queue except that
    it also stores persistently the data it holds into an SQLite database.
    """

    def __init__(self, sqlite, tablename):
        Queue.Queue.__init__(self)

        self.table = tablename
        self.sqlcon = sqlite3.connect(sqlite, check_same_thread = False)
        self.sqlcon.isolation_level = None
        self.sqlcur = self.sqlcon.cursor()
        cur = self.sqlcur
        cur.execute('CREATE TABLE IF NOT EXISTS %s(data BLOB);' % tablename)
        cur.execute('SELECT rowid, data FROM %s ORDER BY rowid;' % tablename)
        while True:
            row = cur.fetchone()
            if row is None:
                break
            self.queue.append((row[0], eval(row[1])))

    def _put(self, item):
        self.sqlcur.execute('INSERT INTO %s(data) VALUES(?)' %
            self.table, (str(item),))
        rowid = self.sqlcur.lastrowid
        self.queue.append((rowid, item))

    def _get(self):
        r = self.queue.popleft()
        self.sqlcur.execute('DELETE FROM %s WHERE rowid=?' % self.table,
            (r[0],))
        return r[1]


class DeviceTokenFormater:

    def __init__(self, format):
        assert format == "base64" or format == "hex"
        self._format = format

    def __call__(self, devtok):
        if self._format == "base64":
           return base64.standard_b64encode(devtok)
        else:
            return ''.join("%02x" % ord(c) for c in devtok)

# This will be used as a function.
devtokfmt = None


class TLSConnectionMaker:
    """
    This is a socket.SSLSocket factory with pre-configured CA, cert
    and key files.
    """

    # This has to be called before any object is created.
    def __init__(self, cacerts, cert, key):
        """
        Configures the CA, certificate and key files to be used when
        creating a connection.
        """
        self.cacerts = cacerts
        self.cert = cert
        self.key = key

    def __call__(self, peer, sleeptime, errorstring):
        """
        Creates an SSL socket to the given `peer', which is a tuple
        (host, port).
        """
        while True:
            try:
                ai = socket.getaddrinfo(peer[0], peer[1], 0, 0,
                    socket.IPPROTO_TCP)
                s = socket.socket(ai[0][0], ai[0][1], ai[0][2])
                sslsock = ssl.wrap_socket(s, keyfile=self.key,
                    certfile=self.cert, server_side=False,
                    cert_reqs=ssl.CERT_REQUIRED, ca_certs=self.cacerts)
                sslsock.connect(ai[0][4])
                break
            except socket.gaierror as e:
                st = 1
            except ssl.SSLError as e:
                # If there is an SSL error, it may be an authentication
                # error and retrying too often may lead us to be
                # banned.
                st = sleeptime
            except Exception as e:
                # Will be at least 1.
                st = (sleeptime + 9) / 10
            logging.error(errorstring % (peer[0], peer[1], e))
            if sleeptime == 0:
                sslsock = None
                break
            time.sleep(st)

        return sslsock


class HTTPResponseReceiver:
    """
    Parses HTTP responses (header + body) as returned by pycurl.
    """

    NEWHEADER = 1
    INHEADER = 2
    WAITINGNEXTBLOCK = 3
    INBODY = 4

    def __init__(self):
        self.state = HTTPResponseReceiver.NEWHEADER
        self.http_re = re.compile("^HTTP/1\.\d ")
        self.lastheader = None
        self.headers = {}
        self.body = []

    def _do_status(self, line):
        if not self.http_re.search(line):
            return False
        a = line.split()
        try:
            status = int(a[1])
        except TypeError as e:
            return False
        if status >= 300 and status < 399:
            self.state = HTTPResponseReceiver.WAITINGNEXTBLOCK
        else:
            self.state = HTTPResponseReceiver.INHEADER
            self.status = status
        return True

    def _do_header(self, line):
        if line[0] == " " or line[0] == "\t":
            if self.lastheader is None:
                return False
            line = line.strip(" \t")
            self.headers[self.lastheaders] = \
              self.headers[self.lastheaders] + " " + line
            return True

        (name, value) = line.split(":", 1)
        value = value.strip(" \t")
        if name in self.headers:
            self.headers[name] = self.headers[name] + ", " + value
        else:
            self.headers[name] = value
        self.lastheader = name
        return True

    def write(self, line):
        line = line.rstrip("\r\n")
        if self.state == HTTPResponseReceiver.NEWHEADER:
            if self._do_status(line):
                return
            print "ERROR: HTTP status line expected: %s" % line
            sys.exit(1)
        if self.state == HTTPResponseReceiver.WAITINGNEXTBLOCK:
            if len(line) == 0:
                self.state = HTTPResponseReceiver.NEWHEADER
                return
            return
        if self.state == HTTPResponseReceiver.INHEADER:
            if len(line) == 0:
                self.state = HTTPResponseReceiver.INBODY
                return
            if self._do_header(line):
                return
            print "ERROR: Unexpected HTTP header format"
            sys.exist(1)
        self.body.append(line)

    def getStatus(self):
        """
        Return status of the HTTP request.
        """
        return self.status

    def getHeaders(self):
        """
        Return a dictionnary whose keys are HTTP header names
        and values their corresponding values.
        """
        return self.headers

    def getBody(self):
        """
        Return an array of lines forming the body.
        """
        return self.body


class Listener(threading.Thread):
    """
    This is the base class for the ZMQ listening socket and
    it must not be instanciated as is.  It receives commands
    from the client and either enqueues notifications or sends
    feedback upon demand.
    There ought to be only one instance of this class for each
    application certificate, though you can make multiple
    listener connected to a single push/feedback queue pair.
    """

    _WHTSP = re.compile("\s+")
    _PLUS = re.compile(r"^\+")

    def __init__(self, zmqsock):
        threading.Thread.__init__(self)
        self.daemon = True
        self.zmqsock = zmqsock

    def _send_error(self, msg, detail = None):
        """
        Returns an error message to the ZMQ peer.
        If `detail' is provided, is will be shown in the daemon log.
        """
        if detail is not None:
            fmt = msg + ": %s"
            logging.warning(fmt, detail)
        else:
            logging.warning(msg)
        self.zmqsock.send("ERROR " + msg)

    def _send_ok(self, res):
        if len(res) == 0:
            self.zmqsock.send("OK")
        else:
            self.zmqsock.send("OK %s" % res)

    @staticmethod
    def _parse_expiry(expiry):
        """
        Parse expiry handling absolute format (seconds Epoch) or
        relative format (starting with "+").
        """
        expiry, nsub = re.subn(Listener._PLUS, "", expiry, 1)
        expiry = int(expiry)
        if nsub == 1:
            expiry = now() + expiry
        return expiry

    def _parse_send_args(self, nargs, msg):
        """
        Parse the send command with a variable number of mandatory
        arguments, using the following grammar:
        send <arg1> ... <argn> <ndevices> <dev1> ... <devn> <payload ...>
        Returns tree a tuple with: (arglist, devlist, payload).  If None
        is returned, then an error message has already been issued.
        You probably want to override this method to add sanity checks.
        """
        try:
            cmdargs = msg[5:]
            arglist = re.split(APNSListener._WHTSP, cmdargs, nargs)
            # Don't use pop() so we raise an IndexError exception.
            cmdargs = arglist[nargs]
            del arglist[nargs]
            ntok, cmdargs = re.split(APNSListener._WHTSP, cmdargs, 1)
            ntok = int(ntok)
            devlist = re.split(APNSListener._WHTSP, cmdargs, ntok)
            # Same here, but usually the payload contains whitespaces so
            # the split will work but we will have a device token format
            # error later.
            payload = devlist[ntok]
            del devlist[ntok]
        except IndexError as e:
            raise IndexError("wrong number of arguments")
        return (arglist, devlist, payload)

    def _parse_send(self, msg):
        """
        Parse the send command.  You must overload this method.
        """

    def _perform_send(self):
        """
        Self-explanatory.  You must overload this method.
        """
        pass

    def _perform_feedback(self):
        """
        Self-explanatory.  You must overload this method.
        """
        pass

    def run(self):
        while True:
            msg = self.zmqsock.recv()
            #
            # Parse line.
            msg = msg.strip()
            if msg[0:5].lower().find("send ") == 0:
                try:
                    res = self._parse_send(msg)
                except Exception as e:
                    self._send_error("Invalid input (%s)" % e , msg)
                    continue
                # An error message has already been issued.
                if res is None:
                    continue

                res = self._perform_send(*res)
                self._send_ok(res)
                continue

            elif msg.lower().find("feedback") == 0:
                res = self._perform_feedback()
                self._send_ok(res)
                continue

            self._send_error("Invalid input", msg)


#############################################################################
# APNS stuff.
#############################################################################

class APNSRecentNotifications:
    """
    Each instance of this class goes with one APNSAgent instance.
    It records notifications that have been recently sent by this
    agent.  Notifications are only kept for a limited amount of time
    as this record is only used for synchronous inline errors
    returned by the APNS.
    We do not need any locking as each object is accessed by only
    one thread (APNSAgent).
    """

    def __init__(self, maxerrorwait):
        if maxerrorwait != 0:
            # For an error wait of 0.1 seconds, this
            # will rotate the dicts every minute.
            self.rotatetime = 600 * maxerrorwait
        else:
            self.rotatetime = 10
        self.tstamp = now()
        self.n = [{}, {}]
        self.i = 0

    def _rotate(self):
        # This part should be protected by a mutex if the object was
        # accessed by multiple threads.
        if now() - self.tstamp >= self.rotatetime:
            i = (self.i + 1) % 2
            self.n[i] = {}
            self.i = i
            self.tstamp = now()
        

    def record(self, ident, notification):
        self._rotate()
        self.n[self.i][ident] = notification

    def lookup(self, ident):
        self._rotate()
        i0 = self.i
        i1 = (i + 1) % 2
        n = self.n[i0].get(ident)
        if n is None:
            n = self.n[i1].get(ident)
        return n


class APNSAgent(threading.Thread):
    """
    Each instance gets notifications from the APNS push queue
    fed by the APNSListener instance and sends it to APNS.
    It additionally handles almost-synchronous inline errors
    and consequently generates special feedback entries for them.
    """

    _EXTENDEDNOTIFICATION = 1
    _MAXTRIAL = 2
    _INVALIDTOKENSTATUS = 8
    # Time between each connection retry if SSL auth error.
    _RETRYTIME = 60

    _error_responses = {
        0: "No error encourtered",
        1: "Processing error",
        2: "Missing device token",
        3: "Missing topic",
        4: "Missing payload",
        5: "Invalid token size",
        6: "Invalid topic size",
        7: "Invalid payload size",
        8: "Invalid token",
        255: "None (unknown)"
    }

    def __init__(self, queue, gateway, maxnotiflag, maxerrorwait, feedbackq,
        tlsconnect):

        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gateway = gateway
        self.maxnotiflag = maxnotiflag
        self.maxerrorwait = maxerrorwait
        self.feedbackq = feedbackq
        self.tlsconnect = tlsconnect
        # Tuple: (id, bintok)
        self.recentnotifications = APNSRecentNotifications(maxerrorwait)
        self.sock = None

    def _connect(self):
        self.sock = self.tlsconnect(self.gateway, APNSAgent._RETRYTIME,
            "Couldn't connect to APNS (%s:%d): %s")
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def _close(self):
        # XXX It seems the socket has already been shut down by the
        # other side but I cannot use SHUT_WR.  This is weird because
        # the connection is supposed to be half-closed in this case
        # so I should be able to use SHUT_WR to do the "passive close".
        #self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.sock = None

    def _reallyprocesserror(self):
        try:
            buf = self.sock.recv()
        except socket.error as e:
            logging.debug("Connection has been shut down abruptly: %s" % e)
            return False
        if len(buf) == 0:
            logging.debug("APNS closed the connection")
            return False

        fmt = '>BBI'
        if len(buf) != struct.calcsize(fmt):
            logging.warning("Unexpected APNS error response size: %d (!= %d)" %
                (len(buf), struct.calcsize(fmt)))
            return True
        # Bad...
        cmd, st, errident = struct.unpack(fmt, buf)
        errdevtok = self.recentnotifications.lookup(errident)
        if errdevtok is None:
            errdevtok = "unknown"
        else:
            errdevtok = devtokfmt(errdevtok)
        logging.warning("Notification #%d to %s response: %s" %
            (errident, errdevtok, APNSAgent._error_responses[st]))
        if st == APNSAgent._INVALIDTOKENSTATUS:
            self.feedbackq.put((0, errdevtok))
        return True

    def _processerror(self):
        """
        Returns True if we received an error response, False if the
        connection has just been closed remotely.
        """

        r = self._reallyprocesserror()
        self._close()
        return r

    def run(self):
        while True:
            if self.sock is None:
                timeout = None
            else:
                timeout = 1
            try:
                ident, creation, expiry, devtok, payload = \
                    self.queue.get(True, timeout)
            except Queue.Empty as e:
                triple = select.select([self.sock], [], [], 0)
                if len(triple[0]) != 0:
                    self._processerror()
                    timeout = None
                else:
                    # Try to be generous with APNS and give it enough
                    # time to return is error.
                    timeout = timeout * 2
                    if timeout > 10:
                        timeout = None
                continue
                    
            bintok = base64.standard_b64decode(devtok)

            # Check notification lag.
            lag = now() - creation
            if lag > self.maxnotiflag:
                logging.info("Discarding notification #%d to %s: " \
                    "delayed by %us (max %us)" %
                    (ident, devtokfmt(bintok), lag, self.maxnotiflag))
                continue

            # Build the binary message.
            logging.debug("Sending notification #%d to %s, " \
                "lagging by %us: %s" %
                (ident, devtokfmt(bintok), lag, payload))
            fmt = '> B II' + 'H' + str(len(bintok)) + 's' + \
                'H' + str(len(payload)) + 's'
            binmsg = struct.pack(fmt, APNSAgent._EXTENDEDNOTIFICATION, ident,
                expiry, len(bintok), bintok, len(payload), payload)
            hexdump(binmsg)
            
            # Now send it.
            if self.sock is None:
                self._connect()

            trial = 0
            while trial < APNSAgent._MAXTRIAL:
                try:
                    self.sock.sendall(binmsg)
                    break
                except socket.error as e:
                    self._processerror()
                    trial = trial + 1
                    logging.debug("Retry (%d) to send notification "
                        "#%d to %s: %s" %
                        (trial, ident, devtokfmt(bintok), e))
                    self._connect()
                    continue
            if trial == APNSAgent._MAXTRIAL:
                logging.warning("Cannot send notification #%d to %s, "
                    "abording" % (ident, devtokfmt(bintok)))
                continue
            self.recentnotifications.record(ident, bintok)
            logging.info("Notification #%d sent", ident)

            if self.maxerrorwait != 0:
                # Receive a possible error in the preceeding message.
                triple = select.select([self.sock], [], [], self.maxerrorwait)
                if len(triple[0]) != 0:
                    self._processerror()


class APNSFeedbackAgent(threading.Thread):
    """
    There ought to be only one instance of this class, at least
    for each application certificate.
    It periodically check the APNS feedback service and 
    creates feedback entries for it.
    """

    def __init__(self, queue, sock, gateway, frequency, tlsconnect):
        threading.Thread.__init__(self)
        self.daemon = True
        self.sock = sock
        self.queue = queue
        self.gateway = gateway
        self.frequency = frequency
        self.tlsconnect = tlsconnect
        self.fmt = '> IH ' + str(DEVTOKLEN) + 's'
        self.tuplesize = struct.calcsize(self.fmt)

    def _close(self):
        self.sock.shutdown(socket.SHUT_RD)
        self.sock.close()
        self.sock = None

    def run(self):
        while True:
            # self.sock is not None on the first run because we
            # inherits the socket from the main thread (which has
            # been used for testing purpose).
            if self.sock is None:
                time.sleep(self.frequency)
                self.sock = self.tlsconnect(self.gateway, self.frequency,
                    "Couldn't connect to feedback service (%s:%d): %s")

            buf = ""
            while True:
                b = self.sock.recv()
                if len(b) == 0:
                    if len(buf) != 0:
                        logging.warning("Unexpected trailing garbage from " \
                            "feedback service (%d bytes remaining)" % len(buf))
                        hexdump(buf)
                    break

                buf = buf + b
                while True:
                    try:
                        bintuple = buf[0:self.tuplesize]
                    except IndexError as e:
                        break
                    if len(bintuple) < self.tuplesize:
                        break
                    buf = buf[self.tuplesize:]
                    ts, toklen, bintok = struct.unpack(self.fmt, bintuple)
                    devtok = devtokfmt(bintok)
                    ts = str(ts)
                    logging.info("New feedback tuple (%s, %s)" % (ts, devtok))
                    self.queue.put((ts, devtok))

            self._close()


class APNSListener(Listener):
    """
    See the Listener class description.
    This class additionally maintains a persistent message ID
    in an SQLite table because it is required in the APNS
    enhanced notification format.
    """

    _PAYLOADMAXLEN = 256

    def __init__(self, zmqsock, sqlitedb, pushq, feedbackq):
        Listener.__init__(self, zmqsock)
        self.sqlitedb = sqlitedb
        self.pushq = pushq
        self.feedbackq = feedbackq

    def _parse_send(self, msg):
        arglist, devtoks, payload = Listener._parse_send_args(self, 1, msg)

        # Check expiry.
        try:
            expiry = Listener._parse_expiry(arglist[0])
        except Exception as e:
            self._send_error("Invalid expiry value: %s" % arglist[0])
            return None
        arglist = [expiry]

        # Check device token format.
        goodtoks = []
        for dt in devtoks:
            devtok = ''
            if len(dt) == DEVTOKLEN * 2:
                # Hexadecimal device token.
                for i in range(0, DEVTOKLEN * 2, 2):
                    c = dt[i:i+2]
                    devtok = devtok + struct.pack('B', int(c, 16))
            else:
                # Maybe base64?
                try:
                    devtok = base64.standard_b64decode(dt)
                except TypeError:
                    self._send_error("Wrong base64 encoding for device " \
                        "token: %s" % dt)
                    return None
            if len(devtok) != DEVTOKLEN:
                self._send_error("Wrong device token length " \
                    "(%d != %s): %s" % (len(devtok), DEVTOKLEN, dt))
                return None
            # Store the token in base64 in the queue, text is better
            # to debug.
            logging.debug("Got notification for device token %s, " \
                "expiring at %d" % (base64.standard_b64encode(devtok),
                expiry))
            goodtoks.append(base64.standard_b64encode(devtok))
        devtoks = goodtoks

        # Check payload length.
        if len(payload) > APNSListener._PAYLOADMAXLEN:
            self._send_error("Payload too long (%d > %d)" % len(payload),
                APNSListener._PAYLOADMAXLEN, payload)
            return None

        # Mimic _parse_send_args() return value.
        return (arglist, devtoks, payload)

    def _perform_send(self, arglist, devtoks, payload):
        expiry = arglist[0]
        self.sqlcur.execute('UPDATE ident SET cur=?',
            (self.curid + len(devtoks), ))

        idlist = []
        for devtok in devtoks:
            self.pushq.put((self.curid, now(), expiry, devtok, payload))
            idlist.append(str(self.curid))
            self.curid = self.curid + 1
        return ' '.join(idlist)

    def _perform_feedback(self):
        feedbacks = []
        try:
            while True:
                timestamp, devtok = self.feedbackq.get_nowait()
                feedbacks.append("%s:%s" % (timestamp, devtok))
        except Queue.Empty:
                pass
        return ' '.join(feedbacks)

    def run(self):
        # Get current notification identifier.
        sqlcon = sqlite3.connect(self.sqlitedb)
        sqlcon.isolation_level = None
        sqlcur = sqlcon.cursor()
        sqlcur.execute('CREATE TABLE IF NOT EXISTS ' \
            'ident(cur INTEGER PRIMARY KEY);')
        sqlcur.execute('SELECT cur FROM ident;')
        row = sqlcur.fetchone()
        if row is None:
            sqlcur.execute('INSERT INTO ident VALUES(0);')
            row = (0,)
        self.curid = row[0]
        logging.info("Notifications current identifier is %d" % self.curid)

        self.sqlcur = sqlcur
        Listener.run(self)



#############################################################################
# GCM stuff.
#############################################################################

class GCMRegisterationIDSChanges:
    """
    This object records all recent changes to registeration IDs as reported
    by CGM.  A registeration ID can be replaced by a new one, not
    registered any more or invalid.
    Each time the queryAll() method is called, the current record is marked
    for deletion in "flushafter" seconds, this gives the opportunity to the
    user application to update its data.
    """

    _FLUSHAFTER = 10

    REPLACED = 1
    NOTREGISTERED = 2
    INVALID = 3

    def __init__(self, sqlite, tablename):
        self.mutex = threading.Lock()
        self.tstamp = 0
        self.flushafter = GCMRegisterationIDSChanges._FLUSHAFTER

        self.table = tablename
        self.sqlcon = sqlite3.connect(sqlite, check_same_thread = False)
        self.sqlcon.isolation_level = None
        self.sqlcur = self.sqlcon.cursor()
        self.sqlcur.execute(
            """CREATE TABLE IF NOT EXISTS %s (
            regid VARCHAR(256) PRIMARY KEY NOT NULL,
            state INTEGER NOT NULL DEFAULT 0,
            newregid VARCHAR(256),
            retrievetime REAL NOT NULL DEFAULT 0)""" % tablename)
        self.sqlcur.execute(
            """CREATE INDEX IF NOT EXISTS regid_retrtime ON %s (
            regid, retrievetime)""" % tablename)
        self.sqlcur.execute(
            """DELETE FROM %s WHERE retrievetime > 0""" % self.table)

    def _update(self, regid, state, newregid):

        with Locker(self.mutex):
            self.sqlcur.execute(
                """INSERT OR REPLACE INTO %s
                (regid, state, newregid)
                VALUES (?, ?, ?)""" % self.table,
                (regid, state, newregid))

    def replace(self, oregid, nregid):
        """
        A new registeration ID (nregid) replaced the old one (oregid).
        """

        self._update(oregid, GCMRegisterationIDSChanges.REPLACED, nregid)

    def unregister(self, regid):
        """
        The registeration ID has been unregistered (application removed
        from device).
        """

        self._update(oregid, GCMRegisterationIDSChanges.NOTREGISTERED, "")

    def invalidate(self, regid):
        """
        The registeration ID has been pointed as invalid by the server.
        """

        self._update(oregid, GCMRegisterationIDSChanges.INVALID, "")

    def query(self, regid):
        """
        Return status of the requested registeration ID as a tuple
        (state, newregid).
        """

        with Locker(self.mutex):
            if self.tstamp != 0 and \
              now() - self.tstamp >= self.flushafter:
                self.sqlcur.execute(
                    """DELETE FROM %s
                    WHERE retrievetime > 0
                    AND retrievetime < ?""" % self.table,
                    (self.tstamp, ))
                self.tstamp = 0

            self.sqlcur.execute(
                """SELECT state, newregid FROM %s
                WHERE regid = ? AND retrievetime = 0""" % self.table,
                (regid, ))
            r = self.sqlcur.fetchone()
            return r

    def queryAll(self):
        """
        Return the whole content of the database as a list of tuples:
        (regid, state, newregid).
        Well, this is not truly tuples, they are row object from sqlite3
        module, but their behaviour mimics tuples.
        """

        with Locker(self.mutex):
            self.tstamp = now()
            self.sqlcur.execute(
                """UPDATE %s SET retrievetime WHERE retrievetime = 0""" %s,
                self.tstamp)
            self.sqlcur.execute(
                """SELECT regid, state, newregid FROM %s
                WHERE retrievetime = ?;""" % self.table,
                (self.tstamp, ))
            r = cur.fetchall()
            return r

    def count(self):
        """
        Return the number of element that are in the database.
        We don't care whether the element are going to be deleted here
        as this method is only called upon startup.
        """

        self.sqlcur.execute("SELECT count(*) FROM %s" % self.table)
        r = self.sqlcur.fetchone()
        return r[0]


class GCMHTTPRequest:

    def __init__(self, server_url, api_key):
        self.curl = pycurl.Curl()
        self.curl.setopt(pycurl.URL, server_url)
        self.curl.setopt(pycurl.HTTPHEADER,
            [ "Content-Type: application/json",
              "Authorization: key=%s" % api_key ])

        self.curl.setopt(pycurl.HEADER, 1)
        self.curl.setopt(pycurl.FOLLOWLOCATION, 1)
        self.curl.setopt(pycurl.MAXREDIRS, 5)
        self.curl.setopt(pycurl.POST, 1)

    def send(self, jsonmsg):
        self.resp = HTTPResponseReceiver()
        self.curl.setopt(pycurl.WRITEFUNCTION, self.resp.write)
        self.curl.setopt(pycurl.POSTFIELDS, jsonmsg)
        self.curl.perform()
        return self.resp


class GCMAgent(threading.Thread):

    def __init__(self, queue, server_url, api_key, maxnotiflag, feedbackdb):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gcmreq = GCMHTTPRequest(server_url, api_key)
        self.maxnotiflag = maxnotiflag
        self.feedbackdb = feedbackdb

    def run(self):
        qitem = None
        while True:
            if qitem is not None:
                self.queue.ack(qitem)

            qitem = self.queue.get()
            creation, collapsekey, expiry, delayidle, devtoks, payload = \
                qitem[2]

            # Check notification lag.
            lag = now() - creation
            # XXX Identify notification by an ID
            if lag > self.maxnotiflag:
                logging.info("GCM: Discarding notification %s: " \
                    "delayed by %us (max %us)" %
                    ("XXX", lag, self.maxnotiflag))
                continue

            # We store an absolute value but GCM wants a relative TTL.
            # Semantically this makes sense to adjust the TTL just
            # before handing the notification to the GCM service.
            ttl = int(round(expiry - now()))
            if ttl < 1:
                logging.info("GCM: Discarding notification %s: " \
                    "time-to-live exceeded by %us (ttl: %us)" %
                    ("XXX", -ttl, round(expiry - creation)))
                continue

            # Build the JSON request.
            req = {}
            req['registration_ids'] = devtoks
            req['collapse_key'] = collapsekey
            req['data'] = payload
            req['delay_while_idle'] = delayidle
            req['time_to_live'] = ttl
            #jsonmsg = json.dumps(msg, separators=(',',':'))
            jsonmsg = json.dumps(req, indent=4, separators=(', ',': '))

            print jsonmsg

class GCMListener(Listener):
    """
    See the Listener class description.
    This class additionally maintains a persistent message ID
    in an SQLite table because it is required in the APNS
    enhanced notification format.
    """

    _MAXNUMIDS = 1000
    _PAYLOADMAXLEN = 4096

    def __init__(self, zmqsock, pushq, idschanges):
        Listener.__init__(self, zmqsock)
        self.pushq = pushq
        self.idschanges = idschanges

    def _parse_send(self, msg):
        arglist, ids, payload = Listener._parse_send_args(self, 3, msg)

        # Collape key (arg #1).
        collapsekey = arglist[0]

        # Check expiry (arg #2).
        expiry = arglist[1]
        try:
            expiry = Listener._parse_expiry(expiry)
        except Exception as e:
            self._send_error("Invalid expiry value: %s" % expiry)
            return None

        # Check delayidle/nodelayidle (arg #3).
        delayidle = arglist[2]
        if delayidle == "delayidle":
            delayidle = True
        elif delayidle == "nodelayidle":
            delayidle = False
        else:
            self._send_error("Invalid (no)delayidle value: %s" % delayidle)
            return None

        arglist = [collapsekey, expiry, delayidle]

        # Check device token format.
        if len(ids) > GCMListener._MAXNUMIDS:
            self._send_error("Too many registeration ids (%d > %d)" % \
                (len(ids), GCMListener._MAXNUMIDS))

        goodids = []
        for i in ids:
            r = self.idschanges.query(i)
            if r is None:
                goodids.append(i)
                continue
            state, newi = r
            if state == GCMRegisterationIDSChanges.REPLACED:
                goodids.append(newi)
                continue
            elif state == GCMRegisterationIDSChanges.NOTREGISTERED:
                # Our client didn't get feedback yet, just discard the
                # message, it is not an error.
                continue
            elif state == GCMRegisterationIDSChanges.INVALID:
                self._send_error("Invalid registeration ID: %s" % r)
                return None

        ids = goodids

        # Check payload length.
        if len(payload) > GCMListener._PAYLOADMAXLEN:
            self._send_error("Payload too long (%d > %d)" % len(payload),
                GCMListener._PAYLOADMAXLEN, payload)
            return None

        # Mimic _parse_send_args() return value.
        return (arglist, ids, payload)

    def _perform_send(self, arglist, devtoks, payload):
        collapsekey = arglist[0]
        expiry = arglist[1]
        delayidle = arglist[2]

        createtime = now()
        self.pushq.put(createtime, (createtime, collapsekey, expiry, delayidle,
            devtoks, payload))
        return str(len(devtoks))

    def _perform_feedback(self):
        feedbacks = self.idschanges.queryAll()
        for i in range(0, len(feedbacks) - 1):
            t = feedbacks[i]
            if t[1] == GCMRegisterationIDSChanges.REPLACED:
                s = "replaced"
            elif t[1] == GCMRegisterationIDSChanges.NOTREGISTERED:
                s = "notregistered"
            elif t[1] == GCMRegisterationIDSChanges.INVALID:
                s = "invalid"
            s = "%s:%s:%s" % (t[0], s, t[2])
            feedbacks[i] = s
        return ' '.join(feedbacks)


#############################################################################
# Main.
#############################################################################

try:
    opts, args = getopt.getopt(sys.argv[1:], "c:h")
except getopt.GetoptError as e:
    if len(e.opt) == 0:
        logging.error("%s" % e.msg)
    else:
        logging.error("%s: %s" % (e.msg, e.opt))
    sys.exit(1)

for o, a in opts:
    if o == "-c":
        CONFIGFILE = a
    elif o == "-h":
        usage()
        sys.exit(0)
    else:
        assert False, "Unhandled option: %s" % o

#
# Get configuration.
#

cp = ConfigParser.SafeConfigParser()
l = cp.read([CONFIGFILE])
if len(l) == 0:
    raise Exception("Cannot open '%s'" % CONFIGFILE)

try:
    logfile = cp.get('main', 'daemon_log_file')
    apns_zmq_bind = cp.get('apns', 'zmq_bind')
    apns_sqlitedb = cp.get('apns', 'sqlite_db')
    apns_cacerts = cp.get('apns', 'cacerts_file')
    apns_cert = cp.get('apns', 'cert_file')
    apns_key = cp.get('apns', 'key_file')
    apns_devtok_format = cp.get('apns', 'device_token_format')
    apns_push_gateway = cp.get('apns', 'push_gateway')
    apns_push_concurrency = int(cp.get('apns', 'push_concurrency'))
    apns_push_max_notif_lag = float(cp.get('apns', 'push_max_notification_lag'))
    apns_push_max_error_wait = float(cp.get('apns', 'push_max_error_wait'))
    apns_feedback_gateway = cp.get('apns', 'feedback_gateway')
    apns_feedback_freq = float(cp.get('apns', 'feedback_frequency'))
    gcm_zmq_bind = cp.get('gcm', 'zmq_bind')
    gcm_server_url = cp.get('gcm', 'server_url')
    gcm_sqlitedb = cp.get('gcm', 'sqlite_db')
    gcm_api_key = cp.get('gcm', 'api_key')
    gcm_concurrency = int(cp.get('gcm', 'concurrency'))
    gcm_max_notif_lag = float(cp.get('gcm', 'max_notification_lag'))
except ConfigParser.Error as e:
    logging.error("%s: %s" % (CONFIGFILE, e))
    sys.exit(1)

if len(logfile) == 0:
        logging.basicConfig(level=logging.DEBUG,
            format='%(asctime)s %(message)s',
            datefmt='%Y/%m/%d %H:%M:%S')
else:
        logging.basicConfig(filename=logfile, level=logging.DEBUG,
            format='%(asctime)s %(message)s',
            datefmt='%Y/%m/%d %H:%M:%S')

if apns_devtok_format != 'base64' and apns_devtok_format != 'hex':
    logging.error("%s: Unknown device token format: %s" %
        (CONFIGFILE, apns_devtok_format))
    sys.exit(1)
devtokfmt = DeviceTokenFormater(apns_devtok_format)

#
# Check APNS push/feedback TLS connections.
#
apns_tlsconnect = TLSConnectionMaker(apns_cacerts, apns_cert, apns_key)
logging.info("Testing APNS push gateway...")
l = apns_push_gateway.split(':', 2)
apns_push_gateway = (l[0], int(l[1]))
s = apns_tlsconnect(apns_push_gateway, 0,
    "%s: Cannot connect to APNS (%%s:%%d): %%s" % CONFIGFILE)
if s is None:
    sys.exit(1)
s.close()

logging.info("Testing APNS feedback gateway...")
l = apns_feedback_gateway.split(':', 2)
apns_feedback_gateway = (l[0], int(l[1]))
apns_feedback_sock = apns_tlsconnect(apns_feedback_gateway, 0,
    "%s: Cannot connect to APNS feedback service (%%s:%%d): %%s" % CONFIGFILE)
if apns_feedback_sock is None:
    sys.exit(1)
# Do not close it because the APNS feedback service immediately sends
# something that we don't want to loose.

#
# Creation ZMQ sockets early so we don't waste other resource if it fails.
#
logging.info("ZMQ REP socket for APNS service bound on tcp://%s" %
    apns_zmq_bind)
try:
    zmqctx_r = zmq.Context()
    apns_zmqsock = zmqctx_r.socket(zmq.REP)
    apns_zmqsock.bind("tcp://%s" % apns_zmq_bind)
except zmq.core.error.ZMQError as e:
    print e
    sys.exit(2)

logging.info("ZMQ REP socket for GCM service bound on tcp://%s" %
    gcm_zmq_bind)
try:
    zmqctx_r = zmq.Context()
    gcm_zmqsock = zmqctx_r.socket(zmq.REP)
    gcm_zmqsock.bind("tcp://%s" % gcm_zmq_bind)
except zmq.core.error.ZMQError as e:
    print e
    sys.exit(2)

#
# Create persistent queues for notifications and feedback.
#
apns_pushq = PersistentQueue(apns_sqlitedb, 'notifications')
apns_feedbackq = PersistentQueue(apns_sqlitedb, 'feedback')
logging.info("%d APNS notifications retrieved from persistent storage" %
    apns_pushq.qsize())
logging.info("%d APNS feedbacks retrieved from persistent storage" %
    apns_feedbackq.qsize())

gcm_pushq = ChronologicalPersistentQueue(gcm_sqlitedb, 'notifications')
gcm_feedbackdb = GCMRegisterationIDSChanges(gcm_sqlitedb, 'feedback')
logging.info("%d GCM notifications retrieved from persistent storage" %
    gcm_pushq.qsize())
logging.info("%d GCM feedbacks retrieved from persistent storage" %
    gcm_feedbackdb.count())

#
# Daemonize.
#
if len(logfile) != 0:
    try:
        pid = os.fork()
    except OSError as e:
        logging.error("Cannot fork: %s" % e)
        sys.exit(2)
    if pid != 0:
        os._exit(0)
    os.setsid()

#
# Start APNS threads and Feedback one.
#
for i in range(apns_push_concurrency):
    t = APNSAgent(apns_pushq, apns_push_gateway, apns_push_max_notif_lag,
        apns_push_max_error_wait, apns_feedbackq, apns_tlsconnect)
    t.start()

t = APNSFeedbackAgent(apns_feedbackq, apns_feedback_sock,
    apns_feedback_gateway, apns_feedback_freq, apns_tlsconnect)
t.start()

for i in range(gcm_concurrency):
    t = GCMAgent(gcm_pushq, gcm_server_url, gcm_api_key,
        gcm_max_notif_lag, gcm_feedbackdb)
    t.start()

#
# Start APNSListener thread.
#
t = APNSListener(apns_zmqsock, apns_sqlitedb, apns_pushq, apns_feedbackq)
t.start()
t = GCMListener(gcm_zmqsock, gcm_pushq, gcm_feedbackdb)
t.run()
