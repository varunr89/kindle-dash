#!/bin/sh
# Kindle e-ink dashboard - the device side of an always-on panel.
#
# Install: copy to /mnt/us/documents/ (over SSH once a shell exists, or via USB).
# hdnext's sh_integration makes any .sh there a tappable library entry, so the panel
# starts by tapping what the Kindle thinks is a book.
#
# The loop is deliberately dumb: one HTTP GET, one framebuffer write, sleep. Every
# decision about what to show happens on the machine that renders the PNG.
#
# BACK OUT: run restore.sh (over SSH or from KOReader's terminal). This hides the
# Amazon UI, so the touchscreen is not a way back - get a shell working first.

# Overridable without editing this file: drop /mnt/us/dashboard.conf on the device
# containing e.g.  DASH_URL=http://host:8791/dash.png
[ -f /mnt/us/dashboard.conf ] && . /mnt/us/dashboard.conf

# sh_integration's tap environment is minimal; be explicit rather than lucky.
export PATH=/usr/local/bin:/bin:/usr/bin:/usr/sbin:/sbin:/mnt/us/libkh/bin

URL_DEFAULT="${DASH_URL:-http://192.168.68.113:8791/dash.png}"
# DASH_URLS: space-separated frame URLs in the publisher's PANELS order. The loop derives
# which one is current from the clock - the same wheel the publisher walks - so the two
# stay in step across reboots and missed cycles without the device keeping state. Each URL
# is therefore fetched once per full cycle, and because a cycle is longer than the CDN's
# 300s max-age every fetch is a cache miss: fresh frames, no token on the device.
URLS="${DASH_URLS:-}"
URL="$URL_DEFAULT"
INTERVAL="${DASH_INTERVAL:-60}"    # seconds between refreshes
CLEAR_EVERY=12                     # full clear every N frames to kill e-ink ghosting
IMG=/tmp/dash.png
LOG=/mnt/us/dashboard.log

FBINK=""
for p in /mnt/us/libkh/bin/fbink /usr/bin/fbink /mnt/us/koreader/fbink; do
    [ -x "$p" ] && FBINK="$p" && break
done
[ -n "$FBINK" ] || { echo "$(date '+%F %T') no fbink found" >> "$LOG"; exit 1; }

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# --- durable SSH --------------------------------------------------------------
# KOReader's SSH server (port 2222) exists only while KOReader runs, but this loop
# hides the UI and is meant to outlive it - so keep a second instance that the loop
# owns. A different port so the two never fight over the socket. Key-only auth, with
# the host keys KOReader already generated under settings/SSH/ (dropbear resolves
# those relative to its cwd, hence the cd).
SSH_PORT="${DASH_SSH_PORT:-2223}"
SSH_PID=/tmp/dropbear_dash.pid
DROPBEAR=/mnt/us/koreader/dropbear

ensure_ssh() {
    [ -x "$DROPBEAR" ] || return 0
    if [ -f "$SSH_PID" ] && kill -0 "$(cat "$SSH_PID" 2>/dev/null)" 2>/dev/null; then
        return 0
    fi
    # KOReader's plugin punches this hole for its own port; ours needs its own rule.
    iptables -A INPUT -p tcp --dport "$SSH_PORT" -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT 2>/dev/null
    ( cd /mnt/us/koreader && ./dropbear -E -R -p "$SSH_PORT" -P "$SSH_PID" -s >/dev/null 2>&1 )
    log "ssh started on port $SSH_PORT"
}

wifi_up() {
    state=$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null)
    case "$state" in
        CONNECTED*) : ;;
        *) lipc-set-prop com.lab126.wifid enable 1 >/dev/null 2>&1; sleep 5 ;;
    esac
}

# Self-updating config. The publisher's conf lives next to the frames in the repo; the
# loop adopts URLS/INTERVAL from it when it changes, so the URL list, the interval and the
# panel set can all change without ever touching this device again. Only the two known
# keys are read - the fetched file is never sourced, so a tampered conf cannot execute.
CONF_URL="${DASH_CONF_URL:-}"
CONF_CACHE=/tmp/dash.conf.cur

reload_conf() {
    [ -n "$CONF_URL" ] || return 0
    command -v curl >/dev/null 2>&1 || return 0
    curl -fsS --connect-timeout 8 --max-time 20 "$CONF_URL" -o /tmp/dash.conf.new 2>/dev/null || return 0
    [ -s /tmp/dash.conf.new ] || return 0
    new_urls=$(sed -n 's/^DASH_URLS=//p' /tmp/dash.conf.new | head -1 | sed 's/^"//; s/"$//')
    [ -n "$new_urls" ] || return 0
    new_iv=$(sed -n 's/^DASH_INTERVAL=\([0-9][0-9]*\).*/\1/p' /tmp/dash.conf.new | head -1)
    if [ "$new_urls" = "$URLS" ] && [ "${new_iv:-$INTERVAL}" = "$INTERVAL" ]; then
        return 0
    fi
    cp /tmp/dash.conf.new "$CONF_CACHE"
    URLS="$new_urls"
    [ -n "$new_iv" ] && INTERVAL="$new_iv"
    log "conf adopted: interval=${INTERVAL}s panels=$(printf '%s' "$URLS" | wc -w)"
}

