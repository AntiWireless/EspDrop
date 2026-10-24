#!/usr/bin/env python3
# EspDrop - install the patched OpenDrop into a virtualenv, for Windows (M4.5).
#
# The Python counterpart of tools/opendrop_install.sh, for a PC without bash,
# git or openssl. Same OpenDrop: PyPI opendrop 0.13.0 plus the 17 patches of
# jedbillyb/airdrop-mt7921 (GPLv3) at the pinned revision, in their fixed order.
# The patches and every resulting file are checked against pinned digests (those
# of the install the bash script makes), so Windows and Linux run byte-identical
# OpenDrop code. On top come pyserial (COM ports), cryptography (the TLS
# certificate, in place of the openssl command) and PySide6 (the desktop app).
#
#   py tools\opendrop_install.py            Windows (Python 3.9+ from python.org)
#   python3 tools/opendrop_install.py       works on Linux too
#
# Options:
#   --venv DIR         target venv (default: OPENDROP_VENV, else ~/opendrop-venv)
#   --patches DIR      take the patches from DIR (a folder with opendrop-*.patch,
#                      e.g. patches/ of an airdrop-mt7921 checkout) instead of
#                      downloading the pinned revision from GitHub
#   --libarchive FILE  use this libarchive DLL (recorded in the venv; "auto"
#                      forgets it again)
#
# Re-running is safe: a complete, matching install is left alone; an incomplete
# or outdated one that this script or the bash one made is rebuilt (an outdated
# one is kept aside until the new one works). The last step runs
# `espdrop check`, which also tells whether libarchive works: Windows 10/11 have
# one (System32\archiveint.dll); if that does not do, host/README.md says how to
# get one.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

OPENDROP_VERSION = "0.13.0"
REV = "27edf28"                                   # as AIRDROP_MT7921_REV in the bash script
REV_FULL = "27edf28900d6b9a948816ee5e864afe1ce245056"
ARCHIVE_URL = f"https://github.com/jedbillyb/airdrop-mt7921/archive/{REV_FULL}.zip"

