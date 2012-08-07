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
import getopt
import logging
import os
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

       return int(time.time())

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

    def __call__(self, peer):
        """
        Creates an SSL socket to the given `peer', which is a tuple
        (host, port).
        """
        ai = socket.getaddrinfo(peer[0], peer[1], 0, 0, socket.IPPROTO_TCP)
        s = socket.socket(ai[0][0], ai[0][1], ai[0][2])
        sslsock = ssl.wrap_socket(s, keyfile=self.key, certfile=self.cert,
            server_side=False, cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=self.cacerts)
        sslsock.connect(ai[0][4])
        return sslsock

# This will be used as a function
tlsconnect = None


class RecentNotifications(threading.Thread):
    """
    Each instance of this class goes with one APNSAgent instance.
    It records notifications that have been recently sent by this
    agent.
    """

    def __init__(self, maxerrorwait):
        threading.Thread.__init__(self)
        if maxerrorwait != 0:
            # For an error wait of 0.1 seconds, this
            # will rotate the dicts every minute.
            self.swtime = 600 * maxerrorwait
        else:
            self.swtime = 10
        self.n = [{}, {}]
        self.i = 0

    def run(self):
        while True:
            time.sleep(self.swtime)
            j = (self.i + 1) % 2
            self.n[j] = {}
            self.i = j

    def record(self, ident, notification):
        self.n[self.i][ident] = notification

    def lookup(self, ident):
        i0 = self.i
        i1 = (i + 1) % 2
        n = self.n[i0].get(ident)
        if n is None:
            n = self.n[i1].get(ident)
        return n


class APNSAgent(threading.Thread):

    _EXTENDEDNOTIFICATION = 1
    _MAXTRIAL = 2
    _INVALIDTOKENSTATUS = 8

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

    def __init__(self, queue, gateway, maxnotiflag, maxerrorwait, feedbackq):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gateway = gateway
        self.maxnotiflag = maxnotiflag
        self.maxerrorwait = maxerrorwait
        self.feedbackq = feedbackq
        # Tuple: (id, bintok)
        self.recentnotifications = RecentNotifications(maxerrorwait)
        self.sock = None

    def _connect(self):
        try:
            self.sock = tlsconnect(self.gateway)
        except Exception as e:
            logging.error("Couldn't connect to APNS (%s:%d): %s" %
                (self.gateway[0], self.gateway[1], e))
            sys.exit(3)
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


class FeedbackAgent(threading.Thread):

    def __init__(self, queue, sock, gateway, frequency):
        threading.Thread.__init__(self)
        self.daemon = True
        self.sock = sock
        self.queue = queue
        self.gateway = gateway
        self.frequency = frequency
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
                self.sock = tlsconnect(self.gateway)

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


class Listener(threading.Thread):

    _PAYLOADMAXLEN = 256
    _WHTSP = re.compile("\s+")

    def __init__(self, sqlitedb, zmqsock, apnsq, feedbackq):
        threading.Thread.__init__(self)
        self.sqlitedb = sqlitedb
        self.zmqsock = zmqsock
        self.apnsq = apnsq
        self.feedbackq = feedbackq

    def _error(self, msg, detail = None):
        if detail is not None:
            fmt = msg + ": %s"
            logging.warning(fmt, detail)
        else:
            logging.warning(msg)
        self.zmqsock.send("ERROR " + msg)

    def _parse_send(self, msg):
        cmdargs = msg[5:]
        try:
            l = re.split(Listener._WHTSP, cmdargs, 2)
            expiry = int(l[0])
            ntok = int(l[1])
            l = re.split(Listener._WHTSP, l[2], ntok)
            devtoks = l[0:ntok]
            payload = l[ntok]
        except Exception as e:
            self._error("Invalid input", msg)
            return None

        goodtoks = []
        wrongtok = 0
        for dt in devtoks:
            if wrongtok:
                    break
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
                    self._error("Wrong base64 encoding for device " \
                        "token: %s" % dt)
                    wrongtok = 1
                    continue
            if len(devtok) != DEVTOKLEN:
                self._error("Wrong device token length " \
                    "(%d != %s): %s" % (len(devtok), DEVTOKLEN, dt))
                wrongtok = 1
                continue
            # Store the token in base64 in the queue, text is better
            # to debug.
            logging.debug("Got notification for device token %s" % \
                base64.standard_b64encode(devtok))
            goodtoks.append(base64.standard_b64encode(devtok))
        devtoks = goodtoks
        if wrongtok:
            return None

        if len(payload) > Listener._PAYLOADMAXLEN:
            self._error("Payload too long (%d > %d)" % len(payload),
                Listener._PAYLOADMAXLEN, payload)
            return None

        return (expiry, devtoks, payload)

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
        curid = row[0]
        logging.info("Notifications current identifier is %d" % curid)

        while True:
            msg = self.zmqsock.recv()
            #
            # Parse line.
            msg = msg.strip()
            if msg[0:5].lower().find("send ") == 0:
                res = self._parse_send(msg)
                if res == None:
                    continue
                expiry, devtoks, payload = res
                sqlcur.execute('UPDATE ident SET cur=?',
                    (curid + len(devtoks), ))
                #
                # Enqueue notifications.
                ids = []
                for devtok in devtoks:
                    self.apnsq.put((curid, now(), expiry, devtok, payload))
                    ids.append(str(curid))
                    curid = curid + 1
                self.zmqsock.send("OK %s" % ' '.join(ids))
                continue

            elif msg.lower().find("feedback") == 0:
                feedbacks = []
                try:
                    while True:
                        timestamp, devtok = self.feedbackq.get_nowait()
                        feedbacks.append("%s:%s" % (timestamp, devtok))
                except Queue.Empty:
                        pass
                if len(feedbacks) == 0:
                    self.zmqsock.send("OK")
                else:
                    self.zmqsock.send("OK "+ ' '.join(feedbacks))
                continue

            self._error("Invalid input", msg)


