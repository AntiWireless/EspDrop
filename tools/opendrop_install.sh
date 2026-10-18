#!/usr/bin/env bash
# EspDrop - install the iOS-26-compatible patched OpenDrop into a virtualenv.
#
# Upstream OpenDrop (seemoo-lab) no longer works with iOS 26 on its own. The
# patch series from jedbillyb/airdrop-mt7921 (GPLv3) makes it work and is the
# reference we follow. This script reproduces only that project's "OpenDrop
# venv" step: PyPI opendrop 0.13.0 plus all 17 patches, in their fixed order.
# Nothing MT7921- or OWL-specific is done; EspDrop provides awdl0 instead.
#
#   tools/opendrop_install.sh            # as your normal user, not root
#
# Re-running is safe: an install that is complete and matches the pinned series
# is left alone (no network needed); one this script left incomplete (an error,
# Ctrl-C) is rebuilt from scratch; one built from another series or Python
# version is rebuilt too, and stays in place until the new one has worked.
#
# Settings (environment variables):
#   OPENDROP_VENV        target venv            (default: ~/opendrop-venv)
#   AIRDROP_MT7921_DIR   where the patches live (default: ~/.cache/espdrop/airdrop-mt7921)
#   AIRDROP_MT7921_REV   pinned patch revision  (default: 27edf28, the one we reviewed)
#
# System requirements (package names vary by distro): python3 (>= 3.8) with the
# venv module, git (patches are applied with `git apply`), openssl (OpenDrop
# makes its TLS certificate with it on first start) and libarchive.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

set -euo pipefail

VENV="${OPENDROP_VENV:-$HOME/opendrop-venv}"
SRC="${AIRDROP_MT7921_DIR:-$HOME/.cache/espdrop/airdrop-mt7921}"
REV="${AIRDROP_MT7921_REV:-27edf28}"
OPENDROP_VERSION=0.13.0
# Fixed series order from airdrop-mt7921 install.sh / patches/README.md
# ("a series, not a menu": each patch assumes all earlier ones are applied).
PATCHES=(ios26-airdrop recv-window py314-send mdns-repeat find-report tls-keylog
         upload-arms ask-confirm mdns-reannounce threaded-server url-items
         zeroconf-update-service salvage-truncated salvage-trim
         send-multifile send-status send-stall)
# Markers in the venv: WIP while this script builds it, OK (holding the series
# signature) once every patch landed and was verified.
STAMP_WIP=.espdrop-installing
STAMP_OK=.espdrop-installed

die()  { echo "error: $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] && die "run this as your normal user (OpenDrop keeps its state in ~/.opendrop)"

step "checking requirements"
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "python3 >= 3.8 required, found $(python3 -V 2>&1)"
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || die "python3 venv module missing (e.g. Debian/Ubuntu: apt install python3-venv)"
command -v git     >/dev/null || die "git not found (needed for git apply)"
command -v openssl >/dev/null || die "openssl not found (OpenDrop creates its certificate with it)"
if command -v ldconfig >/dev/null; then
  # Captured first: `ldconfig -p | grep -q` under pipefail can report a hit as a
  # miss when grep exits early and ldconfig dies of SIGPIPE.
  libs=$(ldconfig -p 2>/dev/null || true)
  case "$libs" in
    *libarchive.so*) ;;
    *) echo "warning: libarchive not found by ldconfig; OpenDrop needs it to unpack transfers" >&2 ;;
  esac
fi
pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "python: $(python3 -V 2>&1)"

# A venv this script made carries one of our markers (only ever written into a
# directory this script created itself) or, from the first version of the
# script, espdrop-freeze.txt next to pyvenv.cfg. Complete: no WIP marker left.
ours()     { [ -e "$1/$STAMP_OK" ] || [ -e "$1/$STAMP_WIP" ] \
               || { [ -f "$1/pyvenv.cfg" ] && [ -f "$1/espdrop-freeze.txt" ]; }; }
complete() { [ ! -e "$1/$STAMP_WIP" ] && { [ -e "$1/$STAMP_OK" ] || [ -f "$1/espdrop-freeze.txt" ]; }; }
# What a finished venv must match: OpenDrop and Python version, the patch
# revision ($1) and the series (names and order).
series_sig() { echo "opendrop $OPENDROP_VERSION python $pyver mt7921 $1 patches ${PATCHES[*]}"; }
up_to_date() { complete "$VENV" && [ -f "$VENV/$STAMP_OK" ] && [ "$(cat "$VENV/$STAMP_OK")" = "$1" ]; }

# A rebuild that was killed hard leaves the previous install aside; put it back.
OLD="$VENV.espdrop-old"
if [ -e "$OLD" ] && ours "$OLD" && complete "$OLD" \
   && { [ ! -e "$VENV" ] || { ours "$VENV" && ! complete "$VENV"; }; }; then
  rm -rf -- "$VENV"
  mv -- "$OLD" "$VENV"
fi

# A commit id pins the patches for good, so an up-to-date install is recognised
# without the network or the patch cache. A branch or tag is fetched each time.
case "$REV" in ''|*[!0-9a-f]*) fixed_rev=0 ;; *) fixed_rev=1 ;; esac
if [ $fixed_rev -eq 1 ] && up_to_date "$(series_sig "$REV")"; then
  echo; echo "already installed and up to date: $VENV"; exit 0
