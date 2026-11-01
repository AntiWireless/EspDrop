#!/usr/bin/env python3
"""EspDrop - the app as a native program, which needs no Python to run.

Nuitka translates the Python code (ours, the patched OpenDrop and every
library they use) to C, compiles it, and puts it together with the Python
runtime, Qt and the ESP's firmware into one folder:

  build/app/EspDrop.dist/EspDrop(.exe)            the program
  build/EspDrop-<version>-windows-x64.zip         what a release ships
  build/EspDrop-<version>-linux-x86_64.tar.gz

The program is the whole app: started without arguments it is the window,
with arguments the command line (EspDrop find, EspDrop flash, ...; on Windows
it uses the console it was started from and opens none of its own).

Run it with the Python of the venv tools/opendrop_install.py made (the patched
OpenDrop must be in there), after `pip install nuitka` (Linux also
`pip install patchelf`). Windows needs a C compiler: Visual Studio or its
Build Tools with C++, or --zig (Nuitka then downloads Zig itself).

  ~/opendrop-venv/bin/python tools/build_app.py --firmware espdrop-esp32c5.bin
  %USERPROFILE%\\opendrop-venv\\Scripts\\python tools\\build_app.py --zig

Without --firmware/--firmware-build it takes what `espdrop flash` would
(ESPDROP_FIRMWARE, a local ESP-IDF build of the bridge); with none at all the
app is built without one and asks for a file when it should flash.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOST = os.path.join(ROOT, "host")
sys.path[:0] = [HOST, HERE]

from espdrop import flash  # noqa: E402
from espdrop.version import __version__  # noqa: E402

IS_WINDOWS = os.name == "nt"
NAME = "EspDrop"
# What the compiled program carries besides its code (and what it is built from).
PACKAGES = ["espdrop", "opendrop", "esptool"]
# Their data files: Apple's root certificate, the flasher stubs, the file types
# OpenDrop looks up (fleep/data.json, read at import).
DATA_PACKAGES = ["opendrop", "esptool", "fleep"]


def step(text):
    print(f"\n== {text}", flush=True)


def check_build_env():
    """The venv has what the app needs, and Nuitka; else SystemExit with why."""
    missing = []
    for module in ("PySide6", "serial", "cryptography", "zeroconf", "esptool", "nuitka"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        sys.exit(f"error: {', '.join(missing)} missing in {sys.executable}; run it with the "
                 "venv of tools/opendrop_install.py, after: pip install nuitka"
                 + ("" if IS_WINDOWS else " patchelf"))
    from espdrop import env
    try:
        env.load_opendrop()
        import opendrop.server
    except ImportError as e:
        sys.exit(f"error: {e}")
    with open(opendrop.server.__file__, encoding="utf-8") as f:
        if "trailing CRLF of the final chunk" not in f.read():
            sys.exit("error: this OpenDrop is not patched; build with the venv of "
                     "tools/opendrop_install.py")


def make_icons(res):
    """The app's mark as files: .ico for the Windows program, .png for Linux."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:     # painting needs one, alive meanwhile
        make_icons.app = QGuiApplication([])
    from espdrop import ui
    os.makedirs(res, exist_ok=True)
    paths = {}
    for name, data in (("espdrop.ico", ui.ico_bytes()), ("espdrop.png", ui._png_bytes(256))):
        paths[name] = os.path.join(res, name)
        with open(paths[name], "wb") as f:
            f.write(data)
    return paths


def firmware(args, res):
    """The firmware image to ship (path), checked; None to build without one."""
    import firmware_image
    source = args.firmware or args.firmware_build or flash.bundled()
    if not source:
        print("warning: no firmware to ship (--firmware IMAGE or --firmware-build DIR); "
              "the app will ask for a file when it flashes", flush=True)
        return None
    fw = flash.load(source)
    if len(fw.parts) > 1:                      # a build folder: merge it into one image
        out = os.path.join(res, flash.FIRMWARE_NAME)
        fw = firmware_image.make_image(source, out)
        source = out
    if fw.chip_id != 23:
        sys.exit(f"error: {source} is for the {fw.chip}; the app ships the ESP32-C5's")
    print(f"firmware: EspDrop {fw.version} for the {fw.chip}, built {fw.built} ({source})")
    if fw.version != __version__:
        print(f"warning: the firmware is {fw.version}, the app {__version__}", flush=True)
    return source


def check_x11_libraries():
    """Linux: Qt's X11 plugin needs some xcb libraries; present here, Nuitka
    puts them into the program, so users need not install them."""
    from espdrop import env
    missing = env.xcb_missing()
    if missing:
        sys.exit("error: Qt's X11 plugin lacks " + ", ".join(missing) + " on this machine; "
                 "install them so the program carries them: sudo apt install "
                 + " ".join(env.debian_package(m) for m in missing))


