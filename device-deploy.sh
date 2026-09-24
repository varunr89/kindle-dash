#!/bin/sh
# Push the current device-side files to the Kindle and restart the loop.
#
# Needs the Kindle reachable as `ssh kindle` (see ~/.ssh/config). Run this after any change
# to dashboard.sh or the panel set. It is the only step that requires touching the device:
# after this, dashboard.sh re-reads dash.conf from the repo every cycle, so the frame list,
# the panel order and the interval can all change from the publishing side alone.
set -eu
cd "$(dirname "$0")"

CONF=device-dashboard.conf
KEY=/mnt/us/documents/dashboard.sh

echo "=== reachability ==="
ssh -o BatchMode=yes -o ConnectTimeout=8 kindle 'echo ok; date "+%F %T %Z"; echo "epoch=$(date +%s)"'

echo "=== device clock vs this host (the slot wheel is clock-derived, so drift costs freshness) ==="
remote=$(ssh -o BatchMode=yes kindle 'date +%s')
local=$(date +%s)
echo "skew: $((remote - local))s"

echo "=== pushing dashboard.sh ==="
ssh -o BatchMode=yes kindle "cat > $KEY" < dashboard.sh
ssh -o BatchMode=yes kindle "chmod 755 $KEY; md5sum $KEY"
md5 -q dashboard.sh | sed 's/^/local md5: /'

echo "=== pushing dashboard.conf (initial; dash.conf in the repo takes over from here) ==="
ssh -o BatchMode=yes kindle 'cat > /mnt/us/dashboard.conf' < "$CONF"
ssh -o BatchMode=yes kindle 'cat /mnt/us/dashboard.conf | head -3'

echo "=== restarting exactly one loop ==="
sh restart-dashboard.sh

echo "=== first draw ==="
ssh -o BatchMode=yes kindle '/mnt/us/documents/dashboard.sh --once' 2>&1 | tail -1
ssh -o BatchMode=yes kindle 'ls -l /tmp/dash.png | awk "{print \$5\" bytes\"}"; grep -c . /mnt/us/dashboard.log'
