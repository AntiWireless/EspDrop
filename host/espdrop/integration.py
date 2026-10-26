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

IS_WINDOWS = os.name == "nt"
try:
    import winreg                              # Windows only
except ImportError:
    winreg = None

APP = "EspDrop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MENU_KEY = r"Software\Classes\*\shell\EspDrop"
SENDTO_NAME = "EspDrop (AirDrop).lnk"
MENU_TEXT = "Mit EspDrop senden"


def launch_command(*args):
    """argv that starts the app with these arguments, as it runs now: the
    venv's pythonw on Windows (no console window), tools/espdrop.py of this
    checkout; a frozen build starts itself."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "gui", *args]
    exe = sys.executable
    if IS_WINDOWS:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(w):
            exe = w
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [exe, os.path.join(root, "tools", "espdrop.py"), "gui", *args]


def supported():
    """What this system offers: {"autostart": bool, "send": label or None}."""
    if IS_WINDOWS:
        return {"autostart": winreg is not None, "send": "Im Explorer-Menü anbieten "
                                                        "(„Senden an“)"}
    if sys.platform.startswith("linux"):
        return {"autostart": True, "send": "Im Dateimanager anbieten („Öffnen mit“)"}
    return {"autostart": False, "send": None}


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
            "Comment": "AirDrop über den ESP32 – empfangsbereit im Hintergrund",
            "Exec": _exec_line(cmd), "Terminal": "false",
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
        _write_desktop(path, {
            "Type": "Application", "Name": "AirDrop (EspDrop)", "Icon": "document-send",
            "Comment": "Dateien per AirDrop an ein iPhone/iPad senden",
            "Exec": _exec_line(launch_command("--send")) + " %F",
            "Terminal": "false", "NoDisplay": "true",
            # Common types; "Open with other application" lists the app for any.
            "MimeType": ";".join(LINUX_MIME) + ";"})
    elif os.path.exists(path):
        os.remove(path)
    _refresh_desktop_db(os.path.dirname(path))


LINUX_MIME = ["application/octet-stream", "application/pdf", "application/zip",
              "text/plain", "image/jpeg", "image/png", "image/gif", "image/heic",
              "image/webp", "video/mp4", "video/quicktime", "audio/mpeg"]


def _set_windows_send(on):
    cmd = launch_command("--send")
    lnk = _sendto_path()
    if on:
        # Explorer appends the selected files to the shortcut's arguments.
        # WScript.Shell writes the .lnk; the values go in through the
        # environment, so no quoting of paths inside PowerShell code.
        env = dict(os.environ, ESPDROP_LNK=lnk, ESPDROP_EXE=cmd[0],
                   ESPDROP_ARGS=subprocess.list2cmdline(cmd[1:]),
                   ESPDROP_DIR=os.path.dirname(cmd[1]) if len(cmd) > 1 else "")
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ESPDROP_LNK);"
                  "$s.TargetPath = $env:ESPDROP_EXE; $s.Arguments = $env:ESPDROP_ARGS;"
                  "$s.WorkingDirectory = $env:ESPDROP_DIR;"
                  "$s.Description = 'Per AirDrop senden (EspDrop)'; $s.Save()")
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _reg_set(MENU_KEY, "", MENU_TEXT)
        _reg_set(MENU_KEY, "Icon", cmd[0])
        _reg_set(MENU_KEY + r"\command", "", subprocess.list2cmdline(cmd) + ' "%1"')
    else:
        if os.path.exists(lnk):
            os.remove(lnk)
        _reg_delete_tree(MENU_KEY)


def _sendto_path():
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "SendTo", SENDTO_NAME)


# ---- registry (HKCU only) ------------------------------------------------------

def _reg_get(key, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _reg_set(key, name, value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\n")
        for k, v in fields.items():
            f.write(f"{k}={v}\n")
    os.replace(tmp, path)


def _refresh_desktop_db(directory):
    """File managers find "Open with" apps through mimeinfo.cache."""
    try:
        subprocess.run(["update-desktop-database", directory], capture_output=True,
                       timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
