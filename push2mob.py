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
import collections
import datetime
import getopt
import heapq
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
import signal
import ssl
import struct
import sys
import threading
import time
import types
import zmq

CHECKPOINT_TIME = 10
DUMP_QUERIES = False
CONFIGFILE = 'push2mob.conf'

def usage():
    print """Usage: push2mob.py [options]
Options:
  -c    Change configuration file (defaults to "./%s")
  -h    Show this help message""" % CONFIGFILE

def now():

        return time.time()

def hexdump(buf, chunklen = 16):
        output = ""
        l = chunklen
        while len(buf) > 0:
            b = buf[:l]
            buf = buf[l:]
            s = 3 * l - 1
            #b = b.ljust(l, '\000')
            fmt = "%-" + str(s) + "s%s%s"
            ouput = output + fmt % (' '.join("%02x" % ord(c) for c in b),
                ' ', ''.join(['.', c][c.isalnum()] for c in b))
            output = output + "\n"
        return output

def jsonload(payload):
    obj = None
    try:
        obj = json.loads(payload)
    except ValueError as e:
        return None
    # Python's json module accepts top-level non-list, non-array values
    # while RFC4627 doesn't.  Catch this here.
    if type(obj) is not types.ListType and type(obj) is not types.DictType:
        return None
    return obj


class Locker:
    def __init__(self, lock):
        self.l = lock
    def __enter__(self):
        self.l.acquire()
        return self.l
    def __exit__(self, type, value, traceback):
        self.l.release()


class AttributeHolder:
    def __init__(self, **kwargs):
        for k in kwargs:
            self.__dict__[k] = kwargs[k]

class PeriodicCallback(threading.Thread):

    def __init__(self):
        super(PeriodicCallback, self).__init__()
        self.daemon = True
        self.period = None

    def configure(self, period, cb):
        self.period = period
        self.cb = cb

    def run(self):
        while True:
            time.sleep(self.period)
            self.cb()

class Exiting(Exception):
    pass

class ExitHelper:
    """
    This object is used to notify all threads that we are planning to exit.
    Threads used it to notify they are ready.
    """
    def __init__(self):
        self.val = 0
        self.exiting = False
        self.cond = threading.Condition()

    def register(self):
        if self.exiting:
            return False
        with Locker(self.cond):
            if self.exiting:
                return False
            self.val += 1
        return True

    def checkexit(self):
        if not self.exiting:
            return
        with Locker(self.cond):
            self.val -= 1
            if self.val == 0:
                self.cond.notifyAll()
        raise Exiting()

    def signalexit(self):
        self.exiting = True

    def waitexit(self):
        with Locker(self.cond):
            while self.val != 0:
                self.cond.wait(1)


class CheckpointableQueue(Queue.Queue):

    def __init__(self, dbinfo):
        Queue.Queue.__init__(self)
        self.dbinfo = dbinfo
        self._init2()

    def _init2(self):
        conn = sqlite3.connect(self.dbinfo.db)
        conn.isolation_level = None
        try:
            c = conn.execute(
              "SELECT data FROM %s ORDER BY rowid" % self.dbinfo.table)
        except:
            return
        for e in c:
            self.queue.append(eval(e[0]))

    def __rename_table(self, conn):
        c = conn.execute(
          """SELECT name FROM sqlite_master WHERE type='table'
          AND name='%s';""" % self.dbinfo.table)
        if c.fetchone() != None:
            try:
                conn.execute("DROP TABLE old_%s" % self.dbinfo.table)
            except:
                pass
            conn.execute("ALTER TABLE %s RENAME to old_%s" % \
              (self.dbinfo.table, self.dbinfo.table))

    def checkpoint(self):
        i = 0
        with Locker(self.mutex):
            conn = sqlite3.connect(self.dbinfo.db)
            conn.isolation_level = None
            self.__rename_table(conn)
            c = conn.cursor()
            c.execute(
                """CREATE TABLE %s (
                rowid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                data BLOB);""" % self.dbinfo.table)
            c.execute("BEGIN")
            for e in self.queue:
                i += 1
                c.execute("INSERT INTO %s (data) VALUES(?)""" % \
                  self.dbinfo.table, (str(e), ))
            c.execute("END")
            conn.close()
        return i


class ChronologicalCheckpointableQueue(CheckpointableQueue):

    def __init__(self, dbinfo):
        CheckpointableQueue.__init__(self, dbinfo)

    def _init(self, maxsize):
        self.queue = []

    def _qsize(self, len=len):
        return len(self.queue)

    def _put(self, item, heappush=heapq.heappush):
        heappush(self.queue, item)

    def _get(self, heappop=heapq.heappop):
        return heappop(self.queue)