def system_libarchive():
    """The libarchive the build machine's OpenDrop uses, as a file (Linux)."""
    from espdrop import env
    path, _detail = env.prepare_libarchive()
    if path and os.path.isfile(path):
        return os.path.realpath(path)
    out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        name, _, where = line.strip().partition(" => ")
        if name.startswith(env.BUNDLED_LIBARCHIVE[0] + " ") and os.path.isfile(where):
            return os.path.realpath(where)
    sys.exit("error: libarchive.so.13 not found on this machine (apt install libarchive13)")


def nuitka_command(args, out, icons, fw):
    version4 = ".".join((__version__.split(".") + ["0", "0", "0"])[:4])
    cmd = [sys.executable, "-m", "nuitka", "--mode=standalone",
           f"--output-dir={out}", f"--output-folder-name={NAME}", f"--output-filename={NAME}",
           "--enable-plugin=pyside6", "--include-qt-plugins=platforminputcontexts",
           "--noinclude-qt-translations", "--nofollow-import-to=tkinter",
           "--no-prefer-source-code",          # zeroconf's compiled modules as they are
           "--assume-yes-for-downloads", f"--report={os.path.join(out, 'nuitka-report.xml')}",
           f"--product-name={NAME}", f"--file-version={version4}",
           f"--product-version={version4}", "--company-name=EspDrop contributors",
           "--file-description=EspDrop - AirDrop über den ESP32-C5",
           "--copyright=GPL-3.0-or-later"]
    cmd += [f"--include-package={p}" for p in PACKAGES]
    cmd += [f"--include-package-data={p}" for p in DATA_PACKAGES]
    if fw:
        cmd.append(f"--include-data-files={fw}=espdrop/firmware/{flash.FIRMWARE_NAME}")
    if IS_WINDOWS:
        cmd += ["--windows-console-mode=attach", f"--windows-icon-from-ico={icons['espdrop.ico']}"]
        if args.zig:
            cmd.append("--zig")
    if args.jobs:
        cmd.append(f"--jobs={args.jobs}")
    cmd.append(os.path.join(HOST, "espdrop_app.py"))   # beside the package: see that file
    return cmd


def glibc_needed(dist):
    """The newest glibc version any file of the program asks for (Linux), as
    "2.35", measured from the symbol versions; None if objdump is missing."""
    import re
    if not shutil.which("objdump"):
        return None
    best = (0,)
    for folder, _dirs, files in os.walk(dist):
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                if f.read(4) != b"\x7fELF":
                    continue
            out = subprocess.run(["objdump", "-T", path], capture_output=True, text=True).stdout
            for v in re.findall(r"GLIBC_(\d+(?:\.\d+)+)", out):
                best = max(best, tuple(int(x) for x in v.split(".")))
    return ".".join(map(str, best)) if best != (0,) else None


def third_party(dist):
    """LICENSE and a list of what the program carries, with each license."""
    from importlib import metadata
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(dist, "LICENSE.txt"))
    rows = []
    for d in metadata.distributions():
        name = d.metadata.get("Name") or "?"
        if name.lower() in ("nuitka", "pip", "patchelf", "ordered-set", "zstandard", "wheel"):
            continue
        lic = (d.metadata.get("License-Expression") or d.metadata.get("License") or "").strip()
        if lic.upper() == "UNKNOWN":
            lic = ""
        if not lic or len(lic) > 80:           # none, or a whole license text: the classifier
            lic = next((c.split(" :: ")[-1] for c in d.metadata.get_all("Classifier") or []
                        if c.startswith("License ::")), lic.splitlines()[0] if lic else "?")
        rows.append(f"{name} {d.version}: {lic}")
    with open(os.path.join(dist, "THIRD_PARTY.txt"), "w", encoding="utf-8") as f:
        f.write(f"{NAME} {__version__} (GPL-3.0-or-later, LICENSE.txt; source code: "
                "https://github.com/antiwireless/espdrop) is built with Python and these "
                "packages, each under its own license:\n\n" + "\n".join(sorted(rows, key=str.lower))
                + "\n\nQt (in PySide6) is used under the LGPL-3.0; its libraries are the "
                  "separate files in this folder.\nlibarchive (Linux, libarchive.so.13): "
                  "BSD-2-Clause.\n")


CONSOLE_CMD = """@echo off
rem EspDrop on the command line (espdrop status, espdrop flash, ...).
rem EspDrop.exe opens no console of its own and writes into this one; this
rem makes cmd and PowerShell wait for it and pass on its exit code.
start "" /b /wait "%~dp0EspDrop.exe" %*
exit /b %errorlevel%
"""


