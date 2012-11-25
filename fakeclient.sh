#!/bin/sh

SAMPLE=sample.json
ZMQCLI=${0%/*}/zmqcli.py

usage() {
	cat >&2 <<EOF
Usage: ${0##*/} <"apns"|"gcm"> <repeats> <count> <devtok_file> <zmq_sock>
Example:
  ${0##*/} apns 10 5000 apns_devtoks.list tcp://127.0.0.1:12195
EOF
	exit $?
}

case `uname -s` in
Linux) REP=seq ;;
SunOS) REP=seq ;;
*BSD) REP=jot ;;
esac

[ $# -ne 5 ] && usage 1

case "$1" in
apns) args="+12345" ;;
gcm) args="collapsekey +12345 delayidle" ;;
*) usage 1 ;;
esac
repeats=$2
count=$3
file=$4
zmqsock=$5

case "$file" in
*.gz) CAT="gzip -dc" ;;
*.bz2) CAT="bzip2 -dc" ;;
*.xz) CAT="xz -dc" ;;
*) CAT="cat" ;;
esac

set -e
lines=$($CAT $file | wc -l | sed 's,^ *,,; s, .*,,')

if [ $count -gt $lines ]; then
	echo "ERROR: $file: contains only $lines while you asked for $count" >&2
	exit 1
fi

for i in $($REP $repeats); do
	echo "send $args $count "$($CAT $file | head -n $count)" "$(cat $SAMPLE) | \
	    $ZMQCLI $zmqsock
done