class OrderedPersistentQueue:
    """
    This is an ordered persistent queue (!).  Every object stored in this
    queue is associated with an ordering provided as a real number .  Objects
    are returned in order and are removed from database upon acknowledgement.
    Upon startup, all returned but unacknowledged are reset.  It completely
    relies on SQLite.
    """

    def __init__(self, dbinfo):
        self.cv = dbinfo.lock
        self._table = dbinfo.table
        self._sqlcon = sqlite3.connect(dbinfo.db, check_same_thread=False)
        self._sqlcon.isolation_level = None
        self._inbatch = False
        self._acklist = dbinfo.acklist
        self.cv.acquire()
        if not dbinfo.initialized:
            dbinfo.initialized = True
            self._sqlcon.execute(
                """CREATE TABLE IF NOT EXISTS %s (
                rowid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                inuse INTEGER NOT NULL DEFAULT 0,
                ordering REAL NOT NULL,
                data BLOB);""" % self._table)
            self._sqlcon.execute(
                """CREATE INDEX IF NOT EXISTS ordering ON %s (
                inuse, ordering)""" % self._table)
            self._sqlcon.execute(
                """UPDATE %s SET inuse = 0 WHERE inuse <> 0""" % self._table)
            # There is one checkpoint thread for table.  Given there may
            # be multiple connection on it, let the first thread handle
            # that stuff only.
            dbinfo.checkpointthread.configure(dbinfo.checkpointperiod,
              self._checkpoint_acks)
            dbinfo.checkpointthread.start()
        self.cv.release()

    def __del__(self):
        pass
        with Locker(self.cv):
            self._checkpoint_acks()

    def _sqlbegin(self):
        self._sqlcon.execute("BEGIN")

    def _sqlend(self):
        self._sqlcon.execute("END")

    def _sqlput(self, ordering, item):
        """
        Actually record in the database.  Return the uid of the
        inserted object.
        This must be called with self.cv locked.
        """

        cur = self._sqlcon.cursor()
        cur.execute(
            """INSERT INTO %s (ordering, data)
            VALUES(?, ?)""" % self._table, (ordering, str(item)))
        return cur.lastrowid

    def _sqlpick(self):
        """
        Lookup next item in the database.  Returns an object containing
        the following attributes: {uid, ordering, data} or None if there is
        no data available.
        This must be called with self.cv locked.
        """

        cur = self._sqlcon.cursor()
        cur.execute(
            """SELECT rowid, ordering, data from %s
            WHERE inuse = 0
            ORDER BY ordering LIMIT 1;""" % self._table)
        r = cur.fetchone()
        if r is None:
            return None
        return AttributeHolder(uid=r[0], ordering=r[1], data=eval(r[2]))

    def _sqlgrab(self, r):
        """
        Actually record the item as being in use in the database.
        The argument is the object returned by _sqlpick().
        This must be called with self.cv locked.
        """

        self._sqlcon.execute(
            """UPDATE %s SET inuse = 1
            WHERE rowid = ?;""" % self._table, (r.uid, ))

    def _sqlack(self, r):
        """
        Actually delete the item from the database.
        The argument is the object returned by _sqlpick().
        This must be called with self.cv locked.
        """

        # XXX Should we only ack grabbed items?
        self._sqlcon.execute(
            """DELETE FROM %s WHERE rowid = ?;""" % self._table,
            (r.uid, ))

    def _sqlreorder(self, r, ordering):
        """
        Reorder an item and mark it as unused.
        The first argument is the object returned by _sqlpick().
        This must be called with self.cv locked.
        """

        self._sqlcon.execute(
            """UPDATE %s SET ordering = ?, inuse = 0
            WHERE rowid = ?""" % self._table, (ordering, r.uid))

    def _sqlqsize(self):
        """
        Actually reckon the number of elements in the queue.
        """

        cur = self._sqlcon.cursor()
        cur.execute("SELECT COUNT(*) FROM %s" % self._table)
        r = cur.fetchone()
        return r[0]

    def _checkpoint_acks(self):
        """
        Record all accumulated acks since the last checkpoint in
        the database.  This method is called by an asynchronous
        thread so we have to lock the database (well, technically it
        should be only this object) to prevent the thread whose this
        object normally belongs to from messing with us.
        """
        with Locker(self.cv):
            self._sqlbegin()
            for t in self._acklist:
                self._sqlack(t)
            # This list is shared!  DO NOT REPLACE IT.
            self._acklist[:] = []
            self._sqlend()

    def startbatchinsert(self):
        while True:
            try:
                self._sqlbegin()
                break
            except:
                continue
        self._inbatch = True

    def endbatchinsert(self):
        self._inbatch = False
        with Locker(self.cv):
            # Lock is not needed to end the transaction actually, but there
            # is litte point to release it and then reacquire it right now.
            self._sqlend()
            self.cv.notifyAll()

    def put(self, ordering, item):
        """
        Put the item in the queue with the given ordering.
        Return the uid of the inserted item.
        """

        if self._inbatch:
            uid = self._sqlput(ordering, item)
            return uid
        with Locker(self.cv):
            uid = self._sqlput(ordering, item)
            self.cv.notify()
            return uid

    def _peek(self, timeout=None):
        """
        Return the next item in an ordered fashion.
        If nothing is available right now, it waits for timeout seconds.
        The result is a object with the following attributes
        {uid, ordering, data} or None if the timeout triggered.
        The timeout implementation is very rough but sufficient for our need.
        This must be called with self.cv locked.
        """

        loops = 0
        while True:
            r = self._sqlpick()
            if r is not None:
                   return r
            if timeout is not None:
                if timeout == 0:
                    return None
                if loops > 0:
                    return None
            self.cv.wait(timeout)
            loops = loops + 1

    def get(self, timeout=None):
        """
        Same as _peek() but grabs the retrieved object.
        """

        with Locker(self.cv):
            r = self._peek(timeout)
            if r is None:
                return None
            self._sqlgrab(r)
            return r

    def ack(self, t):
        """
        Delete item from the queue.  The argument is the object returned
        by the get() method.
        """

        with Locker(self.cv):
            self._acklist.append(t)

    def reorder(self, t, ordering):
        """
        Reorder item in the queue with the given ordering.  The first argument
        is the object returned by the get() method.
        """

        with Locker(self.cv):
            self._sqlreorder(t, ordering)

    def qsize(self):
        """
        Return the number of elements in the queue.
        """

        return self._sqlqsize()



class ChronologicalPersistentQueue(OrderedPersistentQueue):
    """
    This is a chronological persistent queue (!).  Every object stored in
    this queue is associated with a timestamp from the Epoch.  Objects
    are returned chronologically in a timely fashion and are removed from
    database upon acknowledgement.  Upon started, all returned but
    unacknowledged are reset.
    """

    _NEGLIGIBLEWAIT = 0.2

    def __init__(self, dbinfo):
        OrderedPersistentQueue.__init__(self, dbinfo)

    def put(self, when, item):
        """
        Put the item in the queue with the given timestamp.
        Return the uid of the inserted item.
        """

        if self._inbatch:
            uid = self._sqlput(when, item)
            return uid
        with Locker(self.cv):
            uid = self._sqlput(when, item)
            timedelta = when - now()
            if timedelta < self.__class__._NEGLIGIBLEWAIT:
                self.cv.notify()
            return uid

    def put_now(self, item):
        """
        Put the item in the queue with the timestamp set to current time.
        """

        self.put(now(), item)

    def get(self):
        """
        Return the next item chronologically in a timely fashion.
        If nothing is available right now, it waits.
        The result is a object with the following attributes
        {uid, ordering, data}.
        """

        with Locker(self.cv):
            while True:
                r = self._peek()
                timedelta = r.ordering - now()
                if timedelta >= self.__class__._NEGLIGIBLEWAIT:
                    self.cv.wait(timedelta)
                    continue
                break
            self._sqlgrab(r)
            return r


class PersistentFIFO(OrderedPersistentQueue):
    """
    This class is a very simple persistent (on-disk) FIFO queue.
    There is no need to ack the retrieved objects.
    """

    def __init__(self, dbinfo):
        OrderedPersistentQueue.__init__(self, dbinfo)

    def put(self, item):
        return OrderedPersistentQueue.put(self, now(), item)

    def get(self, timeout=None):
        r = OrderedPersistentQueue.get(self, timeout)
        if r is not None:
            self.ack(r)
        return r


