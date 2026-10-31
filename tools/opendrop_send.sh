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
# On the iPhone/iPad: AirDrop "Everyone for 10 Minutes", unlock. It answers as
# a receiver while a BLE wake runs nearby (airdrop-mt7921 FINDINGS §27, §37;
# its share sheet need not be open, as later tests with the ESP's wake showed).
# This script broadcasts that wake, browses for the receiver on awdl0, and sends.
# The device shows an Accept prompt; tap Accept.
#
# The wake comes from the ESP's own Bluetooth when its firmware has it (bridge
# `wake`, reached through the bridge's control socket), else from the PC's
# Bluetooth (tools/airdrop_wake.py). --wake picks one explicitly.
#
# Options:  -n NAME    sender name shown on the iPhone (default: EspDrop)
#           -i IFACE   interface (default: awdl0)
#           -r RECV    receiver: its name, 12-hex id, or index (default: the
#                      first one found this run)
#           -t SECS    how long to browse for a receiver (default: 60)
#           --wake=M   BLE wake from: auto (default: esp if it can, else pc),
#                      esp, pc, or none (if something else broadcasts it)
#           --no-wake  the same as --wake=none
#           -h         this help
# Environment: OPENDROP_VENV (default ~/opendrop-venv), ESPDROP_TLS_KEYLOG=FILE,
#           ESPDROP_FIND_LOG (debug log of the browse, default ~/espdrop-find.log),
#           ESPDROP_WAKE (default for --wake).
# Contacts-only target: pass identifiers to the wake (either source) via
#   ESPDROP_WAKE_ARGS="--appleid you@icloud.com" tools/opendrop_send.sh file
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

set -uo pipefail

NAME=EspDrop
IFACE=awdl0
RECV=""
BROWSE=60
WAKE_MODE="${ESPDROP_WAKE:-auto}"
VENV="${OPENDROP_VENV:-$HOME/opendrop-venv}"
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() { awk 'NR > 1 { if (/^# SPDX/) exit; sub(/^# ?/, ""); print }' "$0"; }

# getopts does not do long options; pull --wake/--no-wake out first.
ARGS=()
for a in "$@"; do
  case "$a" in
    --no-wake) WAKE_MODE=none ;;
    --wake=*) WAKE_MODE="${a#--wake=}" ;;
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
case "$BROWSE" in ''|*[!0-9]*) die "-t needs a number of seconds" ;; esac

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

# The ESP's wake goes through the bridge's control socket (tools/espdrop_cmd.py).
esp_cmd() { "$HERE/espdrop_cmd.py" -i "$IFACE" "$@"; }
# shellcheck disable=SC2086
esp_wake() { esp_cmd ${ESPDROP_WAKE_ARGS:-} wake "$1" >/dev/null 2>&1; }
case "$WAKE_MODE" in
  auto) if esp_cmd wake >/dev/null 2>&1; then WAKE_MODE=esp; else WAKE_MODE=pc; fi ;;
  esp|pc|none) ;;
  *) die "--wake must be auto, esp, pc or none" ;;
esac

report="$HOME/.opendrop/discover.last.json"
# Kept after the run (overwritten each time) so a failed browse can be read.
findlog="${ESPDROP_FIND_LOG:-$HOME/espdrop-find.log}"
wake_pid=""
wake_esp=0
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
  # The ESP's wake also stops by itself when its time is up.
  [ "$wake_esp" -eq 1 ] && { esp_cmd wake off >/dev/null 2>&1; wake_esp=0; }
}
trap 'cleanup; exit 130' INT TERM HUP
trap cleanup EXIT

keylog=()
[ -n "${ESPDROP_TLS_KEYLOG:-}" ] && { mkdir -p "$(dirname "$ESPDROP_TLS_KEYLOG")" && keylog=(SSLKEYLOGFILE="$ESPDROP_TLS_KEYLOG"); }

echo "browsing for an AirDrop receiver on $IFACE (up to ${BROWSE}s) ..."
: > "$findlog"
# Browse FIRST, then wake: a device woken by the BLE advert announces itself
# once, unsolicited, and that announcement is lost if nobody is listening yet.
# -d: log every service seen and why a receiver was skipped.
env PYTHONUNBUFFERED=1 "$VENV/bin/opendrop" -n "$NAME" -d -i "$IFACE" find > "$findlog" 2>&1 &
find_pid=$!
sleep 2

# Long enough for the browse; re-armed below for Accept and the upload.
wake_secs=$((BROWSE + 60))
[ "$wake_secs" -gt 600 ] && wake_secs=600
case "$WAKE_MODE" in
  esp)
    echo "broadcasting the AirDrop BLE wake from the ESP (device: AirDrop Everyone, unlocked) ..."
    if esp_wake "$wake_secs"; then
      wake_esp=1
    else
      echo "warning: the ESP did not start the BLE wake ($(esp_cmd wake 2>&1)); browsing anyway" >&2
    fi ;;
  pc)
    echo "broadcasting the AirDrop BLE wake from the PC (device: AirDrop Everyone, unlocked) ..."
    # shellcheck disable=SC2086
    "$HERE/airdrop_wake.py" ${ESPDROP_WAKE_ARGS:-} >/dev/null 2>&1 &
    wake_pid=$!
    sleep 1
    kill -0 "$wake_pid" 2>/dev/null || { wake_pid=""; echo "warning: BLE wake did not start (python3-dbus / python3-gi installed, bluetooth on?); browsing anyway" >&2; } ;;
