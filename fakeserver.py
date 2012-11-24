#!/usr/bin/env python

#
# SSL/TLS requires a server-side certificate.  Here is how to create one:
# - First create an openssl.conf file with the following content:
#   % [req]
#   % prompt = yes
#   % distinguished_name = dn
#   % 
#   % [dn]
#   % countryName="Country Name"
#   % countryName_default="FR"
#   % countryName_min=2
#   % countryName_max=2
#   % 
#   % organizationName="Organization"
#   % organizationName_default="Self-Signed"
#   % organizationName_min=2
#   % organizationName_max=32
#   % 
#   % commonName="Common Name"
#   % commonName_default="Test"
#   % commonName_min=2
#   % commonName_max=4
#
# - Then use the following commands:
#   $ openssl genrsa -des3 -out fake.key.crypted 1024
#   $ openssl req -new -key fake.key -out fake.csr -config openssl.conf
#   $ openssl rsa -in fake.key.crypted -out fake.key
#   $ openssl x509 -req -days 365 -in fake.csr -signkey fake.key -out fake.crt
#   $ rm fake.key.crypted fake.key.crypted

import BaseHTTPServer
import SocketServer
import json
import os
import select
import socket
import ssl
import sys
import threading

class SSLServer(SocketServer.TCPServer):
    def __init__(self, srvaddr, RequestHandlerClass, sslcert, sslkey):
        SocketServer.TCPServer.__init__(self, srvaddr, RequestHandlerClass)
        self.socket = ssl.wrap_socket(self.socket, server_side=True,
            certfile=sslcert, keyfile=sslkey, ssl_version=ssl.PROTOCOL_TLSv1)

class APNSRequestHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        size = 0
        while True:
            data = self.request.recv(1024)
            size += len(data)
            if len(data) != 0:
                continue
            print ("Client wrote %u bytes" % size)
            break

class GCMRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def do_POST(self):
        bodylen = int(self.headers.getheader('Content-length'))
        try:
            body = json.loads(self.rfile.read(bodylen))
        except:
            self.send_error(415, "Wrong JSON formatting")
            return
	print "Client sent %u notifications" % len(body['registration_ids'])
        resp = json.dumps({
            'multicast_id': 'stubby answer',
            'success': len(body['registration_ids']),
            'failure': 0,
            'canonical_ids': 0
        }) + "\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

SocketServer.ThreadingMixIn.daemon_threads = True
class ThreadedSSLServer(SocketServer.ThreadingMixIn, SSLServer):
    pass

tlist = []
if __name__ == "__main__":
    for arg in sys.argv[1:]:
        a = arg.split(":")
        if a[0] == "apns":
	    print "Starting %s..." % arg
            server = ThreadedSSLServer((a[1], int(a[2])),
              APNSRequestHandler, "fake.crt", "fake.key")
        elif a[0] == "gcm":
	    print "Starting %s..." % arg
            server = ThreadedSSLServer((a[1], int(a[2])),
              GCMRequestHandler, "fake.crt", "fake.key")
        else:
            print "Usage: %s <type:ip:port> [...]"
            print "Type: apns, gcm"
            sys.exit(1)
        t = threading.Thread(target=server.serve_forever)
	t.daemon = True
        tlist.append(t)
        t.start()

for t in tlist:
    while t.isAlive():
	t.join(1)