def package(dist, build):
    machine = platform.machine().lower()
    if IS_WINDOWS:
        arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64"}.get(machine, machine)
        path = os.path.join(build, f"{NAME}-{__version__}-windows-{arch}.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for folder, _dirs, files in os.walk(dist):
                for name in files:
                    full = os.path.join(folder, name)
                    z.write(full, os.path.join(NAME, os.path.relpath(full, dist)))
    else:
        path = os.path.join(build, f"{NAME}-{__version__}-linux-{machine}.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            t.add(dist, arcname=NAME)
    return path


def smoke(dist):
    """The built program starts: --version, check, and the window (offscreen)."""
    exe = os.path.join(dist, NAME + (".exe" if IS_WINDOWS else ""))
    v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
    if v.stdout.strip() != f"espdrop {__version__}":
        sys.exit(f"smoke: --version said {v.stdout!r} {v.stderr!r}")
    c = subprocess.run([exe, "check"], capture_output=True, text=True, timeout=300)
    print(c.stdout, c.stderr, flush=True)
    needs = ["ok   opendrop", "ok   root ca", "ok   libarchive", "ok   esptool", "ok   pyside6",
             "ok   program"]
    if not IS_WINDOWS:
        needs.append("ok   x11")               # the X11 plugin's libraries came along
    for need in needs:
        if need not in c.stdout:
            sys.exit(f"smoke: `check` lacks {need!r}")
    # The path autostart and the menus will start must be this very file.
    said = c.stdout.split("ok   program", 1)[1].split("(Python", 1)[0].strip()

    def same(p):
        return os.path.normcase(os.path.realpath(p.replace("\\\\?\\", "", 1)))
    if same(said) != same(exe):
        sys.exit(f"smoke: the program calls itself {said!r}, but it is {exe!r}")
    # Hidden, no desktop entries for this test copy, beside a running EspDrop.
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", ESPDROP_NO_DESKTOP="1",
               ESPDROP_INSTANCE=f"espdrop-smoke-{os.getpid()}")
    p = subprocess.Popen([exe, "-p", "none-such-port", "gui", "--background"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(15)
        if p.poll() is not None:
            sys.exit(f"smoke: the window ended (rc {p.returncode}): {p.stdout.read()}")
    finally:
        p.kill()
        p.wait(10)
    if not IS_WINDOWS and shutil.which("xvfb-run"):
        # The window once more through Qt's X11 plugin, as on most desktops
        # (offscreen does not load it): still running after 12 s is fine.
        x11 = {k: v for k, v in env.items() if k != "QT_QPA_PLATFORM"}
        r = subprocess.run(["xvfb-run", "-a", "-s", "-screen 0 1280x1000x24", "timeout", "12",
                            exe, "-p", "none-such-port", "gui"], env=x11,
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 124:
            sys.exit(f"smoke: under X11 the window ended (rc {r.returncode}): "
                     f"{r.stdout[-1500:]}{r.stderr[-1500:]}")
    print("smoke: --version, check and the window are fine", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--firmware", help="firmware image to ship (espdrop-esp32c5.bin)")
    ap.add_argument("--firmware-build", help="ESP-IDF build folder of the bridge to ship")
    ap.add_argument("--zig", action="store_true", help="Windows: compile with Zig, not MSVC")
    ap.add_argument("--jobs", type=int, help="parallel C compilations (default: CPUs)")
    ap.add_argument("--smoke", action="store_true", help="start the built program to check it")
    ap.add_argument("--out", default=os.path.join(ROOT, "build"), help="build folder")
    args = ap.parse_args(argv)

    step("checking the build environment")
    check_build_env()
    if not IS_WINDOWS:
        check_x11_libraries()
    build = os.path.abspath(args.out)
    out, res = os.path.join(build, "app"), os.path.join(build, "app-res")
    icons = make_icons(res)
    fw = firmware(args, res)

    step(f"compiling {NAME} {__version__} with Nuitka (takes a while)")
    dist = os.path.join(out, NAME + ".dist")    # Nuitka adds .dist to the folder name
    if os.path.isdir(dist):
        shutil.rmtree(dist)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (HOST, os.environ.get("PYTHONPATH")) if p))
    r = subprocess.run(nuitka_command(args, out, icons, fw), env=env)
    if r.returncode or not os.path.isdir(dist):
        sys.exit(f"error: Nuitka failed (rc {r.returncode})")

    step("adding what the program needs beside its code")
    if not IS_WINDOWS:
        need = glibc_needed(dist)
        print(f"runs on Linux with glibc {need} or newer (x86_64)" if need
              else "glibc needed: unknown (no objdump)", flush=True)
        lib = system_libarchive()
        shutil.copy2(lib, os.path.join(dist, "libarchive.so.13"))
        print(f"libarchive: {lib} (a fallback; the system's comes first)")
    shutil.copy2(icons["espdrop.png"], os.path.join(dist, "espdrop.png"))
    if IS_WINDOWS:
        with open(os.path.join(dist, "espdrop.cmd"), "w", newline="\r\n") as f:
            f.write(CONSOLE_CMD)
    third_party(dist)
    if args.smoke:
        step("starting the built program")
        smoke(dist)
    step("packing")
    path = package(dist, build)
    print(f"{path} ({os.path.getsize(path) / 1e6:.0f} MB)\nthe program: "
          f"{os.path.join(dist, NAME + ('.exe' if IS_WINDOWS else ''))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
