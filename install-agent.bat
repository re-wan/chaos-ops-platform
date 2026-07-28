@echo off
REM ChaosOps Agent Windows installer (offline package).
REM Usage: install-agent.bat <server_url> <install_key>
REM        Example: install-agent.bat https://chaosops.example.com ik_xxxx
REM
REM IMPORTANT: keep this file pure ASCII (English only). Windows cmd.exe parses
REM .bat files using the system code page, so any non-ASCII byte (for example
REM UTF-8 Chinese text) breaks parsing on Chinese Windows. The build script
REM asserts this file stays ASCII; do not add non-ASCII characters.
REM
REM NOTE: this installer takes POSITIONAL arguments, not NAME=value pairs.
REM cmd.exe splits unquoted batch arguments on '=' (as well as space/comma/semicolon),
REM so "install-agent.bat SERVER_URL=x" would arrive as two arguments SERVER_URL and x.
REM Positional arguments avoid that pitfall.
REM
REM Flow: parse args -> locate bundled agent source (dual layout) -> Python 3.10+
REM       check -> copy agent as a package -> venv -> install dependencies
REM       (offline wheels with online fallback for non-Windows wheels) ->
REM       register -> write config.json -> scheduled task (auto-start, correct
REM       working directory).

setlocal EnableDelayedExpansion

REM --------------- Parse positional arguments ---------------
set "SERVER_URL=%~1"
set "INSTALL_KEY=%~2"
set "INSTALL_DIR=%ProgramData%\ChaosOpsAgent"

if "%SERVER_URL%"=="" (
    echo ERROR: missing ^<server_url^> argument.
    echo Usage: install-agent.bat ^<server_url^> ^<install_key^>
    echo Example: install-agent.bat https://chaosops.example.com ik_xxxx
    exit /b 1
)
if "%INSTALL_KEY%"=="" (
    echo ERROR: missing ^<install_key^> argument.
    echo Usage: install-agent.bat ^<server_url^> ^<install_key^>
    echo Example: install-agent.bat https://chaosops.example.com ik_xxxx
    exit /b 1
)
REM Strip a trailing slash from the server URL.
if "%SERVER_URL:~-1%"=="/" set "SERVER_URL=%SERVER_URL:~0,-1%"

echo ChaosOps Agent installer
echo Server: %SERVER_URL%
echo.

REM --------------- Locate bundled agent source (dual layout) ---------------
REM Packaged layout: install-agent.bat and the agent\ directory are siblings in
REM the package root. Dev layout: this script lives in scripts\ and agent\ is at
REM the repository root (one level up).
set "SCRIPT_DIR=%~dp0"
set "AGENT_SRC="
if exist "%SCRIPT_DIR%agent\app" set "AGENT_SRC=%SCRIPT_DIR%agent"
if not defined AGENT_SRC if exist "%SCRIPT_DIR%..\agent\app" set "AGENT_SRC=%SCRIPT_DIR%..\agent"
if not defined AGENT_SRC (
    echo ERROR: bundled agent source not found beside this script.
    exit /b 1
)
echo Agent source: %AGENT_SRC%

REM --------------- Python 3.10+ check (native Windows Python only) ---------------
REM Prefer the Python Launcher (py), which manages native Windows builds only.
REM Fall back to python/python3 on PATH, rejecting Microsoft Store stubs
REM (WindowsApps) and MSYS2/Cygwin builds (their venvs are POSIX-style bin/ not
REM Scripts/ and cannot run as a native Windows service).
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
echo Using Python: %PYTHON_CMD%

REM --------------- Copy agent as a package ---------------
REM The agent code uses absolute imports (from agent.app...), so the agent\
REM package directory must be preserved under INSTALL_DIR (not flattened).
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
if exist "%INSTALL_DIR%\agent" rmdir /S /Q "%INSTALL_DIR%\agent"
xcopy /E /I /Q "%AGENT_SRC%" "%INSTALL_DIR%\agent\" >nul
if errorlevel 1 (
    echo ERROR: failed to copy agent files.
    exit /b 1
)

