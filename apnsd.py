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
import threading
import time
import sqlite3
import struct
import sys
import zmq

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


class APNSAgent(threading.Thread):

    def __init__(self, queue, gateway):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gateway = gateway

    def run(self):
        while True:
            ident, devtok, payload = self.queue.get()
            bintok = base64.standard_b64decode(devtok)
            asciitok = ''.join("%02x" % ord(c) for c in bintok)
            logging.debug("Sending notification #%d to %s (%s): %s" %
                (ident, devtok, asciitok, payload))
            fmt = '> B II' + 'H' + str(len(bintok)) + 's' + \
                'H' + str(len(payload)) + 's'
            binmsg = struct.pack(fmt, 1, ident, now(), len(bintok), bintok,
                len(payload), payload)
            hexdump(binmsg)
            time.sleep(random.randint(3, 9))


class FeedbackAgent(threading.Thread):

    def __init__(self, queue, gateway, frequency):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue
        self.gateway = gateway
        self.freqency = frequency

    def run(self):
        # open socket in non-blocking mode
        while True:
            # while read buf
            #   break if EAGAIN
            #   (time, toklen, devtok) = struct.unpack("> I H 32s", but)
            #   self.queue.put((time, base64.standard_b64encode(devtok)))
            time.sleep(frequency)


class Listener(threading.Thread):

    def __init__(self, sqlitedb, zmqsock, apnsq, feedbackq):
        threading.Thread.__init__(self)
        self.sqlitedb = sqlitedb
        self.zmqsock = zmqsock
        self.apnsq = apnsq
        self.feedbackq = feedbackq

    @staticmethod
    def error(msg, detail = None):
        if detail is not None:
            fmt = msg + ": %s"
            logging.warning(fmt, detail)
        else:
            logging.warning(msg)
        zmqsock.send("ERROR " + msg)

    def run(self):
        zmqsock = self.zmqsock
        apnsq = self.apnsq
        feedbackq = self.feedbackq

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

        whtsp = re.compile("\s+")
        while True:
            msg = self.zmqsock.recv()
            #
            # Parse line.
            msg = msg.strip()
            if msg[0:5].lower().find("send ") == -1:
                Listener.error("Invalid input", msg)
                continue
            cmdargs = msg[5:]
            try:
                l = re.split(whtsp, cmdargs, 1)
                ntok = int(l[0])
                l = re.split(whtsp, l[1], ntok)
                devtoks = l[0:ntok]
                payload = l[ntok]
            except IndexError as e:
                Listener.error("Invalid input", msg)
                continue
            #
            # Check device tokens.
            goodtoks = []
            wrongtok = 0
            for dt in devtoks:
                if wrongtok:
                        break
                devtok = ''
                if len(dt) == 64:
                    # Hexadecimal device token.
                    for i in range(0, 64, 2):
                        c = dt[i:i+2]
                        devtok = devtok + struct.pack('B', int(c, 16))
                else:
                    # Maybe base64?
                    try:
                        devtok = base64.standard_b64decode(dt)
                    except TypeError:
                        Listener.error("Wrong base64 encoding for device " \
                            "token: %s" % dt)
                        wrongtok = 1
                        continue
                if len(devtok) != 32:
                    Listener.error("Wrong device token length " \
                        "(%d != 32): %s" % (len(devtok), dt))
                    wrongtok = 1
                    continue
                # Store the token in base64 in the queue, text is better
                # to debug.
                logging.debug("new devtok %s" % \
                    base64.standard_b64encode(devtok))
                goodtoks.append(base64.standard_b64encode(devtok))
            devtoks = goodtoks
            if wrongtok:
                continue
            #
            #
            if len(payload) > 256:
                Listener.error("Payload too long (%d > 256)" % len(payload),
                    payload)
                continue
            #
            # Enqueue notifications.
            sqlcur.execute('UPDATE ident SET cur=?', (curid + ntok, ))
            for devtok in devtoks:
                apnsq.put((curid, devtok, payload))
                curid = curid + 1
            zmqsock.send("OK I'll promise I'll do my best!")


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
    sqlitedb = cp.get('apnsd', 'sqlitedb')
    logfile = cp.get('apnsd', 'logfile')
    apns_gateway = cp.get('apns', 'gateway')
    apns_concurrency = int(cp.get('apns', 'concurrency'))
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
# Start APNS threads.
#
for i in range(apns_concurrency):
    t = APNSAgent(apnsq)
    t.start()

#
# Start Listener thread.
#
t = Listener(sqlitedb, zmqsock, apnsq, feedbackq)
t.run()