fi

step "fetching the patch series ($REV)"
if [ -d "$SRC/.git" ]; then
  if [ $fixed_rev -eq 0 ] || ! git -C "$SRC" cat-file -e "$REV^{commit}" 2>/dev/null; then
    git -C "$SRC" fetch -q --tags --force origin
  fi
else
  mkdir -p "$(dirname "$SRC")"
  git clone -q https://github.com/jedbillyb/airdrop-mt7921.git "$SRC"
fi
git -C "$SRC" -c advice.detachedHead=false checkout -q "$REV"
for p in "${PATCHES[@]}"; do
  [ -f "$SRC/patches/opendrop-$p.patch" ] || die "patch $p missing in $SRC at $REV"
done
if [ $fixed_rev -eq 1 ]; then
  sig=$(series_sig "$REV")
else
  rev_id=$(git -C "$SRC" rev-parse --short=12 HEAD)
  echo "$REV is $rev_id"
  sig=$(series_sig "$rev_id")
  if up_to_date "$sig"; then echo; echo "already installed and up to date: $VENV"; exit 0; fi
fi

# A working but outdated install stays aside until the new one is verified, and
# comes back if anything fails or is interrupted on the way. (Building the new
# one elsewhere and moving it would not work: a venv is not relocatable.)
restore=0
finish() {
  if [ "$restore" -eq 1 ]; then
    rm -rf -- "$VENV" && mv -- "$OLD" "$VENV" && echo "kept the previous install in $VENV" >&2
  fi
}
trap finish EXIT
if [ -e "$VENV" ]; then
  ours "$VENV" || die "$VENV exists but is not an OpenDrop install made by this script;" \
                      "delete it to install there, or set OPENDROP_VENV"
  if [ -e "$OLD" ]; then
    ours "$OLD" || die "$OLD is in the way; move it elsewhere"
    rm -rf -- "$OLD"
  fi
  if complete "$VENV"; then
    step "rebuilding $VENV (outdated; it stays usable until the new one works)"
    mv -- "$VENV" "$OLD"
    restore=1
  else
    step "rebuilding $VENV (previous install incomplete)"
    rm -rf -- "$VENV"
  fi
fi

step "creating $VENV with opendrop==$OPENDROP_VERSION"
mkdir -p "$(dirname "$VENV")"
mkdir "$VENV"                 # fresh: it did not exist (or was ours and set aside)
touch "$VENV/$STAMP_WIP"
python3 -m venv "$VENV"
# setuptools<81: OpenDrop imports pkg_resources, which setuptools 81 removed.
"$VENV/bin/pip" install -q "opendrop==$OPENDROP_VERSION" 'setuptools<81' \
  || die "pip install failed (run again to retry)"
SITE=$(echo "$VENV"/lib/python*/site-packages)
[ -f "$SITE/opendrop/server.py" ] || die "opendrop not found in $SITE"

# Digest of the package sources; all patches touch only opendrop/*.py.
tree_sum() {
  (cd "$SITE" && find opendrop -name '*.py' -type f -print0 | sort -z \
     | xargs -0 sha256sum | sha256sum)
}

step "applying ${#PATCHES[@]} patches"
for p in "${PATCHES[@]}"; do
  before=$(tree_sum)
  # GIT_DIR: `git apply` inside a git work tree (e.g. a home directory under
  # version control) applies paths relative to that repo and silently skips
  # everything else (airdrop-mt7921 install.sh). This makes it a plain patcher.
  ( cd "$SITE" && GIT_DIR=/nonexistent-espdrop-install git apply \
      "$SRC/patches/opendrop-$p.patch" ) || die "patch $p did not apply (run again to retry)"
  # Do not trust the exit code alone: the patch must actually have changed code.
  [ "$(tree_sum)" != "$before" ] || die "patch $p applied but changed nothing"
  echo "  ok  $p"
done

step "verifying"
grep -q 'trailing CRLF of the final chunk' "$SITE/opendrop/server.py" \
  || die "patches did not land in $SITE (ios26-airdrop missing)"
grep -q 'application/x-dvzip' "$SITE/opendrop/server.py" \
  || die "patches did not land in $SITE (dvzip support missing)"
"$VENV/bin/python" -m py_compile "$SITE"/opendrop/*.py \
  || die "patched OpenDrop does not compile"
# Dependencies are unpinned upstream; record what we got for bug reports.
"$VENV/bin/pip" freeze > "$VENV/espdrop-freeze.txt"
printf '%s\n' "$sig" > "$VENV/$STAMP_OK"
rm -f "$VENV/$STAMP_WIP"
restore=0
if [ -e "$OLD" ] && ours "$OLD"; then rm -rf -- "$OLD"; fi

echo
echo "done: $VENV/bin/opendrop (patched, rev $REV)"
echo "dependency versions recorded in $VENV/espdrop-freeze.txt"
echo "next: start the bridge (sudo tools/awdl_bridge.py /dev/ttyACM0), then tools/opendrop_receive.sh"
