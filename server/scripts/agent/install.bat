@echo off
REM ChaosOps Agent Windows one-click installer (online dist download).
REM Usage: install.bat <server_url> <install_key>
REM        Example: install.bat https://chaosops.example.com ik_xxxx
REM
REM IMPORTANT: keep this file pure ASCII (English only). Windows cmd.exe parses
REM .bat files using the system code page, so any non-ASCII byte (for example
REM UTF-8 Chinese text) breaks parsing on Chinese Windows.
REM
REM NOTE: this installer takes POSITIONAL arguments, not NAME=value pairs.
REM cmd.exe splits unquoted batch arguments on '=' (as well as space/comma/semicolon),
REM so "install.bat SERVER_URL=x" would arrive as two arguments SERVER_URL and x.
REM Positional arguments avoid that pitfall.
REM
REM Flow: argument check -> Python check -> download dist package (install_key auth)
REM       -> sha256 verify -> extract -> venv offline install -> register (consumes
REM       install_key) -> write config.json -> register scheduled task.

setlocal EnableDelayedExpansion

REM --------------- Parse positional arguments ---------------
set "SERVER_URL=%~1"
set "INSTALL_KEY=%~2"
set "AGENT_DIR=%ProgramData%\ChaosOpsAgent"

if "%SERVER_URL%"=="" (
    echo ERROR: missing ^<server_url^> argument.
    echo Usage: install.bat ^<server_url^> ^<install_key^>
    echo Example: install.bat https://chaosops.example.com ik_xxxx
    exit /b 1
)
if "%INSTALL_KEY%"=="" (
    echo ERROR: missing ^<install_key^> argument.
    echo Usage: install.bat ^<server_url^> ^<install_key^>
    echo Example: install.bat https://chaosops.example.com ik_xxxx
    exit /b 1
)

REM Strip a trailing slash from the server URL.
if "%SERVER_URL:~-1%"=="/" set "SERVER_URL=%SERVER_URL:~0,-1%"

echo ChaosOps Agent installer
echo Server: %SERVER_URL%
echo.

REM --------------- Python environment check (native Windows only) ---------------
REM Prefer the Python Launcher (py), which manages native Windows builds only.
REM Fall back to python/python3 on PATH, rejecting Microsoft Store stubs
REM (WindowsApps) and MSYS2/Cygwin builds (their venvs are POSIX-style bin/
REM not Scripts/ and cannot run as a native Windows service).
set "PYTHON_CMD="

where py >nul 2>nul
if !errorlevel! equ 0 (
    for /f "tokens=2" %%v in ('py -3 --version 2^>^&1') do (
        if not defined PYTHON_CMD (
            set "PY_VER=%%v"
            if "!PY_VER:~0,1!"=="3" (
                set "PY_MINOR=!PY_VER:~2,2!"
                if !PY_MINOR! geq 10 set "PYTHON_CMD=py -3"
            )
        )
    )
)

if not defined PYTHON_CMD (
    for %%p in (python python3) do (
        if not defined PYTHON_CMD (
            for /f "delims=" %%f in ('where %%p 2^>nul') do (
                if not defined PYTHON_CMD (
                    set "CAND=%%f"
                    echo !CAND!| findstr /I /C:"WindowsApps" /C:"msys" /C:"cygwin" >nul || (
                        for /f "tokens=2" %%v in ('"!CAND!" --version 2^>^&1') do (
                            if not defined PYTHON_CMD (
                                set "PY_VER=%%v"
                                if "!PY_VER:~0,1!"=="3" (
                                    set "PY_MINOR=!PY_VER:~2,2!"
                                    if !PY_MINOR! geq 10 set "PYTHON_CMD=!CAND!"
                                )
                            )
                        )
                    )
                )
            )
        )
    )
)

if not defined PYTHON_CMD (
    echo ERROR: native Windows Python 3.10+ not found.
    echo        Install Python 3.10 or newer from python.org ^(which provides the
    echo        'py' launcher and a native Windows build^).
    exit /b 1
)
for /f "tokens=2" %%v in ('%PYTHON_CMD% --version 2^>^&1') do echo Using Python: %PYTHON_CMD% (%%v)