class DeviceTokenFormater:

    def __init__(self, format):
        assert format == "base64" or format == "hex"
        self._format = format

    def __call__(self, devtok):
        if self._format == "base64":
           return base64.standard_b64encode(devtok)
        else:
            return ''.join("%02x" % ord(c) for c in devtok)


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
        self.certreq = ssl.CERT_REQUIRED
        if len(cacerts.strip()) == 0:
            self.cacerts = None
            self.certreq = ssl.CERT_NONE

        self.cert = cert
        if len(cert.strip()) == 0:
            self.cert = None

        self.key = key
        if len(key.strip()) == 0:
            self.key = None

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
                    cert_reqs=self.certreq, ca_certs=self.cacerts)
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
            # XXX Raise an exception or return an error.
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
            # XXX Raise an exception or return an error.
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

    def __init__(self, idx, logger, zmqsock, exithelper):
        threading.Thread.__init__(self)
        self.name = "GenericListener%d" % idx
        self.daemon = True
        self.l = logger
        self.zmqsock = zmqsock
        self.exithelper = exithelper

    def _send_error(self, msg, detail = None):
        """
        Returns an error message to the ZMQ peer.
        If `detail' is provided, is will be shown in the daemon log.
        """
        if detail is not None:
            fmt = "%s: %s"
            self.l.warning(fmt, msg, detail)
        else:
            fmt = "%s"
            self.l.warning(fmt, msg)
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
            arglist = re.split(Listener._WHTSP, cmdargs, nargs)
            # Don't use pop() so we raise an IndexError exception.
            cmdargs = arglist[nargs]
            del arglist[nargs]
            ntok, cmdargs = re.split(Listener._WHTSP, cmdargs, 1)
            ntok = int(ntok)
            devlist = re.split(Listener._WHTSP, cmdargs, ntok)
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
        self.exithelper.register()
        while True:
            # There is a small window where we can lose a request, but
            # we wouldn't have answered anything to the client so we
            # it is responsible to retry later.
            try:
                while True:
                    self.exithelper.checkexit()
                    if self.zmqsock.poll(1000):
                        break
            except Exiting:
                self.l.debug("Exiting...")
                break

            msg = self.zmqsock.recv()
            #
            # Parse line.
            msg = msg.strip()
            self.l.debug("Got command: %s" % msg)
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

APNS_DEVTOKLEN = 32

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

    def __init__(self, idx, logger, devtokfmt, pushq, gateway,
        maxerrorwait, feedbackq, tlsconnect, exithelper):

        threading.Thread.__init__(self)
        self.name = "Agent%d" % idx
        self.daemon = True
        self.l = logger
        self.devtokfmt = devtokfmt
        self.pushq = pushq
        self.gateway = gateway
        self.maxerrorwait = maxerrorwait
        self.feedbackq = feedbackq
        self.tlsconnect = tlsconnect
        self.exithelper = exithelper
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
            self.l.debug("Connection has been shut down abruptly: %s" % e)
            return False
        if len(buf) == 0:
            self.l.debug("Remote service closed the connection")
            return False

        fmt = '>BBI'
        if len(buf) != struct.calcsize(fmt):
            self.l.error("Unexpected error response size: " \
                "%d (!= %d)" % (len(buf), struct.calcsize(fmt)))
            return True
        # Bad...
        cmd, st, errident = struct.unpack(fmt, buf)
        errdevtok = self.recentnotifications.lookup(errident)
        if errdevtok is None:
            errdevtok = "unknown"
        else:
            errdevtok = self.devtokfmt(errdevtok)
        if st == APNSAgent._INVALIDTOKENSTATUS:
            self.feedbackq.put((0, errdevtok))
            self.l.info("Notification #%d to %s response: %s" %
                (errident, errdevtok, APNSAgent._error_responses[st]))
        else:
            estr = APNSAgent._error_responses.get(st)
            if estr is None:
                self.l.error("Unexpected error code %d in notification #%d " \
                    "to %s response" % (st, errident, errdevtok))
            else:
                self.l.warning("Notification #%d to %s response: %s" %
                    (errident, errdevtok, estr))

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
        self.exithelper.register()
        while True:
            timeout = 1

            # The sole purpose of this inner loop is too be able to
            # progressively increment the timeout, because as times goes on,
            # APNS will less likely send us an in-line error.
            apnsmsg = None
            try:
                while True:
                    if self.sock is None:
                        timeout = None

                    countdown = timeout if timeout is not None else 0xFFFFFFFF
                    while countdown > 0 and apnsmsg is None:
                        self.exithelper.checkexit()
                        try:
                            apnsmsg = self.pushq.get(True, 1)
                        except Queue.Empty:
                            countdown -= 1

                    if apnsmsg is not None:
                        break

                    triple = select.select([self.sock], [], [], 0)
                    if len(triple[0]) != 0:
                        self._processerror()
                        timeout = None
                    else:
                        # Try to be generous with APNS and give it enough
                        # time to return is error.
                        timeout = timeout * 2 - timeout / 2 - timeout / 4
                        if timeout >= 20:
                            timeout = None
                    continue
            except Exiting:
                self.l.debug("Exiting...")
                break

            uid, creation, expiry, devtok, payload = apnsmsg
            bintok = base64.standard_b64decode(devtok)

            # Build the binary message.
            fmt = '> B II' + 'H' + str(len(bintok)) + 's' + \
                'H' + str(len(payload)) + 's'
            # XXX Should we check the expiry?  We provide an absolute value
            # to APNS which may be in the past.  This is harmless though.
            binmsg = struct.pack(fmt, APNSAgent._EXTENDEDNOTIFICATION, uid,
                expiry, len(bintok), bintok, len(payload), payload)
            if DUMP_QUERIES:
                self.l.debug("Notification #%d: %s", (uid, hexdump(binmsg)))

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
                    self.l.debug("Retry (%d) to send notification "
                        "#%d to %s (previous attempt failed with: %s)" %
                        (trial, uid, self.devtokfmt(bintok), e))
                    self._connect()
                    continue
            if trial == APNSAgent._MAXTRIAL:
                self.l.warning("Cannot send notification #%d to %s, "
                    "abording" % (uid, self.devtokfmt(bintok)))
                continue
            self.recentnotifications.record(uid, bintok)

            lag = now() - creation
            self.l.info("Notification #%d sent delayed by %.3fs" % (uid, lag))

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

    def __init__(self, idx, logger, devtokfmt, feedbackq, sock, gateway,
        frequency, tlsconnect, exithelper):
        threading.Thread.__init__(self)
        self.name = "Feedback%d" % idx
        self.daemon = True
        self.sock = sock
        self.l = logger
        self.devtokfmt = devtokfmt
        self.feedbackq = feedbackq
        self.gateway = gateway
        self.frequency = frequency
        self.tlsconnect = tlsconnect
        self.exithelper = exithelper
        self.fmt = '> IH ' + str(APNS_DEVTOKLEN) + 's'
        self.tuplesize = struct.calcsize(self.fmt)

    def _close(self):
        self.sock.shutdown(socket.SHUT_RD)
        self.sock.close()
        self.sock = None

    def run(self):
        self.exithelper.register()
        while True:
            try:
                # self.sock is not None on the first run because we
                # inherits the socket from the main thread (which has
                # been used for testing purpose).
                if self.sock is None:
                    countdown = self.frequency
                    while countdown > 0:
                        self.exithelper.checkexit() 
                        time.sleep(1)
                        countdown -= 1

                    self.sock = self.tlsconnect(self.gateway, self.frequency,
                        "Couldn't connect to feedback service (%s:%d): %s")

                buf = ""
                while True:
                    # 10 seconds should be enough for APNS to send something!
                    b = None
                    for i in range(10):
                        self.exithelper.checkexit() 
                        triple = select.select([self.sock], [], [], 1)
                        if len(triple[0]) == 0:
                            continue
                        b = self.sock.recv()
                        if len(b) == 0:
                            if len(buf) != 0:
                                self.l.warning("Unexpected trailing garbage " \
                                    "from feedback service (%d bytes remaining)" %
                                    len(buf))
                                hexdump(buf)
                            break
                    if b is None or len(b) == 0:
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
                        devtok = self.devtokfmt(bintok)
                        ts = str(ts)
                        self.l.info("New feedback tuple (%s, %s)" %
                            (ts, devtok))
                        self.feedbackq.put((ts, devtok))

                self._close()
            except Exiting:
                self.l.debug("Exiting...")
                break


