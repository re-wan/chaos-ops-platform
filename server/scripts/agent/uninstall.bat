@echo off
REM ChaosOps Agent Windows uninstaller.
REM Usage: uninstall.bat   (run as Administrator)
REM
REM IMPORTANT: keep this file pure ASCII (English only). Windows cmd.exe parses
REM .bat files using the system code page, so any non-ASCII byte breaks parsing
REM on Chinese Windows.
REM
REM Flow: admin check -> delete scheduled task -> remove install directory.

setlocal EnableDelayedExpansion

set "AGENT_DIR=%ProgramData%\ChaosOpsAgent"

echo ChaosOps Agent uninstaller
echo.

REM --------------- Administrator check ---------------
net session >nul 2>nul
if errorlevel 1 (
    echo ERROR: uninstall requires Administrator privileges.
    echo Right-click and select "Run as administrator".
    exit /b 1
)

REM --------------- Delete scheduled task ---------------
schtasks /query /tn "ChaosOpsAgent" >nul 2>nul
if not errorlevel 1 (
    schtasks /end /tn "ChaosOpsAgent" >nul 2>nul
    schtasks /delete /tn "ChaosOpsAgent" /f >nul 2>nul
    if errorlevel 1 (
        echo WARN: failed to delete scheduled task "ChaosOpsAgent".
        echo Delete it manually in Task Scheduler.
    ) else (
        echo Removed scheduled task "ChaosOpsAgent".
    )
) else (
    echo Scheduled task "ChaosOpsAgent" not found, skipping.
)

REM --------------- Remove install directory ---------------
if exist "%AGENT_DIR%" (
    rmdir /s /q "%AGENT_DIR%"
    if exist "%AGENT_DIR%" (
        echo WARN: failed to remove %AGENT_DIR%.
        echo Stop any running agent processes and delete it manually.
    ) else (
        echo Removed %AGENT_DIR%.
    )
) else (
    echo %AGENT_DIR% not found, skipping.
)

echo.
echo ChaosOps Agent uninstall complete.
echo Note: the node record still exists in the server console.
echo Delete it manually in "Nodes" if no longer needed.
endlocal