# The series in its fixed order (as PATCHES in opendrop_install.sh, "a series,
# not a menu"), each with the sha256 of the file at REV.
PATCHES = {
    "ios26-airdrop": "a12c60bbc8aa2ec9264c350c9641aaccf8de5a13d8762c2d534ebbfcf663eee3",
    "recv-window": "542d8a1795d7ed83ca400139b812cdacc1db27c42accb24917cb086ca96b5c6b",
    "py314-send": "b1069d13edfcae38edb0a6608b079fb9b61aeb69558f3c04cce23c0ae2987c40",
    "mdns-repeat": "5cc87ef8788b3e058a467e918897214eb431ef1ee4cf5049805eb1fade0e03eb",
    "find-report": "a0bae567a362eba6823c1b5454e017bddeb6da897aa7515c1db673da4db7fc8f",
    "tls-keylog": "cb11838f39d05653bfc984edfae081e5a3e857c57563f8f8b46432ac2cceffdf",
    "upload-arms": "2510d8f27ddec43c29fca14655c4fef86e0ec865467608f987212869df1a73de",
    "ask-confirm": "2e86aeb5c1880d660f80c699eb7b380b40b85357c7f3f2a6244cb664fdd2ee96",
    "mdns-reannounce": "6d035db7e092ff48a5817494ee63de4119f128f6fcb2a73d7779e9a04c11e770",
    "threaded-server": "1b6f49185fadd284e168a92f934c12c342dccaecfc1c092d9534a468b1435759",
    "url-items": "a3b5d527e311fb1f29a9e960d55e9b0cff60d7281315057431831299928bc9f2",
    "zeroconf-update-service": "fe550476d1da14f3437932ed97ba7b8314f0d5eba01f0af16bb3702a861d5e26",
    "salvage-truncated": "b00c24773a880eb965db4546c56f8bceb9b05deb832d6527e809344385b4e3ec",
    "salvage-trim": "d2dafd52800740be3140802dd494baf964a96c766ff8621f4db9679b7877ceb5",
    "send-multifile": "30cad173cf6488f4f5e972f048bfbbf2899c7decde2429bebcf7b9fa6dde2935",
    "send-status": "c209e99241d5a5aa6140ca34de9d9ceb6b8b91e8aa226d1104ca31612114051e",
    "send-stall": "c31c766bc816cb2696614bf92f04b0f3781a5e5361da6d2df2c3737642717edb",
}
# OpenDrop's sources after the series, as opendrop_install.sh leaves them.
RESULT = {
    "opendrop/__init__.py": "09c5e6d0cd29501c4ddd3191f8fbe11ea4a9d3ffc5dea74b12d51f244441f299",
    "opendrop/__main__.py": "b482241b8b4b6b8f4c185c768902771f83cb760840ac0f95a02bee8a95771080",
    "opendrop/cli.py": "d42a0cef8870a1525be95ec530d04582e71d5ce92534ee5839e71e98cbb69f02",
    "opendrop/client.py": "77b422a84923a56d2db18b7885d63a24297ab69bdd57eb3d29667eed75d55cc7",
    "opendrop/config.py": "7e42362549fe2ea074688b6b2be2c7cbe7ab7a54f01a35a19618cb26910dba2a",
    "opendrop/server.py": "be731940c314f2b2409c263d6585c407cb935c93c00de805affd602693d76503",
    "opendrop/util.py": "390ee35c01c3d332e57be49266610a44bd658dbe1fe16084a5e5bfeccb30fe2c",
}
EXTRAS = ["pyserial", "cryptography", "PySide6"]
# Markers in the venv, shared with the bash script: WIP while one of us builds
# it, OK (holding the series signature) once every patch landed and was checked.
STAMP_WIP = ".espdrop-installing"
STAMP_OK = ".espdrop-installed"
LIBARCHIVE_RECORD = "espdrop-libarchive.txt"      # read by host/espdrop/env.py

HERE = os.path.dirname(os.path.abspath(__file__))


class PatchError(Exception):
    pass


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def step(msg):
    print(f"\n==> {msg}", flush=True)


def venv_python(venv):
    return os.path.join(venv, "Scripts", "python.exe") if os.name == "nt" \
        else os.path.join(venv, "bin", "python")


def run(cmd, what, **kw):
    try:
        return subprocess.run(cmd, check=True, **kw)
    except (OSError, subprocess.CalledProcessError) as e:
        die(f"{what} failed ({e}); run again to retry")


def series_sig():
    v = sys.version_info
    return (f"opendrop {OPENDROP_VERSION} python {v[0]}.{v[1]} mt7921 {REV} "
            f"patches {' '.join(PATCHES)}")


def ours(d):
    return (os.path.exists(os.path.join(d, STAMP_OK)) or os.path.exists(os.path.join(d, STAMP_WIP))
            or (os.path.isfile(os.path.join(d, "pyvenv.cfg"))
                and os.path.isfile(os.path.join(d, "espdrop-freeze.txt"))))


def complete(d):
    return not os.path.exists(os.path.join(d, STAMP_WIP)) and (
        os.path.exists(os.path.join(d, STAMP_OK))
        or os.path.isfile(os.path.join(d, "espdrop-freeze.txt")))


def up_to_date(d):
    try:
        with open(os.path.join(d, STAMP_OK), encoding="utf-8") as f:
            return complete(d) and f.read().strip() == series_sig()
    except OSError:
        return False


def rmtree(path):
    def retry(func, p, _exc):       # read-only files (Windows)
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---- the patch series -------------------------------------------------------

