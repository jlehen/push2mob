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
#   $ openssl genrsa -des3 -out fakeapns.key.crypted 1024
#   $ openssl req -new -key fakeapns.key -out fakeapns.csr -config openssl.conf
#   $ openssl rsa -in fakeapns.key.crypted -out fakeapns.key
#   $ openssl x509 -req -days 365 -in fakeapns.csr -signkey fakeapns.key -out fakeapns.crt
#   $ rm fakeapns.key.crypted fakeapns.key.crypted

import os
import select
import socket
import ssl
import sys

port = 2195
ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
ssock.bind(("127.0.0.1", port))
ssock.listen(5)
print("Listening!")
clino = 0
readlist = [ssock]
fdinfo = {}		# Contains lists: [clino, bytesread, sslsock]
while True:
    (sread, swrite, sexc) =  select.select(readlist, [], []);

    for sock in sread:
        if sock == ssock:
            (csock, fromaddr) = ssock.accept()
	    try:
		sslsock = ssl.wrap_socket(csock, server_side=True,
		    certfile="fakeapns.crt", keyfile="fakeapns.key",
		    ssl_version=ssl.PROTOCOL_TLSv1)
	    except:
		csock.close()
		continue
            print("New client: %d" % clino)
            clino = clino + 1
	    # Non-blocking handshake.
	    while True:
		try:
		    sslsock.do_handshake()
		    break
		except ssl.SSLError as err:
		    if err.args[0] == ssl.SSL_ERROR_WANT_READ:
			select.select([s], [], [])
		    elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
			select.select([], [s], [])
		    else:
			csock.close()
			sslsock = None
			break
	    if sslsock is None:
		continue
	    readlist.append(csock)
	    fd = csock.fileno()
	    fdinfo[fd] = [clino, 0, sslsock]
            continue

	fd = sock.fileno()
	cliinfo = fdinfo[fd]
	sslsock = cliinfo[2]
	try:
	    while True:
		data = sslsock.recv(512)
		if len(data) == 0:
		    print ("Client %d wrote %u bytes" % (cliinfo[0], cliinfo[1]))
		    del fdinfo[fd]
		    readlist.remove(sock)
		    sslsock.close()
		    break
		cliinfo[1] = cliinfo[1] + len(data)
	except:
	    del fdinfo[fd]
	    readlist.remove(sock)
	    sock.close()
	    pass
