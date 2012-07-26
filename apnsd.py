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
import logging
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
PAYLOADMAXLEN = 256
MAXTRIAL = 2
RESPONSEWAIT = 0.5

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
    @classmethod
    def configure(c, cacerts, cert, key):
        """
        Configures the CA, certificate and key files to be used when
        creating a connection.
        """
        c.cacerts = cacerts
        c.cert = cert
        c.key = key

    @classmethod
    def connect(c, peer):
        """
        Creates an SSL socket to the given `peer', which is a tuple
        (host, port).
        """
        assert c.cacerts is not None
        ai = socket.getaddrinfo(peer[0], peer[1], 0, 0, socket.IPPROTO_TCP)
        s = socket.socket(ai[0][0], ai[0][1], ai[0][2])
        sslsock = ssl.wrap_socket(s, keyfile=c.key, certfile=c.cert,
            server_side=False, cert_reqs=ssl.CERT_REQUIRED, ca_certs=cacerts)
        sslsock.connect(ai[0][4])
        return sslsock


class APNSAgent(threading.Thread):

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

    def __init__(self, queue, gateway, maxlag):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gateway = gateway
        self.maxlag = maxlag
        self.sock = None

    def _connect(self):
        try:
            self.sock = TLSConnectionMaker.connect(self.gateway)
        except Exception as e:
            logging.error("Couldn't connect to APNS (%s:%d): %s" %
                (self.gateway[0], self.gateway[1], e))
            sys.exit(3)
        self.sock.settimeout(0)

    def run(self):
        lastident = None
        while True:
            ident, curtime, devtok, payload = self.queue.get()
            bintok = base64.standard_b64decode(devtok)

            # Check notification lag.
            lag = now() - curtime
            if lag > self.maxlag:
                logging.info("Discarding notification #%d to %s: " \
                    "delayed by %us (max %us)" %
                    (ident, devtokfmt(bintok), lag, self.maxlag))
                time.sleep(random.randint(1, 3))    # DEVEL
                continue

            # Build the binary message.
            logging.debug("Sending notification #%d to %s, " \
                "lagging by %us: %s" %
                (ident, devtokfmt(bintok), lag, payload))
            fmt = '> B II' + 'H' + str(len(bintok)) + 's' + \
                'H' + str(len(payload)) + 's'
            binmsg = struct.pack(fmt, 1, ident, now(), len(bintok), bintok,
                len(payload), payload)
            hexdump(binmsg)
            
            # Now send it.
            if self.sock is None:
                self._connect()
            else:
                # Receive a possible error in the preceeding message.
                triple = select.select([self.sock], [], [], RESPONSEWAIT)
                if len(triple[0]) > 0:
                    buf = self.sock.recv()
                    if len(buf) != struct.calcsize('>BBI'):
                        logging.debug("Unexpected APNS error response size: %d (!= %d)" %
                            (len(buf), struct.calcsize('>BBI')))
                    # Bad...
                    cmd, st, errident = struct.unpack('>BBI', buf)
                    idmismatch = ''
                    if lastident != errident:
                        idmismatch = ' (identifier mismatch: #%d)' % errident
                    logging.warning("Notification #%d to %s response: " \
                        "%s%s" % (lastident, devtokfmt(bintok),
                         APNSAgent._error_responses[st], idmismatch))
                    self.sock.close()       # Yes, close abruptly.
                    self._connect()

            trial = 0
            while trial < MAXTRIAL:
                try:
                    self.sock.sendall(binmsg)
                    lastident = ident
                    break
                except socket.error as e:
                    trial = trial + 1
                    logging.info("Failed attempt %d to send notification "
                        "#%d to %s: %s" %
                        (trial, ident, devtokfmt(bintok), e))
                    self.sock.shutdown(socket.SHUT_RDWR)
                    self.sock.close()
                    self._connect()
                    continue
            if trial == MAXTRIAL:
                logging.warning("Couldn't send notification #%d to %s, "
                    "abording" % (ident, devtokfmt(bintok)))
                continue


