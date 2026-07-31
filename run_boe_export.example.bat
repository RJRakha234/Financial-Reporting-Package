@echo off
setlocal enabledelayedexpansion
REM ---------------------------------------------------------------------------
REM  Copy this file to run_boe_export.bat, fill in the values, then run it.
REM  run_boe_export.bat is git-ignored, so your settings stay off the repository.
REM
REM  Quotes around each "NAME=value" matter: the values contain & and ?, which
REM  cmd would otherwise treat as command separators.
REM ---------------------------------------------------------------------------


REM --- Python -----------------------------------------------------------------
REM  Leave VENV_DIR empty to search the usual locations, or point it at a
REM  virtual environment (the folder containing Scripts\python.exe). On a
REM  managed machine where system-wide installs are restricted, a venv is
REM  usually the only workable option.
REM
REM  To find it: activate the environment, run  where python , and use
REM  everything before \Scripts.

set "VENV_DIR="

if "%VENV_DIR%"=="" (
    for %%D in (
        "%~dp0myvenv" "%~dp0.venv" "%~dp0venv"
        "%USERPROFILE%\myvenv" "%USERPROFILE%\.venv"
    ) do (
        if exist "%%~D\Scripts\python.exe" set "VENV_DIR=%%~D"
    )
)

if "%VENV_DIR%"=="" (
    set "PY=python"
    echo No virtual environment found; using the system Python.
) else (
    set "PY=%VENV_DIR%\Scripts\python.exe"
    echo Using Python from: !PY!
)

REM  Install the dependencies on first run rather than failing on an import.
"%PY%" -c "import requests" 2>nul || "%PY%" -m pip install requests
"%PY%" -c "import selenium" 2>nul || "%PY%" -m pip install selenium


REM --- The BusinessObjects server ---------------------------------------------
REM  NOT the portal address. Find it by opening the report, then running
REM  location.href in the DevTools console with the report frame selected.

set "BOE_BASE=https://boe.example.com"


REM --- The document -----------------------------------------------------------
REM  CUID comes from the report's Properties panel (next to Name and ID).

set "BOE_DOC_CUID=PUT_THE_CUID_HERE"


REM --- Output format ----------------------------------------------------------
REM   E = Excel (.xls)   X = Excel 2007+ (.xlsx)   P = PDF   C = CSV

set "BOE_FORMAT=E"


REM --- Prompt values ----------------------------------------------------------
REM  Semicolon-separated Name=Value pairs. Names must match the Prompts dialog
REM  exactly. Use a comma between values for a multi-value prompt.

set "BOE_PROMPTS=Consol Group=G_GRUP;Fiscal Year=2027;From Period=1;From To=3"


REM --- Sign-in ----------------------------------------------------------------
REM  The portal is loaded first so corporate SSO can run, then the BOE server.
REM  Leave SAP_USER / SAP_PASS commented out unless a logon form appears.

set "SAP_PORTAL_BASE=https://portal.example.com"
REM set "SAP_USER=your_user"
REM set "SAP_PASS=your_password"


REM --- Optional ---------------------------------------------------------------
REM  Only set BOE_OPENDOC_PATH if the default path does not work. Default:
REM     /BOE/OpenDocument/opendoc/openDocument.jsp
REM set "BOE_OPENDOC_PATH=/BOE/OpenDocument/opendoc/openDocument.jsp"
REM set "BOE_DOWNLOAD_DIR=%~dp0sap_downloads"


REM --- Run --------------------------------------------------------------------
REM  Check the URL first:
REM     "%PY%" "%~dp0opendoc_export.py" --print-url
REM  Add --browser if the direct request is refused.

"%PY%" "%~dp0opendoc_export.py" --output "%~dp0sap_downloads\consolidated_pl.xls"

pause