class APNSListener(Listener):
    """
    See the Listener class description.
    This class additionally maintains a persistent message ID
    in an SQLite table because it is required in the APNS
    enhanced notification format.
    """

    _PAYLOADMAXLEN = 256

    def __init__(self, idx, logger, zmqsock, exithelper, pushq, feedbackq):
        Listener.__init__(self, idx, logger, zmqsock, exithelper)
        self.name = "Listener%d" % idx
        self.l = logger
        self.pushq = pushq
        self.feedbackq = feedbackq
        self.uid = random.randint(0, 2**32)

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
            if len(dt) == APNS_DEVTOKLEN * 2:
                # Hexadecimal device token.
                for i in range(0, APNS_DEVTOKLEN * 2, 2):
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
            if len(devtok) != APNS_DEVTOKLEN:
                self._send_error("Wrong device token length (%d != %s): %s" %
                    (len(devtok), APNS_DEVTOKLEN, dt))
                return None
            # Store the token in base64 in the queue, text is better
            # to debug.
            goodtoks.append(base64.standard_b64encode(devtok))
        devtoks = goodtoks

        # Check payload length.
        if len(payload) > APNSListener._PAYLOADMAXLEN:
            self._send_error("Payload too long (%d > %d)" % len(payload),
                APNSListener._PAYLOADMAXLEN, payload)
            return None

        obj = jsonload(payload)
        if obj is None:
            self._send_error("Invalid JSON payload: %s" % payload)
            return None

        # Mimic _parse_send_args() return value.
        return (arglist, devtoks, payload)

    def _perform_send(self, arglist, devtoks, payload):
        expiry = arglist[0]

        idlist = []
        for devtok in devtoks:
            uid = self.uid
            self.uid += 1
            self.pushq.put((uid, now(), expiry, devtok, payload))
            idlist.append(str(uid))
            self.l.debug("Got notification #%d for device token %s, " \
                "expiring at %d" % (uid, base64.standard_b64encode(devtok), expiry))
        return ' '.join(idlist)

    def _perform_feedback(self):
        feedbacks = []
        while True:
            try:
                qobj = self.feedbackq.get_nowait()
            except Queue.Empty:
                break
            timestamp, devtok = qobj.data
            feedbacks.append("%s:%s" % (timestamp, devtok))
        return ' '.join(feedbacks)


#############################################################################
# GCM stuff.
#############################################################################

class GCMFeedbackDatabase:
    """
    This object records all recent changes to registration IDs as reported
    by CGM.  A registration ID can be replaced by a new one, not
    registered any more or invalid.
    Each time the queryAll() method is called, the current record is marked
    for deletion in "flushafter" seconds, this gives the opportunity to the
    user application to update its data.
    """

    _FLUSHAFTER = 10

    REPLACED = 1
    NOTREGISTERED = 2
    INVALID = 3

    def __init__(self, feedback_dbinfo):
        self.mutex = feedback_dbinfo.lock
        self.tstamp = 0
        self.flushafter = GCMFeedbackDatabase._FLUSHAFTER

        self.table = feedback_dbinfo.table
        self.sqlcon = sqlite3.connect(feedback_dbinfo.db)
        self.sqlcon.isolation_level = None
        self.sqlcur = self.sqlcon.cursor()
        self.sqlcur.execute(
            """CREATE TABLE IF NOT EXISTS %s (
            regid VARCHAR(256) PRIMARY KEY NOT NULL,
            state INTEGER NOT NULL DEFAULT 0,
            newregid VARCHAR(256),
            retrievetime REAL NOT NULL DEFAULT 0)""" % self.table)
        self.sqlcur.execute(
            """CREATE INDEX IF NOT EXISTS regid_retrtime ON %s (
            regid, retrievetime)""" % self.table)

    def _update(self, regid, state, newregid):

        with Locker(self.mutex):
            self.sqlcur.execute(
                """INSERT OR REPLACE INTO %s
                (regid, state, newregid)
                VALUES (?, ?, ?)""" % self.table,
                (regid, state, newregid))

    def replace(self, oregid, nregid):
        """
        A new registration ID (nregid) replaced the old one (oregid).
        """

        self._update(oregid, GCMFeedbackDatabase.REPLACED, nregid)

    def unregister(self, regid):
        """
        The registration ID has been unregistered (application removed
        from device).
        """

        self._update(regid, GCMFeedbackDatabase.NOTREGISTERED, "")

    def invalidate(self, regid):
        """
        The registration ID has been pointed as invalid by the server.
        """

        self._update(regid, GCMFeedbackDatabase.INVALID, "")

    def query(self, regid):
        """
        Return status of the requested registration ID as a tuple
        (state, newregid).
        """

        with Locker(self.mutex):
            # Look up only entries which have not been retrieved by the
            # "feedback" command (= 0) and those that have been retrieved
            # very recently (less than `flushafter' seconds) because we
            # consider the application may not have finished to handle them
            # completely yet.
            self.sqlcur.execute(
                """SELECT state, newregid FROM %s
                WHERE regid = ?
                AND (retrievetime == 0 OR retrievetime > ?)""" % self.table,
                (regid, now() - self.flushafter))
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
            if self.tstamp != 0 and \
              now() - self.tstamp >= self.flushafter:
                self.sqlcur.execute(
                    """DELETE FROM %s
                    WHERE retrievetime > 0
                    AND retrievetime <= ?""" % self.table,
                    (self.tstamp, ))

            self.tstamp = now()
            self.sqlcur.execute(
                """UPDATE %s SET retrievetime = ?
                WHERE retrievetime = 0"""  % self.table,
                (self.tstamp, ))
            self.sqlcur.execute(
                """SELECT regid, state, newregid FROM %s
                WHERE retrievetime = ?;""" % self.table,
                (self.tstamp, ))
            r = self.sqlcur.fetchall()
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


