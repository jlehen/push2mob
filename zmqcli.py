#!/usr/bin/env python

import zmq
import random
import time
import sys

context = zmq.Context()

# Socket with direct access to the sink: used to syncronize start of batch
sock = context.socket(zmq.REQ)
sock.connect(sys.argv[1])

try:
    while True:
	    _ = raw_input()
	    sock.send(_)
	    _ = sock.recv()
	    print _
except EOFError:
    pass