REM --------------- Download dist package (install_key is verified, not consumed) ---------------
set "PACKAGE_FILE=%TEMP%\chaosops-agent.tar.gz"
set "CHECKSUM_FILE=%TEMP%\chaosops-agent.checksum"
set "STAGING_DIR=%TEMP%\chaosops-agent-staging"

echo Downloading Agent dist package...
powershell -Command "$ProgressPreference='SilentlyContinue'; try { $resp = Invoke-WebRequest -Uri '%SERVER_URL%/api/v1/agents/dist/latest?install_key=%INSTALL_KEY%' -OutFile '%PACKAGE_FILE%' -UseBasicParsing -PassThru; $checksum = $resp.Headers['X-Agent-Checksum']; if (-not $checksum) { Write-Error 'Response missing X-Agent-Checksum header'; exit 1 }; $checksum | Out-File -Encoding ascii -NoNewline '%CHECKSUM_FILE%'; exit 0 } catch { Write-Error $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo ERROR: failed to download Agent dist package. Check SERVER_URL and INSTALL_KEY.
    exit /b 1
)

REM --------------- sha256 verification ---------------
echo Verifying dist package sha256...
powershell -Command "$expected = (Get-Content '%CHECKSUM_FILE%' -Raw).Trim().ToLower(); $actual = (Get-FileHash '%PACKAGE_FILE%' -Algorithm SHA256).Hash.ToLower(); if ($expected -ne $actual) { Write-Error ('sha256 mismatch (expected ' + $expected + ', got ' + $actual + ')'); exit 1 }"
if errorlevel 1 (
    echo ERROR: dist package sha256 verification failed, package may be corrupted or tampered.
    exit /b 1
)
echo sha256 verification passed

REM --------------- Extract dist package ---------------
REM Windows 10+ ships tar.exe which can unpack tar.gz. Package top level is
REM chaosops-agent-<ver>/agent/...
echo Extracting Agent dist package...
if exist "%STAGING_DIR%" rmdir /S /Q "%STAGING_DIR%"
mkdir "%STAGING_DIR%"
tar -xzf "%PACKAGE_FILE%" -C "%STAGING_DIR%"
if errorlevel 1 (
    echo ERROR: extraction failed ^(Windows 10 1803+ tar is required^).
    exit /b 1
)

REM Preserve agent/ package structure (code uses from agent.app... absolute imports)
if not exist "%AGENT_DIR%" mkdir "%AGENT_DIR%"
set "PKG_AGENT_DIR="
REM Use dir /ad /b on the staging root to avoid wildcard parser quirks with
REM directory names containing dots (e.g. chaosops-agent-0.1.0).
for /f "delims=" %%d in ('dir /ad /b "%STAGING_DIR%" 2^>nul') do (
    if exist "%STAGING_DIR%\%%d\agent" set "PKG_AGENT_DIR=%STAGING_DIR%\%%d\agent"
)
if "%PKG_AGENT_DIR%"=="" (
    echo ERROR: dist package structure is invalid, no chaosops-agent-*\agent directory found.
    exit /b 1
)
if exist "%AGENT_DIR%\agent" rmdir /S /Q "%AGENT_DIR%\agent"
xcopy /E /I /Q "%PKG_AGENT_DIR%" "%AGENT_DIR%\agent" >nul
if errorlevel 1 (
    echo ERROR: failed to copy Agent files.
    exit /b 1
)

REM --------------- Create venv and install dependencies (offline wheels + online fallback) ---------------
echo Creating Python virtual environment and installing dependencies...
%PYTHON_CMD% -m venv "%AGENT_DIR%\.venv"
if errorlevel 1 (
    echo ERROR: failed to create virtual environment.
    exit /b 1
)
set "VENV_PIP=%AGENT_DIR%\.venv\Scripts\pip.exe"

