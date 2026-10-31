# EspDrop - the host app's runtime environment, on Windows and Linux (M4.5).
#
# OpenDrop takes two things from the system that a Linux distribution provides
# and Windows does not:
#   - libarchive, which python-libarchive-c loads with ctypes, for the archive
#     of every transfer. There is no official Windows build. Windows 10/11 ship
#     one of their own as System32\archiveint.dll; we try it after an explicit
#     LIBARCHIVE, the path tools/opendrop_install.py recorded in the venv and
#     the normal library search. `espdrop check` shows whether it works.
#   - the openssl command, with which OpenDrop makes its self-signed TLS
#     certificate on first start. When python-cryptography is installed (the
#     Windows installer adds it) we make the same certificate with it before
#     OpenDrop looks; otherwise OpenDrop calls openssl as before, so Linux is
#     unchanged.
# One OpenDrop habit also breaks on Windows: it builds the archive member names
# and the /Ask FileBomPath with os.path.join(".", name), i.e. ".\name", and
# reads the files back through libarchive's narrow-char paths
# (fix_archive_writer, fix_ask_paths).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import ctypes
import ctypes.util
import datetime
import os
import sys

LIBARCHIVE_RECORD = "espdrop-libarchive.txt"   # in the venv, see tools/opendrop_install.py

# What OpenDrop and libarchive-c need from libarchive, checked before loading
# it for real (libarchive-c binds ~100 functions on import and fails on the
# first missing one, with a less helpful message).
_REQUIRED = ("archive_version_number", "archive_read_new", "archive_read_support_format_all",
             "archive_read_support_filter_all", "archive_write_new",
             "archive_write_set_format_cpio", "archive_write_add_filter_gzip",
             "archive_write_disk_new", "archive_read_disk_new", "archive_entry_new")

if os.name == "nt":
    INSTALL_HINT = ("set it up with: py tools\\opendrop_install.py, then start espdrop with "
                    "tools\\espdrop.cmd (it uses %USERPROFILE%\\opendrop-venv)")
    LIBARCHIVE_HINT = ("see host/README.md, 'Windows': install libarchive with MSYS2 and run "
                       "py tools\\opendrop_install.py --libarchive "
                       "C:\\msys64\\ucrt64\\bin\\libarchive-13.dll")
else:
    INSTALL_HINT = ("set it up with tools/opendrop_install.sh and run espdrop with "
                    "~/opendrop-venv/bin/python")
    LIBARCHIVE_HINT = "install libarchive (Debian/Ubuntu: apt install libarchive13)"

_libarchive = None      # (path or None, detail) once prepare_libarchive() ran


def probe_libarchive(path):
    """(ok, detail) for a libarchive file: it loads and has what we use."""
    try:
        lib = ctypes.CDLL(path)
    except (OSError, TypeError) as e:
        return False, f"cannot load: {e}"
    missing = [s for s in _REQUIRED if not hasattr(lib, s)]
    if missing:
        return False, "lacks " + ", ".join(missing)
    lib.archive_version_number.restype = ctypes.c_int
    v = lib.archive_version_number()
    return True, f"libarchive {v // 1000000}.{v // 1000 % 1000}.{v % 1000}"


def recorded_libarchive():
    """The libarchive tools/opendrop_install.py --libarchive recorded, or None."""
    try:
        with open(os.path.join(sys.prefix, LIBARCHIVE_RECORD), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


BUNDLED_LIBARCHIVE = ("libarchive.so.13",)    # next to the compiled program (Linux)


def libarchive_candidates():
    """Where to look, best first: a path, or None for the normal library search
    (PATH on Windows, ldconfig on Linux; what libarchive-c does by itself)."""
    if os.environ.get("LIBARCHIVE"):
        return [os.environ["LIBARCHIVE"]]       # explicit: that one or nothing
    out = []
    rec = recorded_libarchive()
    if rec:
        out.append(rec)
    out.append(None)
    if os.name == "nt":
        out.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                "System32", "archiveint.dll"))
    from .frozen import program_dir
    bundled = program_dir()
    if bundled:                                 # the Linux build carries a copy
        out += [os.path.join(bundled, n) for n in BUNDLED_LIBARCHIVE
                if os.path.isfile(os.path.join(bundled, n))]
    return out


