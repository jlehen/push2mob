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

    def __init__(self, queue):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue

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


#
# Get configuration.
#
CONFIGFILE = 'apnsd.conf'

cp = ConfigParser.SafeConfigParser()
l = cp.read([CONFIGFILE])
if len(l) == 0:
    raise Exception("Cannot open '%s'" % CONFIGFILE)

try:
    notifications_gateway = cp.get('apnsd', 'notifications_gateway')
    feedback_gateway = cp.get('apnsd', 'feedback_gateway')
    notifications_zmq_bind = cp.get('apnsd', 'notifications_zmq_bind')
    feedback_zmq_bind = cp.get('apnsd', 'feedback_zmq_bind')
    concurrency = int(cp.get('apnsd', 'concurrency'))
    sqlitedb = cp.get('apnsd', 'sqlitedb')
    logfile = cp.get('apnsd', 'logfile')
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
logging.info("Notifications ZMQ PULL socket bound on tcp://%s" %
    notifications_zmq_bind)
try:
    zmqctx_r = zmq.Context()
    zmqrcv = zmqctx_r.socket(zmq.PULL)
    zmqrcv.bind("tcp://%s" % notifications_zmq_bind)
except zmq.core.error.ZMQError as e:
    print e
    sys.exit(2)

logging.info("Feedback ZMQ PUSH socket bound on tcp://%s" %
    feedback_zmq_bind)
try:
    zmqctx_s = zmq.Context()
    zmqsnd = zmqctx_s.socket(zmq.PUSH)
    zmqsnd.bind("tcp://%s" % feedback_zmq_bind)
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
# Get current notification identifier.
#
sqlcon = sqlite3.connect(sqlitedb)
sqlcon.isolation_level = None
sqlcur = sqlcon.cursor()
sqlcur.execute('CREATE TABLE IF NOT EXISTS ident(cur INTEGER PRIMARY KEY);')
sqlcur.execute('SELECT cur FROM ident;')
row = sqlcur.fetchone()
if row is None:
    sqlcur.execute('INSERT INTO ident VALUES(0);')
    row = (0,)
curid = row[0]
logging.info("Notifications current identifier is %d" % curid)

#
# Start APNS threads.
#
for i in range(concurrency):
    t = APNSAgent(apnsq)
    t.start()

#
# Main job, receiving notifications orders.
#
whtsp = re.compile("\s+")
while True:
    msg = zmqrcv.recv()
    #
    # Parse line.
    msg = msg.strip()
    if msg[0:5].lower().find("send ") == -1:
        logging.warning("Invalid input: %s" % msg)
        continue
    cmdargs = msg[5:]
    try:
        l = re.split(whtsp, cmdargs, 1)
        ntok = int(l[0])
        l = re.split(whtsp, l[1], ntok)
        devtoks = l[0:ntok]
        payload = l[ntok]
    except IndexError as e:
        logging.warning("Invalid input: %s" % msg)
        continue
    #
    # Check device tokens.
    goodtoks = []
    for dt in devtoks:
        devtok = ''
        if len(dt) == 64:
            # Hexadecimal device token, convert it to base64.
            for i in range(0, 64, 2):
                c = dt[i:i+2]
                devtok = devtok + struct.pack('B', int(c, 16))
        else:
            # Maybe base64?
            try:
                devtok = base64.standard_b64decode(dt)
            except TypeError:
                logging.warning("Wrong base64 encoding for device " \
                    "token: %s" % dt)
                continue
        if len(devtok) != 32:
            logging.warning("Wrong device token length (%d != 32): %s" %
                (len(devtok), dt))
            continue
        # Store the token in base64 in the queue, text is better to debug.
        logging.debug("new devtok %s" % base64.standard_b64encode(devtok))
        goodtoks.append(base64.standard_b64encode(devtok))
    devtoks = goodtoks
    #
    # Enqueue notifications.
    sqlcur.execute('UPDATE ident SET cur=?', (curid + ntok, ))
    for devtok in devtoks:
        apnsq.put((curid, devtok, payload))
        curid = curid + 1