pick_url() {
    [ -n "$URLS" ] || { URL="$URL_DEFAULT"; return 0; }
    n=0
    for u in $URLS; do n=$((n + 1)); done
    [ "$n" -gt 0 ] || { URL="$URL_DEFAULT"; return 0; }
    idx=$(( ($(date +%s) / INTERVAL) % n ))
    i=0
    for u in $URLS; do
        if [ "$i" = "$idx" ]; then URL="$u"; return 0; fi
        i=$((i + 1))
    done
    URL="$URL_DEFAULT"
}

fetch() {
    pick_url
    rm -f "$IMG.tmp"
    # Cache-buster. Note this does NOT help against raw.githubusercontent.com: measured,
    # that CDN serves `max-age=300` by path and ignores both the query string and a
    # client Cache-Control: no-cache, so ~5 min is a hard floor on frame propagation.
    # Kept because it is free and does work on hosts that honour query strings.
    case "$URL" in
        *\?*) u="$URL&cb=$(date +%s)" ;;
        *)    u="$URL?cb=$(date +%s)" ;;
    esac
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --connect-timeout 10 --max-time 30 "$u" -o "$IMG.tmp" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 30 -O "$IMG.tmp" "$u" 2>/dev/null
    else
        return 1
    fi
}

# Test a single cycle without taking over the screen or hiding the UI:
#   dashboard.sh --once
if [ "$1" = "--once" ]; then
    wifi_up
    reload_conf
    if fetch && [ -s "$IMG.tmp" ]; then
        mv "$IMG.tmp" "$IMG"
        "$FBINK" -g file="$IMG" -W GC16
        echo "drew one frame"
        exit 0
    fi
    echo "fetch failed"
    exit 1
fi

# Keep the panel on and the radio up. Both are restored by restore.sh. Placed after
# the --once branch so a one-shot test can never hide the UI.
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
lipc-set-prop com.lab126.pillow disableEnablePillow disable 2>/dev/null

# Take over from any earlier loop. A tap in the library should mean "start the panel", never
# "start a second copy painting the same framebuffer" - which is what the old ps-based guard
# allowed, because busybox ps truncates the command column and the grep never matched.
# Busybox has no pgrep -f, so walk /proc/<pid>/cmdline and kill only after collecting, so
# this process's own short-lived subshells are not caught in the sweep.
PIDFILE=/tmp/dashboard.pid
legacy=""
for f in /proc/[0-9]*/cmdline; do
    p=${f#/proc/}; p=${p%/cmdline}
    [ "$p" = "$$" ] && continue
    case "$(cat "$f" 2>/dev/null | tr '\0' ' ')" in
        *dashboard.sh*) legacy="$legacy $p" ;;
    esac
done
for p in $legacy; do
    log "retiring previous loop pid $p"
    kill "$p" 2>/dev/null
done
[ -n "$legacy" ] && sleep 2
echo $$ > "$PIDFILE"

ensure_ssh

failures=0
i=0
while true; do
    wifi_up
    ensure_ssh
    reload_conf
    if fetch && [ -s "$IMG.tmp" ]; then
        mv "$IMG.tmp" "$IMG"          # only swap in a frame that arrived whole
        failures=0
        i=$((i + 1))
        if [ $((i % CLEAR_EVERY)) -eq 1 ]; then
            "$FBINK" -f -c >>"$LOG" 2>&1            # flash + clear: wipes ghosting
        fi
        "$FBINK" -g file="$IMG" -W GC16 >>"$LOG" 2>&1   # GC16 = grayscale image waveform
    else
        failures=$((failures + 1))
        log "fetch failed (${failures} in a row) ${URL##*/}"
        # A dropped radio is the usual cause, so nudge it after a few misses.
        [ "$failures" -ge 3 ] && lipc-set-prop com.lab126.wifid enable 1 >/dev/null 2>&1
    fi
    # Wake just after a slot boundary rather than on a drifting 60s cadence. The publisher
    # renders the current slot during the first seconds of each minute, so reading at :15
    # gets this minute's frame instead of the previous cycle's. Holds for any INTERVAL that
    # divides the hour (60, 120, 300, ...).
    now=$(date +%s)
    sleep_for=$(( (now / INTERVAL + 1) * INTERVAL + 15 - now ))
    [ "$sleep_for" -le 0 ] && sleep_for="$INTERVAL"
    sleep "$sleep_for"
done