def prepare_libarchive():
    """Point python-libarchive-c at a usable libarchive, before it is imported.

    Returns (path, detail), path None meaning libarchive-c's own search finds
    it. Raises ImportError with advice when none works."""
    global _libarchive
    if _libarchive:
        return _libarchive
    tried = []
    for cand in libarchive_candidates():
        path = cand or ctypes.util.find_library("archive")
        if not path:
            tried.append("library search: nothing found")
            continue
        ok, detail = probe_libarchive(path)
        if ok:
            if cand:
                os.environ["LIBARCHIVE"] = cand
            _libarchive = (cand, f"{detail} ({path})")
            return _libarchive
        tried.append(f"{path}: {detail}")
    raise ImportError("no usable libarchive (" + "; ".join(tried) + "); " + LIBARCHIVE_HINT)


def load_opendrop():
    """Import the patched OpenDrop, ready for this platform.

    Raises ImportError with advice when OpenDrop or libarchive is missing."""
    import warnings
    prepare_libarchive()
    try:
        with warnings.catch_warnings():
            # config.py imports pkg_resources (hence setuptools<81 in the venv);
            # the deprecation notice on every start is not for our users.
            warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
            import opendrop.client    # noqa: F401  (loads libarchive)
            import opendrop.config    # noqa: F401
            import opendrop.server    # noqa: F401
            import opendrop.util
    except ImportError as e:
        raise ImportError(f"cannot import OpenDrop ({e}); {INSTALL_HINT}") from e
    except (OSError, AttributeError, TypeError) as e:     # libarchive-c binding it
        missing = getattr(e, "filename", None)
        if isinstance(e, FileNotFoundError) and missing and "archive" not in str(missing):
            raise ImportError(f"OpenDrop lacks a file of its own ({e})") from e
        raise ImportError(f"libarchive does not work with OpenDrop ({e}); "
                          f"{LIBARCHIVE_HINT}") from e
    if os.name == "nt":
        fix_archive_writer(opendrop.util.AbsArchiveWrite)
        fix_ask_paths(opendrop.client.AirDropClient)


def fix_archive_writer(cls):
    """Replace OpenDrop's AbsArchiveWrite.add_abs_file (Windows).

    The original names the member os.path.join(".", name), which on Windows is
    ".\\name", and takes the file's data and metadata from libarchive's
    read_disk, whose narrow-char paths are in the ANSI code page there (a user
    folder like C:\\Users\\Jörg breaks it). This one opens the file in Python
    and writes the member itself, as read_disk would describe it: a regular file
    with size, mtime, one link and an inode number (the cpio writer numbers
    members 1, 2, ...), under the name with "/". Idempotent."""
    if getattr(cls.add_abs_file, "espdrop", False):
        return
    from libarchive import ffi
    from libarchive.entry import ArchiveEntry, FileType
    lib = ffi.libarchive
    lib.archive_entry_set_nlink.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.archive_entry_set_nlink.restype = None
    lib.archive_entry_set_ino64.argtypes = [ctypes.c_void_p, ctypes.c_int64]
    lib.archive_entry_set_ino64.restype = None

    def add_abs_file(self, path, store_path):
        st = os.stat(path)
        entry = ArchiveEntry(header_codec=self.header_codec,
                             pathname=store_path.replace("\\", "/"), size=st.st_size,
                             filetype=FileType.REGULAR_FILE, perm=0o644,
                             mtime=int(st.st_mtime))
        lib.archive_entry_set_nlink(entry._entry_p, 1)
        lib.archive_entry_set_ino64(entry._entry_p, st.st_ino or 1)
        ffi.write_header(self._pointer, entry._entry_p)
        with open(path, "rb") as f:
            while True:
                data = f.read(1 << 16)
                if not data:
                    break
                ffi.write_data(self._pointer, data, len(data))
        ffi.write_finish_entry(self._pointer)

    add_abs_file.espdrop = True
    cls.add_abs_file = add_abs_file


def fix_ask_paths(cls):
    """Announce files as "./name" in /Ask (Windows).

    send_ask sets FileBomPath to os.path.join(".", name), i.e. ".\\name" on
    Windows, while the archive holds "./name" (fix_archive_writer), and the
    receiver checks the upload against what /Ask announced (the likely reason an
    iPad accepted such an /Ask and then reset the connection on the /Upload).
    This rewrites the paths in the /Ask plist on its way out; everything else in
    it stays as OpenDrop made it. Idempotent."""
    if getattr(cls.send_POST, "espdrop", False):
        return
    base = cls.send_POST

    def send_POST(self, url, body, *args, **kwargs):
        if url == "/Ask" and isinstance(body, (bytes, bytearray)):
            body = posix_bom_paths(body)
        return base(self, url, body, *args, **kwargs)

    send_POST.espdrop = True
    cls.send_POST = send_POST