REM Bundled wheels are built on Linux; binary wheels such as psutil are
REM manylinux-only and cannot be installed on Windows. Detect that and fall back
REM to downloading Windows-specific packages from PyPI (internet required).
set "NEED_ONLINE=0"
if not exist "%AGENT_DIR%\agent\wheels" set "NEED_ONLINE=1"
if exist "%AGENT_DIR%\agent\wheels\" (
    for %%F in ("%AGENT_DIR%\agent\wheels\*.whl") do (
        echo %%~nxF | findstr /I /C:"manylinux" >nul && set "NEED_ONLINE=1"
    )
)
if "!NEED_ONLINE!"=="1" (
    echo NOTE: bundled wheels include Linux-only builds ^(e.g. psutil^).
    echo       Using bundled pure-Python wheels and downloading Windows-specific
    echo       packages from PyPI. An internet connection is required.
    "%VENV_PIP%" install --disable-pip-version-check --find-links "%AGENT_DIR%\agent\wheels" -r "%AGENT_DIR%\agent\requirements.txt"
) else (
    echo Installing dependencies from bundled offline wheels...
    "%VENV_PIP%" install --disable-pip-version-check --no-index --find-links "%AGENT_DIR%\agent\wheels" -r "%AGENT_DIR%\agent\requirements.txt"
)
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    exit /b 1
)

REM --------------- Register Agent (consumes install_key) ---------------
echo Registering with the server...
set "REGISTER_URL=%SERVER_URL%/api/v1/agents/register"

powershell -Command "try { $body = @{install_key='%INSTALL_KEY%';hostname=$env:COMPUTERNAME;os='windows';arch=$env:PROCESSOR_ARCHITECTURE;version='0.1.0'} | ConvertTo-Json; $resp = Invoke-WebRequest -Uri '%REGISTER_URL%' -Method POST -ContentType 'application/json' -Body $body -UseBasicParsing; $resp.Content | Out-File -Encoding utf8 '%AGENT_DIR%\register_response.json'; exit 0 } catch { Write-Error $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo ERROR: registration failed. Check SERVER_URL and INSTALL_KEY.
    exit /b 1
)

REM Parse response and save config (including auto_update block, same as Linux installer)
%PYTHON_CMD% -c "import json; d=json.load(open(r'%AGENT_DIR%\register_response.json', encoding='utf-8-sig')); json.dump({'server_url':'%SERVER_URL%','agent_token':d['agent_token'],'node_id':d['node_id'],'heartbeat_interval':d['heartbeat_interval'],'auto_update':{'enabled':True,'mode':'manual','check_interval_hours':24,'channel':'stable'}}, open(r'%AGENT_DIR%\config.json', 'w', encoding='utf-8'), indent=2)"
if errorlevel 1 (
    echo ERROR: failed to write config.json.
    exit /b 1
)
del /Q "%AGENT_DIR%\register_response.json" >nul 2>nul

echo Config saved to: %AGENT_DIR%\config.json

REM --------------- Clean up temporary files ---------------
del /Q "%PACKAGE_FILE%" "%CHECKSUM_FILE%" >nul 2>nul
rmdir /S /Q "%STAGING_DIR%" >nul 2>nul

REM --------------- Register Windows scheduled task (auto-start + background) ---------------
REM Write a launcher that sets the working directory first: agent code uses
REM absolute imports (from agent.app...) and needs cwd=AGENT_DIR to resolve the
REM agent package.
echo Registering Windows scheduled task (auto-start at boot)...
(
echo @echo off
echo cd /d "%AGENT_DIR%"
echo "%AGENT_DIR%\.venv\Scripts\python.exe" -m agent.app.main --config-dir "%AGENT_DIR%"
) > "%AGENT_DIR%\start-agent.bat"

REM SYSTEM account + highest privileges + boot trigger; /f overwrites old task
schtasks /create /tn "ChaosOpsAgent" /tr "\"%AGENT_DIR%\start-agent.bat\"" /sc onstart /ru SYSTEM /rl HIGHEST /f >nul 2>nul
if errorlevel 1 (
    echo WARNING: scheduled task registration failed ^(may need Administrator rights^). You can start manually: "%AGENT_DIR%\start-agent.bat"
) else (
    schtasks /run /tn "ChaosOpsAgent" >nul 2>nul
    echo Scheduled task ChaosOpsAgent registered and started ^(SYSTEM account, auto-start at boot^).
)

REM Download the uninstaller into the agent directory (non-fatal on failure).
powershell -Command "try { Invoke-WebRequest -Uri '%SERVER_URL%/api/v1/agents/uninstall.bat' -OutFile '%AGENT_DIR%\uninstall.bat' -UseBasicParsing; exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
    echo WARNING: failed to download uninstall.bat. You can fetch it anytime from %SERVER_URL%/api/v1/agents/uninstall.bat
)

echo.
echo ChaosOps Agent installation complete.
exit /b 0
