#!/bin/sh
# Undo dashboard.sh: stop the loop and give the Kindle its UI back.
#
# Needs a shell on the device (KOReader's SSH plugin on port 2222, KOReader's
# terminal plugin, or kterm). The touchscreen will not do it, because the dashboard
# hides the UI.

pkill -f "dashboard.sh" 2>/dev/null
killall dashboard.sh 2>/dev/null
sleep 1

# Stop the loop's own SSH server (the one dashboard.sh started on 2223).
if [ -f /tmp/dropbear_dash.pid ]; then
    kill "$(cat /tmp/dropbear_dash.pid 2>/dev/null)" 2>/dev/null
    rm -f /tmp/dropbear_dash.pid
fi

# Re-show the UI and re-arm the screensaver (dashboard.sh turned both off).
lipc-set-prop com.lab126.pillow disableEnablePillow enable 2>/dev/null
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null

# If the framework was stopped rather than merely hidden, bring it back (upstart).
initctl start lab126_gui 2>/dev/null || start lab126_gui 2>/dev/null

# Force a repaint so the last dashboard frame does not linger.
for p in /mnt/us/libkh/bin/fbink /usr/bin/fbink /mnt/us/koreader/fbink; do
    [ -x "$p" ] && "$p" -s >>/mnt/us/dashboard.log 2>&1 && break
done

echo "$(date '+%F %T') restore.sh ran" >> /mnt/us/dashboard.log
exit 0