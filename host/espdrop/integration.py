# EspDrop - hooks into the desktop: start at login, send from the file manager.
#
# Windows (per user, no admin rights):
#   - autostart: a value in HKCU\Software\Microsoft\Windows\CurrentVersion\Run
#   - "Senden an": a shortcut in %APPDATA%\Microsoft\Windows\SendTo (all
#     selected files in one call), and "Mit EspDrop senden" in the classic
#     right-click menu (HKCU\Software\Classes\*\shell; one call per file, the
#     running app collects them). On Windows 11 both sit under "Weitere
#     Optionen anzeigen". The modern share sheet ("Teilen") only lists apps
#     with package identity (MSIX); that comes with a packaged build.
# Linux (XDG):
#   - autostart: ~/.config/autostart/espdrop.desktop
#   - "Öffnen mit": ~/.local/share/applications/espdrop-send.desktop
#   - the application menu: ~/.local/share/applications/espdrop.desktop, always
#     (gui.py names it as the windows' desktop file, so Wayland docks show our
#     icon)
#
# Each entry starts the app as `espdrop gui ...` with the interpreter and
# checkout it runs from now; the app forwards to an already running copy
# (gui.py, single instance), so a second launch never fights for the port.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import subprocess
import sys

from .i18n import N_, _

IS_WINDOWS = os.name == "nt"
try:
    import winreg                              # Windows only
except ImportError:
    winreg = None

APP = "EspDrop"
ICON = None              # our icon file, .ico on Windows (gui.py writes it at start)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MENU_KEY = r"Software\Classes\*\shell\EspDrop"
SENDTO_NAME = "EspDrop (AirDrop).lnk"
MENU_TEXT = N_("Mit EspDrop senden")          # in the language the app runs in


# The window has no console when started by a double-click (Windows): its
# standard handles are not valid ones, and a child that would inherit them
# fails with WinError 6 "The handle is invalid". Every child gets these.
NO_STDIO = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL}


def launch_command(*args):
    """argv that starts the app with these arguments, as it runs now: the
    venv's pythonw on Windows (no console window), tools/espdrop.py of this
    checkout; the compiled program starts itself."""
    from .frozen import program
    exe = program()
    if exe:
        return [exe, "gui", *args]
    exe = sys.executable
    if IS_WINDOWS:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(w):
            exe = w
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [exe, os.path.join(root, "tools", "espdrop.py"), "gui", *args]


