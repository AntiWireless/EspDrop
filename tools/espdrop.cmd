@echo off
rem EspDrop - start the host app on Windows, with the venv from tools\opendrop_install.py.
rem
rem   .\tools\espdrop.cmd check, status, find, send FILE..., receive
rem
rem Works in cmd and PowerShell (a .cmd needs no execution policy). The venv is
rem OPENDROP_VENV if set, else %USERPROFILE%\opendrop-venv.
rem
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright (C) 2026  EspDrop contributors
setlocal
set "VENV=%OPENDROP_VENV%"
if not defined VENV set "VENV=%USERPROFILE%\opendrop-venv"
if not exist "%VENV%\Scripts\python.exe" echo error: no OpenDrop venv in "%VENV%" - run: py "%~dp0opendrop_install.py" 1>&2 & exit /b 1
"%VENV%\Scripts\python.exe" "%~dp0espdrop.py" %*
exit /b %ERRORLEVEL%