class GCMExponentialBackoffDatabase:
    """
    This object implements the exponential back-off algorithm as
    described in the following URL:
    https://developers.google.com/google-apps/documents-list/#implementing_exponential_backoff
    UIDs are only kept for a limited but yet quite large amount of time
    in memory, so long running daemons won't eat too much memory.
    This is an in-memory only database.
    """

    @staticmethod
    def _getdelay(n, retryafter):
        expbackoff = (2 ** n) + float(random.randint(0, 1000)) / 1000
        return max(expbackoff, retryafter)

    def __init__(self, maxretries):
        # Compute minimum rotate time with an arbitrary Retry-After
        # header set to 600 seconds.  And just to be sure multiply the
        # result by 2.
        # maxretries    rotatetime
        # 1             1200 (20 min)
        # 2             2400 (40 min)
        # 5             6000 (100 min)
        rotatetime = 0
        for i in range(1, maxretries):
            expbackoff = (2 ** i) + 1
            rotatetime = max(600, expbackoff)
        self.rotatetime = rotatetime * 2

        self.maxretries = maxretries
        self.tstamp = now()
        self.uids = [{}, {}]
        self.i = 0

    def _rotate(self):
        # This part should be protected by a mutex if the object was
        # accessed by multiple threads.
        if now() - self.tstamp >= self.rotatetime:
            i = (self.i + 1) % 2
            self.uids[i] = {}
            self.i = i
            self.tstamp = now()

    def schedule(self, uid, retryafter):
        self._rotate()
        i0 = self.i
        i1 = (self.i + 1) % 2
        if uid in self.uids[i0]:
            i = i0
        elif uid in self.uids[i1]:
            i = i1
        else:
            self.uids[i0][uid] = 0
            return GCMExponentialBackoffDatabase._getdelay(0, retryafter)

        n = self.uids[i][uid] + 1
        if n > self.maxretries:
            return None

        self.uids[i][uid] = n
        return GCMExponentialBackoffDatabase._getdelay(n, retryafter)


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
        resp = HTTPResponseReceiver()
        self.curl.setopt(pycurl.WRITEFUNCTION, resp.write)
        self.curl.setopt(pycurl.POSTFIELDS, jsonmsg)
        self.curl.perform()
        return resp