esac

# `opendrop find` never exits on its own; stop at the first receiver or timeout.
recv_id=""
for _ in $(seq 1 "$((BROWSE * 2))"); do
  kill -0 "$find_pid" 2>/dev/null || break
  recv_id=$(grep -oE 'Found[[:space:]]+index[[:space:]]+[0-9]+[[:space:]]+ID[[:space:]]+[0-9a-f]{12}' "$findlog" \
            | grep -oE '[0-9a-f]{12}$' | head -1)
  [ -n "$recv_id" ] && break
  sleep 0.5
done
# mDNS found the device but /Discover is still pending: record how far its TCP
# got before stop_find closes the socket (OpenDrop's connect has no timeout).
tcp_state=""
neigh_state=""
if [ -z "$recv_id" ] && grep -q 'AirDrop service found' "$findlog"; then
  # "AirDrop service found: <host>, <addr>:<port>, ID ..."
  peer=$(sed -nE 's/.*AirDrop service found: [^,]*, ([0-9a-fA-F:.]+):([0-9]+),.*/\1 \2/p' "$findlog" | tail -n 1)
  if [ -n "$peer" ]; then
    command -v ss >/dev/null && tcp_state=$(ss -tnoH "( dport = :${peer#* } )" 2>/dev/null)
    neigh_state=$(ip -6 neigh show dev "$IFACE" "${peer% *}" 2>/dev/null)
  fi
fi
stop_find   # SIGINT so its finally: writes the report, then bounded reap

grep -E 'Found[[:space:]]+index' "$findlog" | sed 's/^/  /' || true
if [ -z "$recv_id" ] && [ -z "$RECV" ]; then
  echo "no AirDrop receiver found. What the browse saw (full log: $findlog):" >&2
  grep -iE 'discover|airdrop|receiver|service|resolv|error|exception|refused|timed out' "$findlog" \
    | grep -v 'Save discovery results' | tail -n 15 | sed 's/^/    /' >&2
  # OpenDrop: mDNS gives "AirDrop service found", then an HTTPS POST /Discover
  # to the device; a timeout there is also logged as "not discoverable".
  if grep -qi 'not discoverable' "$findlog"; then
    echo "  -> a device was found but /Discover gave no name: it is on Contacts Only," >&2
    echo "     or the HTTPS request to it timed out (see the TCP check below)." >&2
  elif grep -q 'AirDrop service found' "$findlog"; then
    echo "  -> a device was found via mDNS, but its /Discover (HTTPS to the device over" >&2
    echo "     $IFACE) never answered." >&2
    echo "     neighbour entry: ${neigh_state:-none}" >&2
    echo "     its TCP connection when we gave up:" >&2
    printf '%s\n' "${tcp_state:-none (refused, failed or already closed)}" | sed 's/^/       /' >&2
    case "$tcp_state" in
      *SYN-SENT*) echo "     SYN-SENT: the device never answered our SYN - our frames do not reach" >&2
                  echo "     it, or its answers do not reach us (bridge 'tx': uc_noack?)." >&2 ;;
      *ESTAB*)    echo "     ESTAB: TCP is up; the TLS handshake or the /Discover reply stalls." >&2 ;;
    esac
    echo "     Watch it with  sudo tcpdump -i $IFACE -n 'tcp port 8770 or icmp6'" >&2
  fi
  echo "  - the iPhone/iPad must be on AirDrop -> Everyone, unlocked, and in range on" >&2
  echo "    the channel awdl0 is parked on (check 'node' in the bridge: auto=on and" >&2
  echo "    chan= the device's 5 GHz channel)." >&2
  echo "  - 'Everyone for 10 Minutes' falls back to Contacts Only after 10 minutes;" >&2
  echo "    select it again right before sending." >&2
  echo "  - the BLE wake (this run: $WAKE_MODE) comes from the ESP's Bluetooth (bridge" >&2
  echo "    'wake') or the PC's (bluetooth on, python3-dbus/python3-gi); try the other" >&2
  echo "    with --wake=pc / --wake=esp. Raise the browse time with -t if it is slow." >&2
  exit 1
fi

target="${RECV:-$recv_id}"
echo "sending to '${target}' ..."
# send reads the receiver's address/port from $report, which find just wrote.
[ -s "$report" ] || die "no discovery report at $report (find wrote nothing)"
# Keep the ESP's wake up through the Accept prompt and the upload.
[ "$wake_esp" -eq 1 ] && esp_wake 300
env PYTHONUNBUFFERED=1 ${keylog[@]+"${keylog[@]}"} \
    "$VENV/bin/opendrop" -n "$NAME" -i "$IFACE" send -r "$target" "${files[@]}"
rc=$?
[ "$rc" -eq 0 ] && echo "sent." || echo "send failed (exit $rc); tap Accept on the phone and retry, or raise -t." >&2
exit "$rc"
