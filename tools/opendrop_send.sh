#!/usr/bin/env bash
# EspDrop - send a file to an iPhone with the patched OpenDrop over awdl0.
#
# Run tools/awdl_bridge.py first (it creates awdl0 and, on the C5, follows the
# iPhone onto its 5 GHz channel). Then, as your normal user:
#
#   tools/opendrop_send.sh photo.jpg
#   tools/opendrop_send.sh -n MyPC file1.pdf file2.png
#   tools/opendrop_send.sh -r "Anna's iPhone" clip.mov   # pick the receiver
#
# On the iPhone: AirDrop "Everyone for 10 Minutes", unlock, and LEAVE THE SHARE
# SHEET CLOSED (an open share sheet makes the phone a sender, which never
# advertises a receiver for us to find). This script broadcasts the BLE wake
# (tools/airdrop_wake.py) to bring the phone's AirDrop receiver up, browses for
# it on awdl0, and sends. The phone shows an Accept prompt; tap Accept.
#
# Options:  -n NAME    sender name shown on the iPhone (default: EspDrop)
#           -i IFACE   interface (default: awdl0)
#           -r RECV    receiver: its name, 12-hex id, or index (default: the
#                      first one found this run)
#           -t SECS    how long to browse for a receiver (default: 60)
#           --no-wake  do not broadcast the BLE wake (if something else does)
#           -h         this help
# Environment: OPENDROP_VENV (default ~/opendrop-venv), ESPDROP_TLS_KEYLOG=FILE.
# Contacts-only target: pass identifiers to the wake via
#   ESPDROP_WAKE_ARGS="--appleid you@icloud.com" tools/opendrop_send.sh file
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

set -uo pipefail

NAME=EspDrop
IFACE=awdl0
RECV=""
BROWSE=60
WAKE=1
VENV="${OPENDROP_VENV:-$HOME/opendrop-venv}"
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() { awk 'NR > 1 { if (/^# SPDX/) exit; sub(/^# ?/, ""); print }' "$0"; }

# getopts does not do long options; pull --no-wake out first.
ARGS=()
for a in "$@"; do
  case "$a" in
    --no-wake) WAKE=0 ;;
    --help) usage; exit 0 ;;
    *) ARGS+=("$a") ;;
  esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}

while getopts "n:i:r:t:h" opt; do
  case "$opt" in
    n) NAME="$OPTARG" ;;
    i) IFACE="$OPTARG" ;;
    r) RECV="$OPTARG" ;;
    t) BROWSE="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

die() { echo "error: $*" >&2; exit 1; }

[ "$#" -ge 1 ] || die "give at least one file to send (see -h)"
[ "$(id -u)" -eq 0 ] && die "run as your normal user, not root (state lives in ~/.opendrop)"
[ -x "$VENV/bin/opendrop" ] || die "no OpenDrop in $VENV - run tools/opendrop_install.sh first"
case "$NAME" in *"'"*|*'"'*) die "the name must not contain quotes (it goes into the TLS certificate)" ;; esac
command -v ip >/dev/null || die "the 'ip' command (iproute2) is required"

files=()
for f in "$@"; do
  [ -f "$f" ] || die "no such file: $f"
  files+=(-f "$f")
done

# Our link-local on IFACE once usable (exists, not tentative), else empty.
usable_ll() {
  ip -6 -o addr show dev "$IFACE" scope link 2>/dev/null \
    | awk '!/tentative/ { for (i = 1; i <= NF; i++) if ($i == "inet6") { split($(i+1), a, "/"); print a[1]; exit } }'
}
[ -n "$(usable_ll)" ] || die "$IFACE has no usable link-local; is 'sudo tools/awdl_bridge.py /dev/ttyACM0' running?"

