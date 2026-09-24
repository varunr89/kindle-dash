#!/bin/sh
# Kindle dashboard publisher - the STABLE launcher.
#
# This file is deliberately NOT part of the code checkout. The checkout is replaced from
# GitHub once a minute, and a shell reads its own script incrementally, so replacing a
# script that is mid-loop can feed the interpreter a mix of old and new text. The loop
# lives here; every line of rendering logic it calls is fetched fresh from the `code`
# branch. After editing this file in the repo it has to be re-installed by hand:
#
#     cp ~/projects/kindle-dash/deploy/kindle-dash-loop.sh ~/bin/ && chmod +x ~/bin/kindle-dash-loop.sh
#
# Per minute: pull the code (fetch failures are survivable), publish the current slot on the
# minute boundary, and only after that do the heavier full refresh if the code moved. The
# order matters - a full refresh takes ~30s, so doing it before the slot publish would push
# that minute's frame past the moment the device reads it.
set -u

DIR="$HOME/projects/kindle-dash"          # a git checkout of the `code` branch
ENV_FILE="$HOME/.hermes/kindle-dash.env"
LOG="$DIR/publish.log"
BRANCH="${GH_CODE_BRANCH:-code}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# set -a so the sourced vars are exported to python: `.` sets shell variables but does NOT
# export them, and publish.py reads them from the environment.
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

log() { printf '%s %s\n' "$(date '+%F %T')" "$1" >> "$LOG"; }

trim_log() {
    if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
        tail -n 600 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

cd "$DIR" || { echo "no $DIR"; exit 1; }

# Warn when the installed launcher has drifted from the copy in the repo: this one never
# updates itself, so the repo copy is the record of what it should be.
if [ -f deploy/kindle-dash-loop.sh ] && [ -f "$0" ] && ! cmp -s deploy/kindle-dash-loop.sh "$0"; then
    log "NOTE: installed launcher differs from deploy/kindle-dash-loop.sh - re-install it by hand"
fi

pull_code() {
    # True when the checkout moved. A failed fetch (offline, GitHub down, rate limited) is
    # not fatal: keep rendering the code already on disk rather than stopping the panel.
    before=$(git rev-parse HEAD 2>/dev/null || echo none)
    if ! git fetch --quiet origin "$BRANCH" 2>>"$LOG"; then
        log "WARN: fetch failed, rendering the revision already on disk ($before)"
        return 1
    fi
    if ! git checkout --quiet --force -B "$BRANCH" FETCH_HEAD 2>>"$LOG"; then
        log "WARN: checkout of $BRANCH failed, still on $before"
        return 1
    fi
    after=$(git rev-parse HEAD 2>/dev/null || echo none)
    [ "$before" != "$after" ] || return 1
    log "code updated $before -> $after"
    return 0
}

full_refresh() {
    # A panel added or edited on GitHub should be on the wall now, not one slot per minute.
    out=$(python3 render_panels.py --publish-all 2>&1)
    rc=$?
    log "refresh rc=$rc $(echo "$out" | tr '\n' ' ')"
}

sync_conf() {
    # dash.conf is derived from PANELS, so a panel added on GitHub also needs its URL in the
    # device config. Pushed only when the content actually changes.
    new=$(python3 render_panels.py --conf 2>/dev/null) || return 0
    [ "$new" = "$(cat dash.conf 2>/dev/null)" ] && return 0
    printf '%s\n' "$new" > dash.conf
    out=$(GH_PATH=dash.conf python3 publish.py dash.conf 2>&1)
    log "conf republished: $(echo "$out" | tr '\n' ' ')"
}

run_once() {
    started=$(date '+%s')
    out=$(python3 render_panels.py --publish-slot 2>&1)
    rc=$?
    log "rc=$rc $(( $(date '+%s') - started ))s $(echo "$out" | tr '\n' ' ')"
    trim_log
    return 0
}

if [ "${1:-}" = "--loop" ]; then
    log "loop started (pid $$)"
    while true; do
        now=$(date '+%s')
        # Wake on the exact minute boundary the slot index is derived from, so the device's
        # clock-derived wheel stays in step without any coordination between the machines.
        sleep_for=$(( 60 - now % 60 ))
        [ "$sleep_for" -le 0 ] && sleep_for=1
        sleep "$sleep_for"
        run_once
        if pull_code; then
            full_refresh
            sync_conf
        fi
    done
fi

# One-shot: pull, refresh if the code moved, then publish the current slot.
if pull_code; then
    full_refresh
    sync_conf
fi
run_once
trim_log
exit 0