class FeedbackAgent(threading.Thread):

    def __init__(self, queue, gateway, frequency):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gateway = gateway
        self.frequency = frequency
        self.queue.put((12345678890, "ABCDEF="))

    def run(self):
        # open socket in non-blocking mode
        while True:
            # while read buf
            #   break if EAGAIN
            #   (time, toklen, devtok) = struct.unpack("> I H 32s", but)
            #   self.queue.put((str(time), devtokfmt.format(devtok)))
            time.sleep(self.frequency)


class Listener(threading.Thread):

    whtsp = re.compile("\s+")

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
            l = re.split(Listener.whtsp, cmdargs, 1)
            ntok = int(l[0])
            l = re.split(Listener.whtsp, l[1], ntok)
            devtoks = l[0:ntok]
            payload = l[ntok]
        except IndexError as e:
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

        if len(payload) > PAYLOADMAXLEN:
            self._error("Payload too long (%d > %d)" % len(payload),
                PAYLOADMAXLEN, payload)
            return None

        return (devtoks, payload)

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
                devtoks, payload = res
                sqlcur.execute('UPDATE ident SET cur=?',
                    (curid + len(devtoks), ))
                #
                # Enqueue notifications.
                curtime = now()
                for devtok in devtoks:
                    self.apnsq.put((curid, curtime, devtok, payload))
                    curid = curid + 1
                self.zmqsock.send("OK I'll promise I'll do my best!")
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
                    self.zmqsock.send("OK 0")
                else:
                    self.zmqsock.send("OK " + str(len(feedbacks)) + " " +
                        ' '.join(feedbacks))
                continue

            self._error("Invalid input", msg)

#
# Get configuration.
#
CONFIGFILE = 'apnsd.conf'

cp = ConfigParser.SafeConfigParser()
l = cp.read([CONFIGFILE])
if len(l) == 0:
    raise Exception("Cannot open '%s'" % CONFIGFILE)

try:
    zmq_bind = cp.get('apnsd', 'zmq_bind')
    sqlitedb = cp.get('apnsd', 'sqlite_db')
    logfile = cp.get('apnsd', 'log_file')
    cacerts = cp.get('apnsd', 'cacerts_file')
    cert = cp.get('apnsd', 'cert_file')
    key = cp.get('apnsd', 'key_file')
    devtok_format = cp.get('apnsd', 'device_token_format')
    apns_gateway = cp.get('apns', 'gateway')
    apns_concurrency = int(cp.get('apns', 'concurrency'))
    apns_max_lag = int(cp.get('apns', 'max_lag'))
    feedback_gateway = cp.get('feedback', 'gateway')
    feedback_frequency = int(cp.get('feedback', 'frequency'))
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
TLSConnectionMaker.configure(cacerts, cert, key)
logging.info("Testing APNS gateway...")
try:
    l = apns_gateway.split(':', 2)
    apns_gateway = tuple(l)
    s = TLSConnectionMaker.connect(apns_gateway)
    s.close()
except Exception as e:
    logging.error("%s: Cannot connect to APNS: %s" % (CONFIGFILE, e))
    sys.exit(1)

logging.info("Testing feedback gateway...")
try:
    l = feedback_gateway.split(':', 2)
    feedback_gateway = tuple(l)
    s = TLSConnectionMaker.connect(feedback_gateway)
    s.close()
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
# Start APNS threads and Feedback one.
#
for i in range(apns_concurrency):
    t = APNSAgent(apnsq, apns_gateway, apns_max_lag)
    t.start()

t = FeedbackAgent(feedbackq, feedback_gateway, feedback_frequency)
t.start()

#
# Start Listener thread.
#
t = Listener(sqlitedb, zmqsock, apnsq, feedbackq)
t.run()