report="$HOME/.opendrop/discover.last.json"
findlog="$(mktemp -t espdrop-find-XXXXXX.log)"
wake_pid=""
find_pid=""
# Ignore SIGPIPE so a closed stdout (| tee quit) cannot kill us mid-cleanup.
trap '' PIPE
# Stop `opendrop find` with SIGINT (so its finally: writes the report) and reap
# it, but never block: escalate to SIGKILL if it does not go within ~3 s.
stop_find() {
  [ -n "$find_pid" ] || return 0
  kill -INT "$find_pid" 2>/dev/null
  for _ in $(seq 1 30); do
    kill -0 "$find_pid" 2>/dev/null || break
    sleep 0.1
  done
  kill -KILL "$find_pid" 2>/dev/null
  wait "$find_pid" 2>/dev/null
  find_pid=""
}
cleanup() {
  stop_find
  [ -n "$wake_pid" ] && kill "$wake_pid" 2>/dev/null
  rm -f "$findlog"
}
trap 'cleanup; exit 130' INT TERM HUP
trap cleanup EXIT

keylog=()
[ -n "${ESPDROP_TLS_KEYLOG:-}" ] && { mkdir -p "$(dirname "$ESPDROP_TLS_KEYLOG")" && keylog=(SSLKEYLOGFILE="$ESPDROP_TLS_KEYLOG"); }

if [ "$WAKE" -eq 1 ]; then
  echo "broadcasting the AirDrop BLE wake (phone: AirDrop Everyone, share sheet CLOSED) ..."
  # shellcheck disable=SC2086
  "$HERE/airdrop_wake.py" ${ESPDROP_WAKE_ARGS:-} >/dev/null 2>&1 &
  wake_pid=$!
  sleep 1
  kill -0 "$wake_pid" 2>/dev/null || { wake_pid=""; echo "warning: BLE wake did not start (python3-dbus / python3-gi installed, bluetooth on?); browsing anyway" >&2; }
fi

echo "browsing for an AirDrop receiver on $IFACE (up to ${BROWSE}s) ..."
: > "$findlog"
env PYTHONUNBUFFERED=1 "$VENV/bin/opendrop" -n "$NAME" -i "$IFACE" find > "$findlog" 2>&1 &
find_pid=$!

# `opendrop find` never exits on its own; stop at the first receiver or timeout.
recv_id=""
for _ in $(seq 1 "$((BROWSE * 2))"); do
  kill -0 "$find_pid" 2>/dev/null || break
  recv_id=$(grep -oE 'Found[[:space:]]+index[[:space:]]+[0-9]+[[:space:]]+ID[[:space:]]+[0-9a-f]{12}' "$findlog" \
            | grep -oE '[0-9a-f]{12}$' | head -1)
  [ -n "$recv_id" ] && break
  sleep 0.5
done
stop_find   # SIGINT so its finally: writes the report, then bounded reap

grep -E 'Found[[:space:]]+index' "$findlog" | sed 's/^/  /' || true
if [ -z "$recv_id" ] && [ -z "$RECV" ]; then
  echo "no AirDrop receiver found." >&2
  echo "  - the iPhone must be on AirDrop -> Everyone with the share sheet CLOSED," >&2
  echo "    unlocked and awake, and in range on the channel awdl0 is parked on (check" >&2
  echo "    'node' in the bridge: auto=on and chan= the phone's 5 GHz channel)." >&2
  echo "  - the BLE wake needs the PC's bluetooth on; without python3-dbus/python3-gi" >&2
  echo "    it is skipped. Raise the browse time with -t if it is slow to appear." >&2
  exit 1
fi

target="${RECV:-$recv_id}"
echo "sending to '${target}' ..."
# send reads the receiver's address/port from $report, which find just wrote.
[ -s "$report" ] || die "no discovery report at $report (find wrote nothing)"
env PYTHONUNBUFFERED=1 ${keylog[@]+"${keylog[@]}"} \
    "$VENV/bin/opendrop" -n "$NAME" -i "$IFACE" send -r "$target" "${files[@]}"
rc=$?
[ "$rc" -eq 0 ] && echo "sent." || echo "send failed (exit $rc); tap Accept on the phone and retry, or raise -t." >&2
exit "$rc"