def data_dir():
    """Per-user folder for files the desktop needs from us (the icon)."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, APP)
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "espdrop")


def supported():
    """What this system offers: {"autostart": bool, "send": label or None,
    "send_hint": what the label switches on}."""
    if IS_WINDOWS:
        return {"autostart": winreg is not None,
                "send": N_("Im Rechtsklickmenü des Explorers anbieten"),
                "send_hint": N_("„Senden an → EspDrop (AirDrop)“ und „Mit EspDrop senden“")}
    if sys.platform.startswith("linux"):
        return {"autostart": True, "send": N_("Im Dateimanager anbieten"),
                "send_hint": N_("„Öffnen mit → AirDrop (EspDrop)“")}
    return {"autostart": False, "send": None, "send_hint": None}


# ---- autostart ---------------------------------------------------------------

def autostart_enabled():
    if IS_WINDOWS:
        return _reg_get(RUN_KEY, APP) is not None
    return os.path.isfile(_xdg_autostart())


def set_autostart(on):
    """Start the app hidden (tray, receiving) at login, or not."""
    cmd = launch_command("--background")
    if IS_WINDOWS:
        if on:
            _reg_set(RUN_KEY, APP, subprocess.list2cmdline(cmd))
        else:
            _reg_delete_value(RUN_KEY, APP)
        return
    path = _xdg_autostart()
    if on:
        _write_desktop(path, {
            "Type": "Application", "Name": APP,
            "Comment": _("AirDrop über den ESP32 – empfangsbereit im Hintergrund"),
            "Exec": _exec_line(cmd), "Icon": ICON or "document-send", "Terminal": "false",
            "X-GNOME-Autostart-enabled": "true"})
    elif os.path.exists(path):
        os.remove(path)


# ---- sending from the file manager -------------------------------------------

def send_menu_enabled():
    if IS_WINDOWS:
        return os.path.isfile(_sendto_path()) or _reg_get(MENU_KEY, "") is not None
    return os.path.isfile(_xdg_send())


def set_send_menu(on):
    if IS_WINDOWS:
        _set_windows_send(on)
        return
    path = _xdg_send()
    if on:
        changed = _write_desktop(path, {
            "Type": "Application", "Name": "AirDrop (EspDrop)", "Icon": ICON or "document-send",
            "Comment": _("Dateien per AirDrop an ein iPhone/iPad senden"),
            "Exec": _exec_line(launch_command("--send")) + " %F",
            "Terminal": "false", "NoDisplay": "true",
            # Common types; "Open with other application" lists the app for any.
            "MimeType": ";".join(LINUX_MIME) + ";"})
    elif os.path.exists(path):
        os.remove(path)
        changed = True
    else:
        changed = False
    if changed:
        _refresh_desktop_db(os.path.dirname(path))


LINUX_MIME = ["application/octet-stream", "application/pdf", "application/zip",
              "text/plain", "image/jpeg", "image/png", "image/gif", "image/heic",
              "image/webp", "video/mp4", "video/quicktime", "audio/mpeg"]


def _set_windows_send(on):
    cmd = launch_command("--send")
    lnk = _sendto_path()
    if on:
        # The right-click entry first: plain registry values, it works even
        # where PowerShell below is blocked (the error still reaches the caller).
        _reg_set(MENU_KEY, "", _(MENU_TEXT))
        _reg_set(MENU_KEY, "Icon", ICON or cmd[0])
        _reg_set(MENU_KEY + r"\command", "", subprocess.list2cmdline(cmd) + ' "%1"')
        # Explorer appends the selected files to the shortcut's arguments.
        # WScript.Shell writes the .lnk; the values go in through the
        # environment, so no quoting of paths inside PowerShell code.
        env = dict(os.environ, ESPDROP_LNK=lnk, ESPDROP_EXE=cmd[0],
                   ESPDROP_ARGS=subprocess.list2cmdline(cmd[1:]),
                   ESPDROP_DIR=os.path.dirname(cmd[1]) if len(cmd) > 1 else "",
                   ESPDROP_ICON=ICON or "", ESPDROP_DESC=_("Per AirDrop senden (EspDrop)"))
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ESPDROP_LNK);"
                  "$s.TargetPath = $env:ESPDROP_EXE; $s.Arguments = $env:ESPDROP_ARGS;"
                  "$s.WorkingDirectory = $env:ESPDROP_DIR;"
                  "if ($env:ESPDROP_ICON) { $s.IconLocation = $env:ESPDROP_ICON + ',0' };"
                  "$s.Description = $env:ESPDROP_DESC; $s.Save()")
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        if os.path.exists(lnk):
            os.remove(lnk)
        _reg_delete_tree(MENU_KEY)


def _sendto_path():
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "SendTo", SENDTO_NAME)


def set_launcher():
    """Linux: the app in the desktop's application menu (not a choice: it is
    how one starts an installed program there)."""
    if IS_WINDOWS or not sys.platform.startswith("linux"):
        return
    path = _xdg_launcher()
    if _write_desktop(path, {
            "Type": "Application", "Name": APP, "GenericName": "AirDrop",
            "Comment": _("AirDrop mit iPhone, iPad und Mac über den ESP32-C5"),
            "Exec": _exec_line(launch_command()), "Icon": ICON or "document-send",
            "Terminal": "false", "Categories": "Network;FileTransfer;",
            "Keywords": "AirDrop;iPhone;iPad;Mac;teilen;senden;",
            "StartupWMClass": APP}):
        _refresh_desktop_db(os.path.dirname(path))


# ---- the installed program (install.py) -----------------------------------------

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\EspDrop"
START_MENU_NAME = "EspDrop.lnk"


def _start_menu_path():
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs",
                        START_MENU_NAME)


def set_program(exe, version, on=True):
    """Windows: the start menu entry and the entry in "Installed apps"
    (Apps & features), whose "Uninstall" runs `exe uninstall`. Per user, no
    admin rights."""
    if not IS_WINDOWS:
        return
    lnk = _start_menu_path()
    if not on:
        if os.path.exists(lnk):
            os.remove(lnk)
        _reg_delete_tree(UNINSTALL_KEY)
        return
    _reg_set(UNINSTALL_KEY, "DisplayName", APP)
    _reg_set(UNINSTALL_KEY, "DisplayVersion", version)
    _reg_set(UNINSTALL_KEY, "Publisher", "EspDrop contributors")
    _reg_set(UNINSTALL_KEY, "DisplayIcon", exe)
    _reg_set(UNINSTALL_KEY, "InstallLocation", os.path.dirname(exe))
    _reg_set(UNINSTALL_KEY, "UninstallString", subprocess.list2cmdline([exe, "uninstall"]))
    _reg_set(UNINSTALL_KEY, "URLInfoAbout", "https://github.com/AntiWireless/EspDrop")
    _reg_set(UNINSTALL_KEY, "NoModify", 1)
    _reg_set(UNINSTALL_KEY, "NoRepair", 1)
    try:
        _reg_set(UNINSTALL_KEY, "EstimatedSize", os.path.getsize(exe) // 1024)
    except OSError:
        pass
    env = dict(os.environ, ESPDROP_LNK=lnk, ESPDROP_EXE=exe,
               ESPDROP_DIR=os.path.dirname(exe),
               ESPDROP_DESC=_("AirDrop mit iPhone, iPad und Mac über den ESP32-C5"))
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ESPDROP_LNK);"
              "$s.TargetPath = $env:ESPDROP_EXE; $s.WorkingDirectory = $env:ESPDROP_DIR;"
              "$s.IconLocation = $env:ESPDROP_EXE + ',0';"
              "$s.Description = $env:ESPDROP_DESC; $s.Save()")
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def remove_all():
    """Every entry this app made on the desktop: autostart, the file manager
    entry, the application menu, the start menu and "Installed apps".
    Returns what failed."""
    errors = []
    for fn in (lambda: set_autostart(False), lambda: set_send_menu(False),
               _remove_launcher, lambda: set_program(None, None, on=False)):
        try:
            fn()
        except Exception as e:                 # noqa: BLE001 - go on with the rest
            errors.append(str(e))
    return errors


def _remove_launcher():
    if IS_WINDOWS:
        return
    path = _xdg_launcher()
    if os.path.exists(path):
        os.remove(path)
        _refresh_desktop_db(os.path.dirname(path))


def refresh():
    """Keep enabled entries pointing at the app as it runs now: a checkout or
    venv that moved, a new icon. Rewrites only what differs (on Windows the
    "Senden an" shortcut costs a PowerShell start)."""
    set_launcher()
    cmd = launch_command("--background")
    if autostart_enabled():
        if IS_WINDOWS:
            if _reg_get(RUN_KEY, APP) != subprocess.list2cmdline(cmd):
                set_autostart(True)
        else:
            set_autostart(True)
    if send_menu_enabled():
        if IS_WINDOWS:
            want = subprocess.list2cmdline(launch_command("--send")) + ' "%1"'
            if (_reg_get(MENU_KEY + r"\command", "") != want
                    or _reg_get(MENU_KEY, "") != _(MENU_TEXT)          # another language
                    or _reg_get(MENU_KEY, "Icon") != (ICON or launch_command()[0])
                    or not os.path.isfile(_sendto_path())):
                set_send_menu(True)
        else:
            set_send_menu(True)


def apply_defaults():
    """First start: autostart and the file manager entry are on by default
    (the user can switch them off; this runs only once). Returns what failed."""
    errors = []
    try:
        set_launcher()
    except OSError as e:
        errors.append(f"set_launcher: {e}")
    caps = supported()
    for ok, fn in ((caps["autostart"], set_autostart), (caps["send"], set_send_menu)):
        if ok:
            try:
                fn(True)
            except Exception as e:             # noqa: BLE001 - reported, not fatal
                errors.append(f"{fn.__name__}: {e}")
    return errors


# ---- registry (HKCU only) ------------------------------------------------------

def _reg_get(key, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _reg_set(key, name, value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        if isinstance(value, int):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)
        else:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _reg_delete_value(key, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def _reg_delete_tree(key):
    """Delete key and its subkeys (DeleteKey only removes empty keys)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
        for sub in subs:
            _reg_delete_tree(key + "\\" + sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except OSError:
        pass


# ---- XDG ------------------------------------------------------------------------

def _xdg_autostart():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "espdrop.desktop")


def _xdg_launcher():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "applications", "espdrop.desktop")


def _xdg_send():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "applications", "espdrop-send.desktop")


def _exec_quote(arg):
    """One argument for a desktop entry's Exec line (freedesktop spec)."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def _exec_line(argv):
    return " ".join(_exec_quote(a) for a in argv).replace("%", "%%")


def _write_desktop(path, fields):
    """Write a desktop entry; False when it already had this content."""
    text = "[Desktop Entry]\n" + "".join(f"{k}={v}\n" for k, v in fields.items())
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


def _refresh_desktop_db(directory):
    """File managers find "Open with" apps through mimeinfo.cache."""
    try:
        subprocess.run(["update-desktop-database", directory], capture_output=True,
                       stdin=subprocess.DEVNULL,
                       timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
