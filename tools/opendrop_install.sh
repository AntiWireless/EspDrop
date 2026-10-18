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
if command -v ldconfig >/dev/null && ! ldconfig -p 2>/dev/null | grep -q 'libarchive\.so'; then
  echo "warning: libarchive not found by ldconfig; OpenDrop needs it to unpack transfers" >&2
fi
echo "python: $(python3 -V 2>&1)"

# `git apply` run inside a git work tree applies paths relative to that repo
# and silently skips everything else (airdrop-mt7921 install.sh). Keep the venv
# outside any repository; GIT_DIR below guards against it as well.
venv_parent="$(dirname "$VENV")"
mkdir -p "$venv_parent"
if git -C "$venv_parent" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "$VENV is inside a git repository; choose another OPENDROP_VENV (e.g. ~/opendrop-venv)"
fi

if [ -d "$VENV" ]; then
  SITE=$(echo "$VENV"/lib/python*/site-packages)
  if grep -q 'trailing CRLF of the final chunk' "$SITE/opendrop/server.py" 2>/dev/null; then
    echo "already installed: $VENV (delete it to reinstall)"
    exit 0
  fi
  die "$VENV exists but is not a patched OpenDrop; delete it and run again"
fi

step "fetching the patch series ($REV)"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q origin
else
  mkdir -p "$(dirname "$SRC")"
  git clone -q https://github.com/jedbillyb/airdrop-mt7921.git "$SRC"
fi
git -C "$SRC" -c advice.detachedHead=false checkout -q "$REV"
for p in "${PATCHES[@]}"; do
  [ -f "$SRC/patches/opendrop-$p.patch" ] || die "patch $p missing in $SRC at $REV"
done

step "creating $VENV with opendrop==$OPENDROP_VERSION"
python3 -m venv "$VENV"
# setuptools<81: OpenDrop imports pkg_resources, which setuptools 81 removed.
"$VENV/bin/pip" install -q "opendrop==$OPENDROP_VERSION" 'setuptools<81' \
  || die "pip install failed"
SITE=$(echo "$VENV"/lib/python*/site-packages)

step "applying ${#PATCHES[@]} patches"
for p in "${PATCHES[@]}"; do
  ( cd "$SITE" && GIT_DIR=/nonexistent-espdrop-install git apply \
      "$SRC/patches/opendrop-$p.patch" ) || die "patch $p did not apply"
  echo "  ok  $p"
done

step "verifying"
# Check that the patches really landed instead of trusting git apply's exit code.
grep -q 'trailing CRLF of the final chunk' "$SITE/opendrop/server.py" \
  || die "patches did not land in $SITE (ios26-airdrop missing)"
grep -q 'application/x-dvzip' "$SITE/opendrop/server.py" \
  || die "patches did not land in $SITE (dvzip support missing)"
"$VENV/bin/python" -m py_compile "$SITE"/opendrop/*.py \
  || die "patched OpenDrop does not compile"
# Dependencies are unpinned upstream; record what we got for bug reports.
"$VENV/bin/pip" freeze > "$VENV/espdrop-freeze.txt"

echo
echo "done: $VENV/bin/opendrop (patched, rev $REV)"
echo "dependency versions recorded in $VENV/espdrop-freeze.txt"
echo "next: start the bridge (sudo tools/awdl_bridge.py /dev/ttyACM0), then tools/opendrop_receive.sh"
