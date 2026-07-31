@echo off
REM ---------------------------------------------------------------------------
REM  Copy this file to run_export.bat, fill in the values, then run it.
REM  run_export.bat is git-ignored, so your settings stay off the repository.
REM
REM  Quotes around each "NAME=value" matter: the report path contains & and ?,
REM  which cmd would otherwise treat as command separators.
REM ---------------------------------------------------------------------------

REM --- Required: where the portal lives, and the report's iView path ---------
REM  Get both from the src of the report's isolatedWorkArea iframe.
REM  See AUTOMATION.md, Stage 1. Drop windowId / NavMode / PrevNavTarget.

set "SAP_PORTAL_BASE=https://portal.example.com"
set "SAP_REPORT_PATH=/irj/servlet/prt/portal/prtroot/pcd!3aportal_content!2f...!2fMy_Report?ExecuteLocally=true&sapDocumentRenderingMode=Edge"

REM --- Optional: only needed if the portal shows a login form ----------------
REM  Under corporate SSO, leave these commented out.

REM set "SAP_USER=your_user"
REM set "SAP_PASS=your_password"

REM --- Optional: everything below has a sensible default --------------------

REM set "SAP_DOWNLOAD_DIR=%~dp0sap_downloads"
REM set "SAP_BROWSER=edge"
REM set "SAP_TIMEOUT=90"

REM --- Run -------------------------------------------------------------------

python "%~dp0sap_export.py" --output "%~dp0sap_downloads\trial_balance.xls"

pause
