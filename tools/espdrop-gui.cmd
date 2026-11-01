@echo off
rem EspDrop - start the desktop app on Windows, without a console window.
rem
rem   .\tools\espdrop-gui.cmd          (or double-click it in Explorer)
rem
rem Uses the venv from tools\opendrop_install.py: OPENDROP_VENV if set, else
rem %USERPROFILE%\opendrop-venv.
rem
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright (C) 2026  EspDrop contributors
setlocal
set "VENV=%OPENDROP_VENV%"
if not defined VENV set "VENV=%USERPROFILE%\opendrop-venv"
if not exist "%VENV%\Scripts\pythonw.exe" echo error: no OpenDrop venv in "%VENV%" - run: py "%~dp0opendrop_install.py" 1>&2 & pause & exit /b 1
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0espdrop.py" gui %*
