#!/bin/sh
# One-tap network + capability probe.
# Tap "netinfo" in the Kindle library. It prints the essentials to the screen with
# fbink AND writes the full dump to /mnt/us/netinfo.log (readable over USB).
#
# Answers, from the device itself: which network it is on and its address, whether
# curl/wget exist, and whether KOReader's dropbear is up.

export PATH=/usr/local/bin:/bin:/usr/bin:/usr/sbin:/sbin:/mnt/us/libkh/bin
LOG=/mnt/us/netinfo.log

FBINK=""
for p in /mnt/us/libkh/bin/fbink /usr/bin/fbink /mnt/us/koreader/fbink; do
    [ -x "$p" ] && FBINK="$p" && break
done

{
  echo "=== ran ==="; date
  echo "=== id ==="; id
  echo "=== uname ==="; uname -a

  echo "=== interfaces ==="
  ifconfig -a 2>/dev/null || ip addr 2>/dev/null

  echo "=== routes ==="
  route -n 2>/dev/null || ip route 2>/dev/null

  echo "=== wifi via lipc ==="
  for p in cmState enable currentEssid; do
      printf '%-14s ' "$p"
      lipc-get-prop com.lab126.wifid "$p" 2>/dev/null || echo "(no prop)"
  done

  echo "=== resolv.conf ==="
  cat /etc/resolv.conf 2>/dev/null

  echo "=== fetchers / shells ==="
  for b in curl wget busybox nc telnet dbclient ssh scp dropbear; do
      printf '%-10s ' "$b"
      command -v "$b" 2>/dev/null || echo "-"
  done

  echo "=== fbink ==="
  ls -la /mnt/us/libkh/bin/fbink /usr/bin/fbink /mnt/us/koreader/fbink 2>/dev/null

  echo "=== processes of interest ==="
  ps 2>/dev/null | grep -iE "koreader|reader\.lua|dropbear|volumd" | grep -v grep

  echo "=== koreader ssh dir ==="
  ls -la /mnt/us/koreader/settings/SSH/ 2>/dev/null

  echo "=== reachability ==="
  ping -c1 -W3 192.168.68.1 2>&1 | tail -2
  ping -c1 -W3 1.1.1.1 2>&1 | tail -2

  echo "=== done ==="
} > "$LOG" 2>&1

# --- the same essentials, on screen -------------------------------------------
IP=""
for i in wlan0 wlan1 eth0 usb0; do
    a=$(ifconfig "$i" 2>/dev/null | awk '/inet /{print $2} /inet addr/{print $2}' | head -1 | sed 's/^addr://')
    [ -n "$a" ] && IP="$IP $i=$a"
done
[ -n "$IP" ] || IP=" (no address found)"

ESSID=$(lipc-get-prop com.lab126.wifid currentEssid 2>/dev/null)
STATE=$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null)
CURL=$(command -v curl 2>/dev/null || echo "-")
WGET=$(command -v wget 2>/dev/null || echo "-")

if [ -n "$FBINK" ]; then
    # -fk = flash + cls (grouped short options: the action flag goes last), then a
    # single invocation with multiple STRINGs, which FBInk prints on consecutive
    # lines. Do NOT pass -c here: it clears before *each* string.
    #
    # The framework repaints over anything drawn on the framebuffer, which is why a
    # one-shot print can flash and vanish before it is read. Redraw every few seconds
    # for a bounded window instead, then stop (self-terminating, touches nothing else).
    n=0
    while [ $n -lt 20 ]; do
        "$FBINK" -fk >/dev/null 2>&1
        "$FBINK" -y 1 \
            "netinfo.sh  $(date '+%H:%M:%S')  [$((20 - n))]" \
            "IP:$IP" \
            "ssid: $ESSID" \
            "wifi: $STATE" \
            "curl: $CURL" \
            "wget: $WGET" >/dev/null 2>&1
        n=$((n + 1))
        sleep 3
    done
fi