class GCMAgent(threading.Thread):

    _error_strings = {
        'MissingRegistration'   : 'Missing Registration ID',
        'InvalidRegistration'   : 'Invalid Registration ID',    # invalidate
        'MismatchSenderId'      : 'Mismatched Sender',          # invalidate
        'NotRegistered'         : 'Unregistered Device',        # unregister
        'MessageTooBig'         : 'Message Too Big',
        'InvalidDataKey'        : 'Invalid Data Key',
        'InvalidTtl'            : 'Invalid Time To Live',
        'Unavailable'           : 'Timeout',                    # retry
        'InternalServerError'   : 'Internal Server Error',      # retry
        # Stolen from Google's gcm-server source code
        'QuotaExceeded'         : 'Quota Exceeded',             # retry
        'DeviceQuotaExceeded'   : 'Device Quota Exceeded',      # retry
        'MissingCollapseKey'    : 'Missing Collapse Key'
    }

    def __init__(self, idx, logger, pushq, server_url, api_key,
        min_interval, dry_run, expbackoffdb, feedback_dbinfo, exithelper):

        threading.Thread.__init__(self)
        self.name = "Agent%d" % idx
        self.daemon = True
        self.l = logger
        self.pushq = pushq
        self.gcmreq = GCMHTTPRequest(server_url, api_key)
        self.mininterval = min_interval
        self.dryrun = dry_run
        self.expbackoffdb = expbackoffdb
        self.feedback_dbinfo = feedback_dbinfo
        self.exithelper = exithelper

    def run(self):
        self.feedbackdb = GCMFeedbackDatabase(self.feedback_dbinfo)

        needsleep = 0
        gcmmsg = None
        self.exithelper.register()
        while True:
            if needsleep:
                time.sleep(self.mininterval)
            needsleep = 1

            gcmmsg = None
            try:
                while gcmmsg is None:
                    self.exithelper.checkexit()
                    try:
                        gcmmsg = self.pushq.get(True, 1)
                    except Queue.Empty:
                        pass
            except Exiting:
                self.l.debug("Exiting...")
                break

            uid, creation, collapsekey, expiry, delayidle, devtoks, payload = \
                gcmmsg[1]

            # We store an absolute value but GCM wants a relative TTL.
            # Semantically this makes sense to adjust the TTL just
            # before handing the notification to the GCM service.
            ttl = int(round(expiry - now()))
            if ttl < 1:
                self.l.warning("Discarding notification #%d: " \
                    "time-to-live exceeded by %us (ttl: %us)" %
                    (uid, -ttl, round(expiry - creation)))
                continue

            # Build the JSON request.
            req = {}
            req['registration_ids'] = devtoks
            req['collapse_key'] = collapsekey
            req['data'] = payload
            req['delay_while_idle'] = delayidle
            req['time_to_live'] = ttl
            if self.dryrun:
                req['dry_run'] = True
            jsonmsg = json.dumps(req, indent=4, separators=(', ',': '))

            if DUMP_QUERIES:
                self.l.debug("Notification #%d: %s", (uid, jsonmsg))
            jsonmsg = json.dumps(req, separators=(',',':'))

            httpresp = self.gcmreq.send(jsonmsg)
            status = httpresp.getStatus()
            jsonresp = ''.join(httpresp.getBody())
            resphdrs = httpresp.getHeaders()
            retryafterhdr = 0
            try:
                retryafterhdr = int(resphdrs['Retry-After'])
            except KeyError as e:
                retryafterhdr = 0
            except ValueError as e:
                # TODO We can handle Retry-After: being a date here.
                retryafterhdr = 0

            # First check status code.
            if status == 200:
                pass
            elif status == 400:
                self.l.error("Invalid JSON in notification #%d " \
                    "(details: %s): %s" % (uid, jsonresp, jsonmsg))
                continue
            elif status == 401:
                # GCM provides a response but nothing relevant for the
                # possible causes of this error.
                self.l.error("Authentication error for " \
                    "notification #%d (details: %s): %s" %
                    (uid, jsonresp, jsonmsg))
                continue
            elif status == 500 or status == 503:
                delay = self.expbackoffdb.schedule(uid, retryafter)
                if delay is not None:
                    self.pushq.put((now() + delay, gcmmsg))
                # These errors happen from time to time, they are not
                # strictly errors, so just issue warnings.
                if status == 500:
                    self.l.warning("Internal server error for " \
                        "notification #%d, retrying in %.3fs, but this should" \
                        "probably be reported to GCM Error body (details: %s)" %
                        (uid, delay, jsonresp))
                else: # status == 503
                    self.l.warning("Service unavailable for " \
                        "notification #%d, retrying in %.3fs (details: %s)" %
                        (uid, delay, jsonresp))
                # Don't ack the gcmmsg.  It has been handled above.
                gcmmsg = None
                continue
            else:
                self.l.error("Unexpected HTTP status code %d in " \
                    "notification #%d (details: %s): %s" %
                    (status, uid, jsonresp, jsonmsg))
                continue

            # Now check the body.
            try:
                resp = json.loads(jsonresp)
            except Exception as e:
                self.l.error("Couldn't decode JSON returned in" \
                    "notification #%d: %s" %
                    (uid, jsonresp))
                continue

            lag = now() - creation
            self.l.info("Notification #%d sent delayed by %.3fs as id %s: " \
                "success %d, failure %d, canonical_ids %d" %
                (uid, lag, resp['multicast_id'],
                 resp['success'], resp['failure'], resp['canonical_ids']))
            if resp['failure'] == 0 and resp['canonical_ids'] == 0:
                continue

            if len(devtoks) != len(resp['results']):
                self.l.warning("Weird number of results in " \
                    "notification #%d (%d devices, %d results): %s" %
                    (uid, len(devtoks), len(data['results'], jsonresp)))

            devtoks2retry = []
            for i in range(len(devtoks)):
                devtok = devtoks[i]
                result = resp['results'][i]

                if 'message_id' in result:
                    if 'registration_id' not in result:
                        continue
                    self.l.info("In notification #%d, registration ID %s " \
                        "has been replaced by %s" %
                        (uid, devtok, result['registration_id']))
                    self.feedbackdb.replace(devtok, result['registration_id'])
                    continue

                try:
                    error = result['error']
                except KeyError as e:
                    self.l.warning("Expected 'error' in results[%d] in" \
                        "notification #%d: %s" % (i, uid, jsonresp))
                    continue

                emsg = ""
                try:
                    emsg = GCMAgent._error_strings[error]
                except KeyError as e:
                    self.l.error("Unexpected error for registration " \
                        "ID %s in notification #%d: %s" %
                        (devtok, uid, error))
                    continue
                self.l.info("%s for registration ID %s " \
                    "in notification #%d" % (emsg, devtok, uid))

                # Special actions for some errors.
                if error == 'InvalidRegistration' or \
                    error == 'MismatchSenderId':
                    self.feedbackdb.invalidate(devtok)
                elif error == 'NotRegistered':
                    self.feedbackdb.unregister(devtok)
                elif error == 'Unavailable' or \
                     error == 'InternalServerError' or \
                     error == 'QuotaExceeded' or \
                     error == 'DeviceQuotaExceeded':
                    devtoks2retry.append(devtok)

            if len(devtoks2retry) == 0:
                continue
            delay = self.expbackoffdb.schedule(uid, retryafter)
            if delay is None:
                continue

            if len(devtoks2retry) == len(devtoks):
                self.pushq.put((now() + delay, gcmmsg))
                continue

            self.pushq.put((now() + delay, (uid, creation, collapsekey, expiry,
                delayidle, devtoks2retry, payload)))