def fetch_patches(src_dir):
    """{name: bytes} for the whole series, each checked against its pin."""
    got = {}
    if src_dir:
        for name in PATCHES:
            p = os.path.join(src_dir, f"opendrop-{name}.patch")
            try:
                with open(p, "rb") as f:
                    got[name] = f.read()
            except OSError as e:
                die(f"patch {name} missing in {src_dir} ({e})")
    else:
        print(f"downloading {ARCHIVE_URL}", flush=True)
        try:
            with urllib.request.urlopen(ARCHIVE_URL, timeout=120) as r:
                blob = r.read()
            z = zipfile.ZipFile(io.BytesIO(blob))
        except (OSError, urllib.error.URLError, zipfile.BadZipFile) as e:
            die(f"download failed ({e}); without internet, fetch that zip elsewhere, "
                f"unpack it and use --patches <folder>/patches")
        members = {n.rsplit("/", 1)[-1]: n for n in z.namelist() if "/patches/" in n}
        for name in PATCHES:
            m = members.get(f"opendrop-{name}.patch")
            if not m:
                die(f"patch {name} missing in the downloaded archive")
            got[name] = z.read(m)
    for name, data in got.items():
        if sha256(data) != PATCHES[name]:
            die(f"patch {name} is not the one reviewed for rev {REV} (sha256 {sha256(data)})")
    return got


HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """A unified diff (git or diff -u style) -> [(path, [(old_start, old, new)])].

    Paths lose their first component (-p1). Only what the series uses: changes
    to existing text files; anything else is refused rather than guessed."""
    lines = text.split("\n")
    files, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            src = ln[4:].split("\t")[0].strip()
            dst = lines[i + 1][4:].split("\t")[0].strip()
            if "/dev/null" in (src, dst):
                raise PatchError(f"creates or deletes a file ({src} -> {dst})")
            files.append((dst.split("/", 1)[1] if "/" in dst else dst, []))
            i += 2
            continue
        m = HUNK.match(ln)
        if m:
            if not files:
                raise PatchError("hunk before any file header")
            old_n = 1 if m[2] is None else int(m[2])
            new_n = 1 if m[4] is None else int(m[4])
            old, new = [], []
            i += 1
            while len(old) < old_n or len(new) < new_n:
                if i >= len(lines):
                    raise PatchError("patch ends inside a hunk")
                ln = lines[i]
                tag, body = ln[:1], ln[1:]
                if tag == " " or ln == "":        # "" : a blank context line, space stripped
                    old.append(body)
                    new.append(body)
                elif tag == "-":
                    old.append(body)
                elif tag == "+":
                    new.append(body)
                else:
                    raise PatchError(f"unexpected line in a hunk: {ln[:60]!r}")
                i += 1
            if (len(old), len(new)) != (old_n, new_n):
                raise PatchError(f"hunk {ln!r}: line counts do not match its header")
            files[-1][1].append((int(m[1]), old, new))
            continue
        i += 1                                    # diff/index lines, commentary
    return files


def _find(lines, old, want):
    """Index where `old` occurs, nearest to `want` (exact match only)."""
    n = len(old)
    for d in range(len(lines) + 1):
        for pos in ((want,) if d == 0 else (want - d, want + d)):
            if 0 <= pos <= len(lines) - n and lines[pos:pos + n] == old:
                return pos
    return None


def apply_hunks(lines, hunks, path="file"):
    """Apply one file's hunks in order, each where its context matches exactly
    (at its line or the nearest offset, like `git apply` without fuzz)."""
    out, shift = list(lines), 0
    for start, old, new in hunks:
        base = start - 1 if old else start        # a pure insertion goes after line `start`
        if old:
            pos = _find(out, old, base + shift)
            if pos is None:
                raise PatchError(f"{path}: the hunk at line {start} does not apply")
        else:
            pos = min(max(base + shift, 0), len(out))
        out[pos:pos + len(old)] = new
        shift = pos - base + len(new) - len(old)
    return out