#
# Main.
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
    zmq_bind = cp.get('apnsd', 'zmq_bind')
    sqlitedb = cp.get('apnsd', 'sqlite_db')
    logfile = cp.get('apnsd', 'daemon_log_file')
    cacerts = cp.get('apnsd', 'cacerts_file')
    cert = cp.get('apnsd', 'cert_file')
    key = cp.get('apnsd', 'key_file')
    devtok_format = cp.get('apnsd', 'device_token_format')
    apns_gateway = cp.get('apns', 'gateway')
    apns_concurrency = int(cp.get('apns', 'concurrency'))
    apns_max_notif_lag = int(cp.get('apns', 'max_notification_lag'))
    apns_max_error_wait = float(cp.get('apns', 'max_error_wait'))
    feedback_gateway = cp.get('feedback', 'gateway')
    feedback_freq = int(cp.get('feedback', 'frequency'))
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

if devtok_format != 'base64' and devtok_format != 'hex':
    logging.error("%s: Unknown device token format: %s" %
        (CONFIGFILE, devtok_format))
    sys.exit(1)
devtokfmt = DeviceTokenFormater(devtok_format)

#
# Check APNS/feedback TLS connections.
#
tlsconnect = TLSConnectionMaker(cacerts, cert, key)
logging.info("Testing APNS gateway...")
try:
    l = apns_gateway.split(':', 2)
    apns_gateway = tuple(l)
    s = tlsconnect(apns_gateway)
    s.close()
except Exception as e:
    logging.error("%s: Cannot connect to APNS: %s" % (CONFIGFILE, e))
    sys.exit(1)

logging.info("Testing feedback gateway...")
try:
    l = feedback_gateway.split(':', 2)
    feedback_gateway = tuple(l)
    feedback_sock = tlsconnect(feedback_gateway)
    # Do not close it because the feedback service immediately sends
    # something that we don't want to loose.
except:
    logging.error("%s: Cannot connect to feedback service: %s" %
        (CONFIGFILE, e))
    logging.error("%s")
    sys.exit(1)

#
# Creation ZMQ sockets early so we don't waste other resource if it fails.
#
logging.info("Notifications ZMQ REP socket bound on tcp://%s" %
    zmq_bind)
try:
    zmqctx_r = zmq.Context()
    zmqsock = zmqctx_r.socket(zmq.REP)
    zmqsock.bind("tcp://%s" % zmq_bind)
except zmq.core.error.ZMQError as e:
    print e
    sys.exit(2)

#
# Create persistent queues for notifications and feedback.
#
apnsq = PersistentQueue(sqlitedb, 'notifications')
feedbackq = PersistentQueue(sqlitedb, 'feedback')
logging.info("%d notifications retrieved from persistent storage" %
    apnsq.qsize())
logging.info("%d feedbacks retrieved from persistent storage" %
    feedbackq.qsize())

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
for i in range(apns_concurrency):
    t = APNSAgent(apnsq, apns_gateway, apns_max_notif_lag, apns_max_error_wait,
        feedbackq)
    t.start()

t = FeedbackAgent(feedbackq, feedback_sock, feedback_gateway, feedback_freq)
t.start()

#
# Start Listener thread.
#
t = Listener(sqlitedb, zmqsock, apnsq, feedbackq)
t.run()