def posix_bom_paths(ask_plist: bytes) -> bytes:
    """An /Ask body with "\\" in its FileBomPath values made "/" (unchanged
    bytes when there is none)."""
    import plistlib
    body = plistlib.loads(ask_plist)
    changed = False
    for f in body.get("Files") or []:
        bom = f.get("FileBomPath") if isinstance(f, dict) else None
        if isinstance(bom, str) and "\\" in bom:
            f["FileBomPath"] = bom.replace("\\", "/")
            changed = True
    return plistlib.dumps(body, fmt=plistlib.FMT_BINARY) if changed else ask_plist


# ---- TLS identity ----------------------------------------------------------

def identity_files(airdrop_dir="~/.opendrop"):
    keys = os.path.join(os.path.expanduser(airdrop_dir), "keys")
    return keys, os.path.join(keys, "certificate.pem"), os.path.join(keys, "key.pem")


def ensure_identity(name, airdrop_dir="~/.opendrop"):
    """Create OpenDrop's TLS key and self-signed certificate if they are missing.

    The same files and shape as OpenDrop's `openssl req -newkey rsa:2048 -nodes
    -x509 -days 365 -subj /CN=<name>` (config.py), made with
    python-cryptography. Returns "exists", "created", or "openssl" when
    cryptography is not installed (OpenDrop then runs openssl itself)."""
    keys, cert_file, key_file = identity_files(airdrop_dir)
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return "exists"
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return "openssl"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name or "OpenDrop")])
    now = datetime.datetime.now(datetime.timezone.utc)
    pub = key.public_key()
    # openssl's default v3_ca extensions for req -x509.
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(pub)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(pub),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    os.makedirs(keys, exist_ok=True)
    _write(key_file, key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()), private=True)
    _write(cert_file, cert.public_bytes(serialization.Encoding.PEM))
    return "created"


def _write(path, data, private=False):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---- espdrop check ---------------------------------------------------------

def _archive_selftest():
    """Pack two files the way OpenDrop's client does, read them back and
    extract them the way its server does. Returns (ok, detail, warnings)."""
    import io
    import tempfile
    import libarchive
    import libarchive.extract
    from opendrop.util import AbsArchiveWrite

    files = {"espdrop-check.txt": b"EspDrop libarchive check\n" * 50,
             "pr\u00fcfung-\u00e4.txt": b"non-ASCII name\n"}
    warnings = []
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        for fn, data in files.items():
            with open(os.path.join(src, fn), "wb") as f:
                f.write(data)
        stream = io.BytesIO()
        with libarchive.custom_writer(stream.write, "cpio", filter_name="gzip",
                                      archive_write_class=AbsArchiveWrite) as archive:
            for fn in files:
                archive.add_abs_file(os.path.join(src, fn), os.path.join(".", fn))
        blob = stream.getvalue()
        with libarchive.memory_reader(blob) as archive:
            got = [(e.pathname, e.mtime) for e in archive]
        names = [n for n, _ in got]
        want = [f"./{fn}" for fn in files]
        if names[:1] != want[:1]:
            return False, f"archive holds {names!r}, expected {want!r}", warnings
        mtime = int(os.stat(os.path.join(src, next(iter(files)))).st_mtime)
        if got[0][1] != mtime:          # e.g. a time_t size mismatch in the binding
            warnings.append(f"file time packed as {got[0][1]}, expected {mtime}")
        if names[1:] != want[1:]:
            warnings.append(f"non-ASCII name packed as {names[1:]!r}")
        cwd = os.getcwd()
        os.chdir(dst)                           # OpenDrop's server extracts into cwd
        try:
            libarchive.extract.extract_memory(blob)
        finally:
            os.chdir(cwd)
        for fn, data in files.items():
            p = os.path.join(dst, fn)
            if not os.path.isfile(p):
                if fn.isascii():
                    return False, f"extracted files: {os.listdir(dst)!r}", warnings
                warnings.append(f"non-ASCII name unpacked as {os.listdir(dst)!r}")
                continue
            with open(p, "rb") as f:
                if f.read() != data:
                    return False, f"{fn}: content differs after unpacking", warnings
    return True, f"cpio+gzip pack/read/extract ok ({len(blob)} bytes)", warnings