def apply_patch(site, data, name):
    """Apply one patch of the series to the files under `site`; all or nothing."""
    files = parse_patch(data.decode("utf-8"))
    if not files:
        raise PatchError(f"{name}: no changes in it")
    changed = {}
    for path, hunks in files:
        parts = path.split("/")
        if os.path.isabs(path) or ".." in parts or parts[0] != "opendrop":
            raise PatchError(f"{name}: refusing path {path!r}")
        full = os.path.join(site, *parts)
        if full in changed:
            text = changed[full]
        else:
            with open(full, "rb") as f:
                raw = f.read()
            if b"\r" in raw:
                raise PatchError(f"{name}: {path} has CR line ends")
            text = raw.decode("utf-8")
        changed[full] = "\n".join(apply_hunks(text.split("\n"), hunks, path))
    for full, text in changed.items():
        with open(full, "wb") as f:
            f.write(text.encode("utf-8"))


def tree(site):
    """{opendrop/x.py: sha256} of the package sources (what the patches touch)."""
    pkg = os.path.join(site, "opendrop")
    out = {}
    for fn in sorted(os.listdir(pkg)):
        if fn.endswith(".py"):
            with open(os.path.join(pkg, fn), "rb") as f:
                out[f"opendrop/{fn}"] = sha256(f.read())
    return out


# ---- the venv ---------------------------------------------------------------