REM --------------- Create virtual environment ---------------
echo Creating Python virtual environment...
if exist "%INSTALL_DIR%\.venv" rmdir /S /Q "%INSTALL_DIR%\.venv"
%PYTHON_CMD% -m venv "%INSTALL_DIR%\.venv"
if errorlevel 1 (
    echo ERROR: failed to create virtual environment.
    exit /b 1
)
if not exist "%INSTALL_DIR%\.venv\Scripts\python.exe" (
    echo ERROR: the virtual environment has no Scripts\python.exe.
    echo        The selected Python is not a native Windows build ^(e.g. MSYS2/Cygwin
    echo        or the Microsoft Store stub^). Install native Python from python.org.
    exit /b 1
)
set "VENV_PY=%INSTALL_DIR%\.venv\Scripts\python.exe"
set "VENV_PIP=%INSTALL_DIR%\.venv\Scripts\pip.exe"

REM --------------- Install dependencies (offline wheels + online fallback) ---------------
REM Bundled wheels are built on Linux; binary wheels such as psutil are
REM manylinux-only and cannot be installed on Windows. Detect that and fall back
REM to downloading Windows-specific packages from PyPI (internet required).
set "NEED_ONLINE=0"
if not exist "%INSTALL_DIR%\agent\wheels" set "NEED_ONLINE=1"
if exist "%INSTALL_DIR%\agent\wheels\" (
    for %%F in ("%INSTALL_DIR%\agent\wheels\*.whl") do (
        echo %%~nxF | findstr /I /C:"manylinux" >nul && set "NEED_ONLINE=1"
    )
)
if "!NEED_ONLINE!"=="1" (
    echo NOTE: bundled wheels include Linux-only builds ^(e.g. psutil^).
    echo       Using bundled pure-Python wheels and downloading Windows-specific
    echo       packages from PyPI. An internet connection is required.
    "%VENV_PIP%" install --disable-pip-version-check --find-links "%INSTALL_DIR%\agent\wheels" -r "%INSTALL_DIR%\agent\requirements.txt"
) else (
    echo Installing dependencies from bundled offline wheels...
    "%VENV_PIP%" install --disable-pip-version-check --no-index --find-links "%INSTALL_DIR%\agent\wheels" -r "%INSTALL_DIR%\agent\requirements.txt"
)
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    exit /b 1
)

REM --------------- Register agent (consumes install_key) ---------------
echo Registering with the server...
"%VENV_PY%" -c "import json,urllib.request; req=urllib.request.Request('%SERVER_URL%/api/v1/agents/register', data=json.dumps({'install_key':'%INSTALL_KEY%','hostname':'%COMPUTERNAME%','os':'windows','arch':'%PROCESSOR_ARCHITECTURE%','version':'0.1.0'}).encode(), headers={'Content-Type':'application/json'}); resp=urllib.request.urlopen(req, timeout=30); data=json.loads(resp.read()); cfg={'server_url':'%SERVER_URL%','agent_token':data['agent_token'],'node_id':data['node_id'],'heartbeat_interval':data.get('heartbeat_interval',10),'auto_update':{'enabled':True,'mode':'manual','check_interval_hours':24,'channel':'stable'}}; json.dump(cfg, open(r'%INSTALL_DIR%\config.json','w'), indent=2)"
if errorlevel 1 (
    echo ERROR: registration failed. Check SERVER_URL and INSTALL_KEY ^(valid, not expired^).
    exit /b 1
)
echo Config saved to: %INSTALL_DIR%\config.json

REM --------------- Scheduled task (auto-start, correct working directory) ---------------
REM Write a launcher that sets the working directory first: the agent code uses
REM absolute imports (from agent.app...) and needs cwd=INSTALL_DIR to resolve the
REM agent package.
echo Registering scheduled task ^(auto-start at boot, SYSTEM account^)...
(
echo @echo off
echo cd /d "%INSTALL_DIR%"
echo "%VENV_PY%" -m agent.app.main --config-dir "%INSTALL_DIR%"
) > "%INSTALL_DIR%\start-agent.bat"

schtasks /create /tn "ChaosOpsAgent" /tr "\"%INSTALL_DIR%\start-agent.bat\"" /sc onstart /ru SYSTEM /rl HIGHEST /f >nul 2>nul
if errorlevel 1 (
    echo WARNING: scheduled task registration failed ^(may need Administrator rights^).
    echo          You can start the agent manually: "%INSTALL_DIR%\start-agent.bat"
) else (
    schtasks /run /tn "ChaosOpsAgent" >nul 2>nul
    echo Scheduled task ChaosOpsAgent registered and started.
)

echo.
echo ChaosOps Agent installation complete.
exit /b 0
