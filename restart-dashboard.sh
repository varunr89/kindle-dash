#!/bin/sh
# Host-side helper: make sure exactly ONE dashboard loop is running on the Kindle.
#
# Why this exists: busybox `ps` truncates the command column, so `ps | grep dashboard.sh`
# never matches and a naive "is it already running?" check cheerfully starts a second
# copy. Two loops both painting the framebuffer means double flashes and double fetches.
# /proc/*/cmdline is the only honest way to count them.

KINDLE="${KINDLE:-kindle}"

ssh -o BatchMode=yes "$KINDLE" 'sh -s' <<'REMOTE'
count() {
    n=0
    for d in /proc/[0-9]*; do
        [ -r "$d/cmdline" ] || continue
        case "$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" in
            *dashboard.sh*) n=$((n + 1)) ;;
        esac
    done
    echo "$n"
}

echo "=== before ==="
echo "loops running: $(count)"

if [ "$(count)" -gt 0 ]; then
    for d in /proc/[0-9]*; do
        [ -r "$d/cmdline" ] || continue
        case "$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" in
            *dashboard.sh*) kill "$(basename "$d")" 2>/dev/null ;;
        esac
    done
    sleep 2
    echo "after kill: $(count)"
fi

echo "=== starting one ==="
nohup /mnt/us/documents/dashboard.sh >/dev/null 2>&1 &
sleep 4

echo "loops running: $(count)"
echo "--- conf in effect ---"
cat /mnt/us/dashboard.conf 2>/dev/null
echo "--- ssh listeners ---"
netstat -ltn 2>/dev/null | grep -E "2222|2223"
echo "--- last log lines ---"
tail -3 /mnt/us/dashboard.log 2>/dev/null | cut -c1-70
REMOTE