def build(venv, py, patches):
    step(f"creating {venv} with opendrop=={OPENDROP_VERSION}")
    os.makedirs(venv)                 # fresh: it did not exist (or was ours and set aside)
    open(os.path.join(venv, STAMP_WIP), "w").close()
    run([sys.executable, "-m", "venv", venv], "creating the venv")
    # setuptools<81: OpenDrop imports pkg_resources, which setuptools 81 removed.
    run([py, "-m", "pip", "install", "-q", f"opendrop=={OPENDROP_VERSION}", "setuptools<81"]
        + EXTRAS, "pip install")
    site = subprocess.run([py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          check=True, capture_output=True, text=True).stdout.strip()
    if not os.path.isfile(os.path.join(site, "opendrop", "server.py")):
        die(f"opendrop not found in {site}")

    step(f"applying {len(PATCHES)} patches")
    for name, data in patches.items():
        before = tree(site)
        try:
            apply_patch(site, data, name)
        except (PatchError, OSError, UnicodeDecodeError) as e:
            die(f"patch {name} did not apply: {e}")
        if tree(site) == before:
            die(f"patch {name} applied but changed nothing")
        print(f"  ok  {name}", flush=True)

    step("verifying")
    got = tree(site)
    bad = sorted(p for p in set(RESULT) | set(got) if RESULT.get(p) != got.get(p))
    if bad:
        die(f"patched OpenDrop differs from the reviewed install in: {', '.join(bad)}")
    run([py, "-m", "compileall", "-q", os.path.join(site, "opendrop")], "compiling OpenDrop")
    # Dependencies are unpinned upstream; record what we got for bug reports.
    freeze = run([py, "-m", "pip", "freeze"], "pip freeze", capture_output=True).stdout
    with open(os.path.join(venv, "espdrop-freeze.txt"), "wb") as f:
        f.write(freeze)
    with open(os.path.join(venv, STAMP_OK), "w", encoding="utf-8") as f:
        f.write(series_sig() + "\n")
    os.remove(os.path.join(venv, STAMP_WIP))
    print(f"OpenDrop {OPENDROP_VERSION} + {len(PATCHES)} patches: identical to the "
          f"Linux install ({len(RESULT)} files checked)")


def main():
    ap = argparse.ArgumentParser(description="install the patched OpenDrop for EspDrop")
    ap.add_argument("--venv", default=os.environ.get("OPENDROP_VENV")
                    or os.path.join(os.path.expanduser("~"), "opendrop-venv"))
    ap.add_argument("--patches", metavar="DIR", help="folder with opendrop-*.patch "
                                                    "(default: download rev " + REV + ")")
    ap.add_argument("--libarchive", metavar="FILE",
                    help="libarchive DLL to use (recorded in the venv); 'auto' forgets it")
    args = ap.parse_args()
    venv = os.path.abspath(args.venv)
    py = venv_python(venv)

    step("checking requirements")
    if sys.version_info < (3, 9):
        die(f"Python 3.9 or newer required, this is {sys.version.split()[0]}")
    if os.name == "posix" and os.geteuid() == 0:
        die("run this as your normal user (OpenDrop keeps its state in ~/.opendrop)")
    try:
        import venv as _venv  # noqa: F401
        import ensurepip  # noqa: F401
    except ImportError:
        die("this Python has no venv module (Debian/Ubuntu: apt install python3-venv)")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")

    # A rebuild that was killed hard leaves the previous install aside; put it back.
    old = venv + ".espdrop-old"
    if os.path.exists(old) and ours(old) and complete(old) and (
            not os.path.exists(venv) or (ours(venv) and not complete(venv))):
        if os.path.exists(venv):
            rmtree(venv)
        os.rename(old, venv)

    if up_to_date(venv):
        print(f"\nalready installed and up to date: {venv}")
        extras = [m for m in ("serial", "cryptography", "PySide6")
                  if subprocess.run([py, "-c", f"import {m}"], capture_output=True).returncode]
        if extras:      # a venv the bash script made, or one from before the GUI
            step("adding pyserial, cryptography and PySide6")
            run([py, "-m", "pip", "install", "-q"] + EXTRAS, "pip install")
    else:
        step(f"fetching the patch series (rev {REV})")
        patches = fetch_patches(args.patches)
        restore = False
        if os.path.exists(venv):
            if not ours(venv):
                die(f"{venv} exists but is not an OpenDrop install made by EspDrop; "
                    f"delete it to install there, or use --venv")
            if os.path.exists(old):
                if not ours(old):
                    die(f"{old} is in the way; move it elsewhere")
                rmtree(old)
            if complete(venv):
                step(f"rebuilding {venv} (outdated; it stays usable until the new one works)")
                try:
                    os.rename(venv, old)
                except OSError as e:     # Windows: a program still runs from it
                    die(f"cannot move {venv} aside ({e}); close espdrop and anything "
                        f"else using that venv")
                restore = True
            else:
                step(f"rebuilding {venv} (previous install incomplete)")
                rmtree(venv)
        try:
            build(venv, py, patches)
        except BaseException:
            if restore:
                if os.path.exists(venv):
                    rmtree(venv)
                os.rename(old, venv)
                print(f"kept the previous install in {venv}", file=sys.stderr)
            raise
        if os.path.exists(old) and ours(old):
            rmtree(old)

    record = os.path.join(venv, LIBARCHIVE_RECORD)
    if args.libarchive == "auto":
        if os.path.exists(record):
            os.remove(record)
    elif args.libarchive:
        path = os.path.abspath(args.libarchive)
        if not os.path.isfile(path):
            die(f"{path} does not exist")
        with open(record, "w", encoding="utf-8") as f:
            f.write(path + "\n")
        print(f"libarchive: {path} (recorded in {record})")

    step("espdrop check")
    rc = subprocess.run([py, os.path.join(HERE, "espdrop.py"), "check"]).returncode
    launcher = r".\tools\espdrop.cmd" if os.name == "nt" else f"{py} tools/espdrop.py"
    gui = r".\tools\espdrop-gui.cmd" if os.name == "nt" else f"{launcher} gui"
    if rc:
        print(f"\ninstalled in {venv}, but the check above found problems.\n"
              f"libarchive: see host/README.md ('Windows'), then run this again with "
              f"--libarchive <path to the DLL>.", file=sys.stderr)
        return 1
    print(f"\ndone: {venv}\nnext: plug in the ESP (bridge firmware) and run, from the checkout,\n"
          f"  {launcher} status\n  {launcher} find\n  {launcher} send <file>\n"
          f"  {launcher} receive\n"
          f"the desktop app: {gui}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
