#!/usr/bin/env python
# vim: ts=4:sw=4:et
#
# Copyright (C) 2012 Jeremie Le Hen <jeremie@le-hen>
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
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
import time
import sys
import zmq

def now():

        return int(time.time())

CONFIGFILE = 'apnsd.conf'

cp = ConfigParser.SafeConfigParser()
l = cp.read([CONFIGFILE])
if len(l) == 0:
    raise Exception("Cannot open '%s'" % CONFIGFILE)

try:
    listen = cp.get('apnsd', 'listen')
    concurrency = int(cp.get('apnsd', 'concurrency'))
except ConfigParser.Error as e:
    print "%s: %s" % (CONFIGFILE, e)
    sys.exit(1)

print ">>> Binding ZMQ PULL socket on tcp://%s..." % listen
try:
    zmqctx = zmq.Context()
    zmqrcv = zmqctx.socket(zmq.PULL)
    zmqrcv.bind("tcp://%s" % listen)
except zmq.core.error.ZMQError as e:
    print e
    sys.exit(2)

while True:
    msg = zmqrcv.recv()
    print msg
