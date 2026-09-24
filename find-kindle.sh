#!/bin/sh
# Find the Kindle's SSH server (KOReader dropbear on 2222) on a subnet.
# Usage: find-kindle.sh 192.168.68    (or 192.168.0)  [via-mini]
#
# Why this exists: macOS 27's `arp -an` returns nothing on these hosts, so ARP-table
# discovery does not work. Scan the port directly instead.
#
# With `via-mini`, the sweep runs on the Mac mini (which sits on the 192.168.68.x
# network); use that when this host is on a different subnet.

S="${1:?subnet prefix, e.g. 192.168.68}"
VIA="$2"

scan='
S="'"$S"'"
i=1
while [ $i -le 254 ]; do
    ( nc -z -G1 "$S.$i" 2222 2>/dev/null && echo "SSH-OPEN $S.$i" ) &
    i=$((i + 1))
done
wait
'

if [ "$VIA" = "via-mini" ]; then
    ssh varunramesh@100.93.195.91 "$scan" | sort -u
else
    sh -c "$scan" | sort -u
fi
echo "scan complete: $S.0/24"