class GCMListener(Listener):
    """
    See the Listener class description.
    This class additionally maintains a persistent message ID
    in an SQLite table because it is required in the APNS
    enhanced notification format.
    """

    _MAXNUMIDS = 1000
    _MAXTTL = 2419200       # 4 weeks
    _PAYLOADMAXLEN = 4096

    def __init__(self, idx, logger, zmqsock, exithelper, pushq,
      feedback_dbinfo):
        Listener.__init__(self, idx, logger, zmqsock, exithelper)
        self.name = "Listener%d" % idx
        self.l = logger
        self.pushq = pushq
        self.feedback_dbinfo = feedback_dbinfo
        self.uid = random.randint(0, 2**32)

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
        if round(expiry - now()) > GCMListener._MAXTTL:
            self._send_error("Expiry value too high " \
                "(max %ds in the future): %s" %
                (GCMListener._MAXTTL, expiry))

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

        goodids = []
        for i in ids:
            r = self.idschanges.query(i)
            if r is None:
                goodids.append(i)
                continue
            state, newi = r
            if state == GCMFeedbackDatabase.REPLACED:
                goodids.append(newi)
                continue
            elif state == GCMFeedbackDatabase.NOTREGISTERED or \
                state == GCMFeedbackDatabase.INVALID:
                # Our client didn't get feedback yet, just discard the
                # message, it is not an error.
                continue

        if len(goodids) == 0:
            self._send_error("All registrations IDs have been filtered out, " \
                "please get feedback")
            return None
        ids = goodids

        # Check payload.
        if len(payload) > GCMListener._PAYLOADMAXLEN:
            self._send_error("Payload too long (%d > %d)" % len(payload),
                GCMListener._PAYLOADMAXLEN, payload)
            return None

        obj = jsonload(payload)
        if obj is None:
            self._send_error("Invalid JSON payload: %s" % payload)
            return None
        payload = obj

        # Mimic _parse_send_args() return value.
        return (arglist, ids, payload)

    def _perform_send(self, arglist, devtoks, payload):
        collapsekey = arglist[0]
        expiry = arglist[1]
        delayidle = arglist[2]

        createtime = now()
        uids = []
        while len(devtoks) > 0:
            toks = devtoks[:GCMListener._MAXNUMIDS]
            devtoks = devtoks[GCMListener._MAXNUMIDS:]
            uid = self.uid
            self.uid += 1
            self.pushq.put((createtime, (uid, createtime, collapsekey,
                expiry, delayidle, toks, payload)))
            self.l.debug("Got notification #%d for %d devices, " \
                "expiring at %d" % (uid, len(toks), expiry))
            uids.append(str(uid))
        return ' '.join(uids)

    def _perform_feedback(self):
        feedbacks = self.idschanges.queryAll()
        for i in range(len(feedbacks)):
            t = feedbacks[i]
            if t[1] == GCMFeedbackDatabase.REPLACED:
                s = "replaced"
            elif t[1] == GCMFeedbackDatabase.NOTREGISTERED:
                s = "notregistered"
            elif t[1] == GCMFeedbackDatabase.INVALID:
                s = "invalid"
            # t[0] and t[2] are unicode strings.
            s = "%s:%s:%s" % (str(t[0]), s, str(t[2]))
            feedbacks[i] = s
        return ' '.join(feedbacks)

    def run(self):
        self.idschanges = GCMFeedbackDatabase(self.feedback_dbinfo)
        Listener.run(self)


#############################################################################
# Main.
#############################################################################

def parse_loglevel(l):
    loglevels = {
        'debug'   : logging.DEBUG,
        'info'    : logging.INFO,
        'warning' : logging.WARNING,
        'error'   : logging.ERROR
    }
    ret = loglevels.get(l)
    if ret is None:
        raise Exception("Unknown log level: %s" % l)
    return ret

def createLogger(name, logfile, level, propagate, formatter):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if len(logfile) == 0:
        handler = logging.FileHandler("/dev/null")
        logger.addHandler(handler)
        logger.propagate = True
        return logger

    logger.propagate = propagate
    handler = logging.FileHandler(logfile)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