def xcb_missing():
    """Libraries Qt's X11 plugin needs that this system lacks (Linux), [] if
    none, None if that cannot be told. Wayland sessions do not need them; a
    build carries them (tools/build_app.py)."""
    if not sys.platform.startswith("linux"):
        return None
    import shutil
    import subprocess
    from .frozen import program_dir
    base = program_dir()
    if base:
        plugin = os.path.join(base, "PySide6", "qt-plugins", "platforms", "libqxcb.so")
    else:
        try:
            import PySide6
        except ImportError:
            return None
        plugin = os.path.join(os.path.dirname(PySide6.__file__), "Qt", "plugins", "platforms",
                              "libqxcb.so")
    if not os.path.isfile(plugin) or not shutil.which("ldd"):
        return None
    try:
        out = subprocess.run(["ldd", plugin], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted({ln.split()[0] for ln in out.splitlines() if "not found" in ln})


def debian_package(soname):
    """'libxcb-cursor.so.0' -> 'libxcb-cursor0' (Debian's naming)."""
    name, _, version = soname.partition(".so.")
    major = version.split(".")[0]
    return name + ("-" if name[-1:].isdigit() else "") + major


def check(out=print):
    """What `espdrop check` reports. Returns the number of problems."""
    import platform
    problems = 0

    def line(ok, what, detail):
        nonlocal problems
        tag = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        problems += ok is False
        out(f"  {tag} {what:<11} {detail}")

    from .frozen import compiled, program
    v = sys.version_info
    if compiled():                               # what autostart and the menus start
        line(True, "program", f"{program()} (Python {platform.python_version()} inside)")
    else:
        line(v >= (3, 9), "python", f"{platform.python_version()} {platform.architecture()[0]} "
                                    f"({sys.executable})")
    try:
        import serial
        from .transport import candidates
        ports = candidates()
        line(True, "pyserial", f"{serial.__version__}; ESP ports: {', '.join(ports) or 'none'}")
    except ImportError:
        line(None if os.name == "posix" else False, "pyserial",
             "not installed (needed on Windows; Linux falls back to the raw tty)")
    try:
        import zeroconf
        line(True, "zeroconf", zeroconf.__version__)
    except ImportError as e:
        line(False, "zeroconf", f"{e}; {INSTALL_HINT}")
    try:
        import PySide6
        line(True, "pyside6", f"{PySide6.__version__} (espdrop gui)")
    except ImportError:
        line(None, "pyside6", "not installed: the command line works, the desktop app "
                              "(espdrop gui) needs it")
    missing = xcb_missing()
    if missing:
        line(None, "x11", f"Qt's X11 plugin lacks {', '.join(missing)}: under X11 (Wayland "
                          f"does without) the window needs: sudo apt install "
                          f"{' '.join(debian_package(m) for m in missing)}")
    elif missing == []:
        line(True, "x11", "Qt's X11 plugin has what it needs")
    try:
        import esptool
        line(True, "esptool", f"{esptool.__version__} (espdrop flash, firmware from the app)")
    except ImportError:
        line(None, "esptool", "not installed: everything works, but the ESP's firmware "
                              "cannot be put on from here (pip install esptool)")
    try:
        path, detail = prepare_libarchive()
        line(True, "libarchive", detail)
    except ImportError as e:
        line(False, "libarchive", str(e))
        return problems                         # OpenDrop cannot load without it
    try:
        load_opendrop()
        import opendrop
        import opendrop.server
        from .frozen import compiled
        if compiled():
            # No source to look into (the marker is a comment); the build
            # refuses an OpenDrop without the patches (tools/build_app.py).
            patched, where = True, "checked when this program was built"
        else:
            with open(opendrop.server.__file__, encoding="utf-8") as f:
                patched = "trailing CRLF of the final chunk" in f.read()
            where = os.path.dirname(opendrop.__file__)
        line(patched, "opendrop", f"{opendrop.__version__}, "
                                  f"{'patched' if patched else 'NOT patched'} ({where})")
    except ImportError as e:
        line(False, "opendrop", str(e))
        return problems
    try:                                        # what AirDropConfig needs (and looks up so)
        ca = opendrop.config.resource_filename("opendrop", "certs/apple_root_ca.pem")
        line(os.path.isfile(ca), "root ca", ca if os.path.isfile(ca) else f"missing: {ca}")
    except Exception as e:                      # noqa: BLE001 - report, do not crash
        line(False, "root ca", f"{type(e).__name__}: {e}")
    try:
        ok, detail, warnings = _archive_selftest()
        line(ok, "archive", detail)
        for w in warnings:
            line(None, "archive", w)
    except Exception as e:                      # noqa: BLE001 - report, do not crash
        line(False, "archive", f"{type(e).__name__}: {e}")
    keys, cert_file, _key = identity_files()
    if os.path.exists(cert_file):
        line(True, "tls", f"certificate in {keys}")
    else:
        try:
            import cryptography  # noqa: F401
            line(True, "tls", f"will be made with python-cryptography in {keys}")
        except ImportError:
            import shutil
            line(bool(shutil.which("openssl")), "tls",
                 "OpenDrop will make it with openssl" if shutil.which("openssl")
                 else "neither python-cryptography nor openssl: pip install cryptography")
    return problems
