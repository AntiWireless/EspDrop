#!/usr/bin/env bash
# EspDrop - receive AirDrop files with the patched OpenDrop over awdl0.
#
# Run tools/awdl_bridge.py first (it creates awdl0), then, as your normal user:
#
#   tools/opendrop_receive.sh                 # files land in ~/Downloads/airdrop
#   tools/opendrop_receive.sh -n MyPC -d /tmp/in
#
# On the iPhone: AirDrop "Everyone for 10 Minutes", unlock, open the share sheet
# and keep it open, pick the receiver name. Every transfer is accepted
# automatically while this runs (anyone in range can send you files).
#
# The script waits until awdl0 has a usable link-local address (OpenDrop binds
# its mDNS socket once at startup and never retries), and restarts OpenDrop when
# it exits or when the bridge recreates awdl0.
#
# Options:  -n NAME  receiver name shown on the iPhone (default: EspDrop)
#           -i IFACE interface (default: awdl0)
#           -d DIR   download directory (default: ~/Downloads/airdrop)
#           -l FILE  log file (default: ~/espdrop-opendrop.log)
# Environment: OPENDROP_VENV (default ~/opendrop-venv), OPENDROP_ANNOUNCE_INTERVAL
#           (default 2 s), ESPDROP_TLS_KEYLOG=FILE to write TLS keys for Wireshark.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

set -uo pipefail

NAME=EspDrop
IFACE=awdl0
DIR="$HOME/Downloads/airdrop"
LOG="$HOME/espdrop-opendrop.log"
VENV="${OPENDROP_VENV:-$HOME/opendrop-venv}"

while getopts "n:i:d:l:h" opt; do
  case "$opt" in
    n) NAME="$OPTARG" ;;
    i) IFACE="$OPTARG" ;;
    d) DIR="$OPTARG" ;;
    l) LOG="$OPTARG" ;;
    *) sed -n '2,24p' "$0"; exit 1 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && die "run as your normal user, not root (state lives in ~/.opendrop)"
[ -x "$VENV/bin/opendrop" ] || die "no OpenDrop in $VENV - run tools/opendrop_install.sh first"
case "$NAME" in *"'"*|*'"'*) die "the name must not contain quotes (it goes into the TLS certificate)" ;; esac
command -v ip >/dev/null || die "the 'ip' command (iproute2) is required"

# Our link-local on IFACE once usable (exists, not tentative), else empty.
usable_ll() {
  ip -6 -o addr show dev "$IFACE" scope link 2>/dev/null \
    | awk '!/tentative/ { for (i = 1; i <= NF; i++) if ($i == "inet6") { split($(i+1), a, "/"); print a[1]; exit } }'
}

wait_for_iface() {
  local said=0 ll
  while :; do
    ll=$(usable_ll)
    if [ -n "$ll" ]; then
      echo "$IFACE ready: $ll%$IFACE"
      return 0
    fi
    if [ $said -eq 0 ]; then
      echo "waiting for $IFACE (is 'sudo tools/awdl_bridge.py /dev/ttyACM0' running?) ..."
      said=1
    fi
    sleep 0.5
  done
}

preflight() {
  # OpenDrop advertises the first IPv6 address of the interface; it must be
  # the link-local, and no other interface may carry the same fe80:: address.
  local n
  n=$(ip -6 -o addr show dev "$IFACE" | wc -l)
  [ "$n" -gt 1 ] && echo "warning: $IFACE has $n IPv6 addresses; OpenDrop expects only the link-local" >&2
  if command -v systemctl >/dev/null && systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    echo "note: avahi-daemon is running and also speaks mDNS on $IFACE, costing scarce" >&2
    echo "      channel-6 airtime. For the test: sudo systemctl stop avahi-daemon avahi-daemon.socket" >&2
  fi
  if command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q '^Status: active'; then
    echo "note: ufw is active; allow tcp 8771-8772 and udp 5353 on $IFACE, e.g." >&2
    echo "      sudo ufw allow in on $IFACE to any port 8771:8772 proto tcp" >&2
    echo "      sudo ufw allow in on $IFACE to any port 5353 proto udp" >&2
  fi
}

mkdir -p "$DIR" || die "cannot create $DIR"
cd "$DIR" || die "cannot enter $DIR"

pid=""
# Background jobs of a script ignore Ctrl-C, so stop OpenDrop explicitly.
trap 'echo; [ -n "$pid" ] && kill "$pid" 2>/dev/null; echo "stopped."; exit 0' INT TERM

keylog=()
[ -n "${ESPDROP_TLS_KEYLOG:-}" ] && keylog=(SSLKEYLOGFILE="$ESPDROP_TLS_KEYLOG")

echo "receiving into $DIR as '$NAME' on $IFACE; log: $LOG"
while :; do
  wait_for_iface
  preflight
  ifindex_before=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo none)
  echo "--- $(date '+%F %T') starting opendrop" | tee -a "$LOG"
  # Process substitution (not a pipe) so that $! is OpenDrop itself.
  env PYTHONUNBUFFERED=1 \
      OPENDROP_ANNOUNCE_INTERVAL="${OPENDROP_ANNOUNCE_INTERVAL:-2}" \
      ${keylog[@]+"${keylog[@]}"} \
      "$VENV/bin/opendrop" -n "$NAME" -d -i "$IFACE" receive \
      > >(tee -a "$LOG") 2>&1 &
  pid=$!
  # Restart OpenDrop if the bridge recreated the interface: it binds only once.
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    now=$(cat "/sys/class/net/$IFACE/ifindex" 2>/dev/null || echo none)
    if [ "$now" != "$ifindex_before" ]; then
      echo "--- $IFACE changed (bridge restarted?), restarting opendrop" | tee -a "$LOG"
      kill "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid" 2>/dev/null
  pid=""
  echo "--- $(date '+%F %T') opendrop exited, restarting in 2 s" | tee -a "$LOG"
  sleep 2
done