if __name__ == "__main__":
    # For early log messages.
    logging.basicConfig(level=logging.DEBUG,
        format='%(asctime)s MAIN/%(threadName)s: %(message)s',
        datefmt='%Y/%m/%d %H:%M:%S')

    #
    # Parse command-line options.
    #

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
        daemon = cp.getboolean('main', 'daemon')
        logfile = cp.get('main', 'log_file')
        try:
            loglevel = parse_loglevel(cp.get('main', 'log_level'))
        except Exception as e:
            raise Exception("main.log_level: %s" % e)
        apns_zmq_bind = cp.get('apns', 'zmq_bind')
        apns_sqlitedb = cp.get('apns', 'sqlite_db')
        apns_tableprefix = cp.get('apns', 'table_prefix')
        apns_logfile = cp.get('apns', 'log_file')
        try:
            apns_loglevel = parse_loglevel(cp.get('apns', 'log_level'))
        except Exception as e:
            raise Exception("apns.log_level: %s" % e)
        apns_logpropagate = cp.getboolean('apns', 'log_propagate')
        apns_cacerts = cp.get('apns', 'cacerts_file')
        apns_cert = cp.get('apns', 'cert_file')
        apns_key = cp.get('apns', 'key_file')
        apns_devtok_format = cp.get('apns', 'device_token_format')
        apns_push_gateway = cp.get('apns', 'push_gateway')
        apns_push_concurrency = cp.getint('apns', 'push_concurrency')
        apns_push_max_error_wait = cp.getfloat('apns', 'push_max_error_wait')
        apns_feedback_gateway = cp.get('apns', 'feedback_gateway')
        apns_feedback_freq = cp.getfloat('apns', 'feedback_frequency')
        gcm_zmq_bind = cp.get('gcm', 'zmq_bind')
        gcm_server_url = cp.get('gcm', 'server_url')
        gcm_sqlitedb = cp.get('gcm', 'sqlite_db')
        gcm_tableprefix = cp.get('gcm', 'table_prefix')
        gcm_logfile = cp.get('gcm', 'log_file')
        try:
            gcm_loglevel = parse_loglevel(cp.get('gcm', 'log_level'))
        except Exception as e:
            raise Exception("gcm.log_level: %s" % e)
        gcm_logpropagate = cp.getboolean('gcm', 'log_propagate')
        gcm_api_key = cp.get('gcm', 'api_key')
        gcm_concurrency = cp.getint('gcm', 'concurrency')
        gcm_max_retries = cp.getint('gcm', 'max_retries')
        gcm_min_interval = cp.getfloat('gcm', 'min_interval')
        gcm_dry_run = cp.getboolean('gcm', 'dry_run')
    except BaseException as e:
        logging.error("%s: %s" % (CONFIGFILE, e))
        sys.exit(1)

    if daemon and len(logfile) == 0:
        logging.error("Option main.log_file cannot be empty in daemon mode")
        sys.exit(1)

    if apns_devtok_format != 'base64' and apns_devtok_format != 'hex':
        main_logger.error("%s: Unknown device token format: %s" %
            (CONFIGFILE, apns_devtok_format))
        sys.exit(1)
    apns_devtokfmt = DeviceTokenFormater(apns_devtok_format)

    #
    # Configure logging.
    #
    formatter = logging.Formatter('%(asctime)s %(name)s/%(threadName)s: ' \
        '%(message)s', '%Y/%m/%d %H:%M:%S')
    main_logger = createLogger('push2mob', logfile, loglevel, False, formatter)
    main_logger.propagate = False
    if not daemon:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        main_logger.addHandler(handler)
    apns_logger = createLogger('push2mob.APNS', apns_logfile, apns_loglevel,
        apns_logpropagate, formatter)
    gcm_logger = createLogger('push2mob.GCM', gcm_logfile, gcm_loglevel,
        gcm_logpropagate, formatter)

    #
    # Daemonize.
    #
    if daemon:
        try:
            pid = os.fork()
        except OSError as e:
            main_logger.error("Cannot fork: %s" % e)
            sys.exit(2)
        if pid != 0:
            os._exit(0)
        os.setsid()

    try:
        main_logger.warning("Starting with pid %u..." % os.getpid())

        #
        # Check APNS push/feedback TLS connections.
        #
        apns_tlsconnect = TLSConnectionMaker(apns_cacerts, apns_cert, apns_key)
        main_logger.info("Testing APNS push gateway...")
        l = apns_push_gateway.split(':', 2)
        apns_push_gateway = (l[0], int(l[1]))
        s = apns_tlsconnect(apns_push_gateway, 0,
            "%s: Cannot connect to APNS (%%s:%%d): %%s" % CONFIGFILE)
        if s is None:
            sys.exit(3)
        s.close()

        main_logger.info("Testing APNS feedback gateway...")
        l = apns_feedback_gateway.split(':', 2)
        apns_feedback_gateway = (l[0], int(l[1]))
        apns_feedback_sock = apns_tlsconnect(apns_feedback_gateway, 0,
            "%s: Cannot connect to APNS feedback service (%%s:%%d): %%s" % \
            CONFIGFILE)
        if apns_feedback_sock is None:
            sys.exit(3)
        # Do not close it because the APNS feedback service immediately sends
        # something that we don't want to loose.

        #
        # Creation ZMQ sockets early so we don't waste other resource
        # if it fails.
        #
        main_logger.info("ZMQ REP socket for APNS service bound on tcp://%s" %
            apns_zmq_bind)
        try:
            zmqctx_r = zmq.Context()
            apns_zmqsock = zmqctx_r.socket(zmq.REP)
            apns_zmqsock.bind("tcp://%s" % apns_zmq_bind)
        except zmq.core.error.ZMQError as e:
            main_logger.error("Cannot create ZMQ REP socket for APNS on " \
                "tcp://%s: %s" % (apns_zmq_bind, e))
            sys.exit(3)

        main_logger.info("ZMQ REP socket for GCM service bound on tcp://%s" %
            gcm_zmq_bind)
        try:
            zmqctx_r = zmq.Context()
            gcm_zmqsock = zmqctx_r.socket(zmq.REP)
            gcm_zmqsock.bind("tcp://%s" % gcm_zmq_bind)
        except zmq.core.error.ZMQError as e:
            main_logger.error("Cannot create ZMQ REP socket for GCM on " \
                "tcp://%s: %s" % (gcm_zmq_bind, e))
            sys.exit(3)

        #
        # Create persistent queues for notifications and feedback.
        #
        apns_push_dbinfo = AttributeHolder(db=apns_sqlitedb,
            table=('%s_notifications' % apns_tableprefix),
            checkpointthread=PeriodicCallback())
        apns_feedback_dbinfo = AttributeHolder(db=apns_sqlitedb,
            table=('%s_feedback' % apns_tableprefix),
            checkpointthread=PeriodicCallback())
        gcm_push_dbinfo = AttributeHolder(db=gcm_sqlitedb,
            table=('%s_notifications' % gcm_tableprefix),
            checkpointthread=PeriodicCallback())
        gcm_feedback_dbinfo = AttributeHolder(db=gcm_sqlitedb,
            table='%s_feedback' % gcm_tableprefix,
            lock=threading.Lock(),
            checkpointthread=PeriodicCallback())

        apns_pushq = CheckpointableQueue(apns_push_dbinfo)
        main_logger.info("%d APNS notifications retrieved from persistent " \
            "storage" % apns_pushq.qsize())
        apns_feedbackq = CheckpointableQueue(apns_feedback_dbinfo)
        main_logger.info("%d APNS feedbacks retrieved from persistent " \
            "storage" % apns_feedbackq.qsize())
        gcm_pushq = ChronologicalCheckpointableQueue(gcm_push_dbinfo)
        main_logger.info("%d GCM notifications retrieved from persistent " \
            "storage" % gcm_pushq.qsize())
        db = GCMFeedbackDatabase(gcm_feedback_dbinfo)
        main_logger.info("%d GCM feedbacks retrieved from persistent " \
            "storage" % db.count())
        del db

        gcm_expbackoffdb = GCMExponentialBackoffDatabase(gcm_max_retries)

        #
        # Prepare the exit door.
        #
        exithelper = ExitHelper()
        def exit_handler(signum, frame):
            main_logger.info("Exit requested, waiting threads acknowledgement...")
            exithelper.signalexit()
        signal.signal(signal.SIGTERM, exit_handler)
        signal.signal(signal.SIGINT, exit_handler)

        #
        # Start APNS ang GCM agent threads and APNS feedback one.
        #
        threadlist = []
        for i in range(apns_push_concurrency):
            t = APNSAgent(i, apns_logger, apns_devtokfmt, apns_pushq,
                apns_push_gateway, apns_push_max_error_wait,
                apns_feedbackq, apns_tlsconnect, exithelper)
            threadlist.append(t)
            t.start()

        t = APNSFeedbackAgent(0, apns_logger, apns_devtokfmt,
            apns_feedbackq, apns_feedback_sock, apns_feedback_gateway,
            apns_feedback_freq, apns_tlsconnect, exithelper)
        threadlist.append(t)
        t.start()

        for i in range(gcm_concurrency):
            t = GCMAgent(i, gcm_logger, gcm_pushq, gcm_server_url,
                gcm_api_key, gcm_min_interval, gcm_dry_run, gcm_expbackoffdb,
                gcm_feedback_dbinfo, exithelper)
            threadlist.append(t)
            t.start()

        #
        # Start APNSListener and GCMListener threads.
        #
        t = APNSListener(0, apns_logger, apns_zmqsock, exithelper, apns_pushq,
            apns_feedbackq)
        threadlist.append(t)
        t.start()
        t = GCMListener(0, gcm_logger, gcm_zmqsock, exithelper, gcm_pushq,
            gcm_feedback_dbinfo)
        threadlist.append(t)
        t.start()

        exithelper.waitexit()
        apns_pushq_size = apns_pushq.checkpoint()
        apns_feedbackq_size = apns_feedbackq.checkpoint()
        gcm_pushq_size = gcm_pushq.checkpoint()
        main_logger.info("Checkpointed %u APNS notifications, " \
          "%u APNS feedback tuples, %u GCM notifications" %
          (apns_pushq_size, apns_feedbackq_size, gcm_pushq_size))
        # Never reached.
        sys.exit(0)

    except SystemExit:
        # This exception is raised by sys.exit().
        pass
    except BaseException as e:
        main_logger.exception("Uncaugth exception: %s" % e)
        sys.exit(99)
