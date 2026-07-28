@echo off
REM ChaosOps Server installer wrapper for Windows (V2)
REM Calls bin\chaosops-installer.exe with all arguments.

setlocal
set "SCRIPT_DIR=%~dp0"
"%SCRIPT_DIR%bin\chaosops-installer.exe" %*
if %errorlevel% neq 0 exit /b %errorlevel%
