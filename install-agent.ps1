# ChaosOps Agent Windows PowerShell 安装脚本（离线包）
# 用法: .\install-agent.ps1 -ServerUrl "https://your-server" -InstallKey "ik_xxxx"
#
# 重要：本文件必须以 UTF-8 BOM 保存。PowerShell 5.1 在无 BOM 时按系统 ANSI
# 代码页读取脚本，含中文会乱码甚至解析失败。构建脚本会断言 BOM 存在，请勿移除。
#
# 流程：参数 -> 定位捆绑 Agent 源码（双布局）-> Python 检查 -> 按包结构复制
#       agent -> venv -> 安装依赖（离线 wheels，manylinux 轮自动降级在线安装）
#       -> 注册 -> 写 config.json -> 注册计划任务（开机自启、正确工作目录）。

param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$InstallKey,

    [string]$InstallDir = "$env:ProgramData\ChaosOpsAgent"
)

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$Message) Write-Host "[INFO] $Message" }
function Write-Warn { param([string]$Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-ErrorLog { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }

# 处理路径中的空格与尾部斜杠
$InstallDir = $InstallDir.TrimEnd('\')
$ServerUrl = $ServerUrl.TrimEnd('/')
$ConfigFile = Join-Path $InstallDir "config.json"
$AgentModule = "agent.app.main"

# 检查 Python 3.10+（仅原生 Windows Python）
# 优先 Python Launcher(py)：它只管理原生 Windows 构建，解析出具体 python.exe 路径使用。
# 回退 python/python3 时拒绝 Microsoft Store 桩(WindowsApps)与 MSYS2/Cygwin（POSIX 风格 venv，不能作原生 Windows 服务）。
$pythonCmd = $null
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $pyVer = & py -3 --version 2>&1
    if ($pyVer -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
        $pythonCmd = (& py -3 -c "import sys; print(sys.executable)").Trim()
    }
}
if (-not $pythonCmd) {
    foreach ($candidate in @("python", "python3")) {
        if ($pythonCmd) { break }
        foreach ($cmd in (Get-Command $candidate -ErrorAction SilentlyContinue -All)) {
            if ($pythonCmd) { break }
            $src = $cmd.Source
            if ($src -match "WindowsApps|msys|cygwin") { continue }
            $verOut = & $src --version 2>&1
            if ($verOut -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
                $pythonCmd = $src
            }
        }
    }
}
if (-not $pythonCmd) {
    Write-ErrorLog "未找到原生 Windows Python 3.10+，请从 python.org 安装 Python 3.10 或更高版本"
    exit 1
}
Write-Info "使用 Python: $pythonCmd"

# 定位捆绑 Agent 源码（双布局）
# 打包布局：install-agent.ps1 与 agent\ 同处包根；开发布局：脚本在 scripts\，agent\ 在仓库根（上一层）。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentSrc = $null
$candSame = Join-Path $ScriptDir "agent"
$candParent = Join-Path (Split-Path -Parent $ScriptDir) "agent"
if (Test-Path (Join-Path $candSame "app")) {
    $AgentSrc = $candSame
} elseif (Test-Path (Join-Path $candParent "app")) {
    $AgentSrc = $candParent
}
if (-not $AgentSrc) {
    Write-ErrorLog "未在脚本旁找到捆绑的 Agent 源码目录"
    exit 1
}
Write-Info "Agent 源码: $AgentSrc"

# 创建安装目录并按包结构复制 agent（代码用 from agent.app... 绝对导入，必须保留 agent\ 包层，不能扁平化）
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
$AgentPkgDir = Join-Path $InstallDir "agent"
if (Test-Path $AgentPkgDir) {
    Remove-Item $AgentPkgDir -Recurse -Force
}
New-Item -ItemType Directory -Path $AgentPkgDir -Force | Out-Null
Write-Info "复制 Agent 文件到: $AgentPkgDir"
Copy-Item -Path (Join-Path $AgentSrc "*") -Destination $AgentPkgDir -Recurse -Force

# 创建虚拟环境
Write-Info "创建 Python 虚拟环境..."
$venvDir = Join-Path $InstallDir ".venv"
if (Test-Path $venvDir) {
    Remove-Item $venvDir -Recurse -Force
}
& $pythonCmd -m venv $venvDir
if ($LASTEXITCODE -ne 0) {
    Write-ErrorLog "创建虚拟环境失败"
    exit 1
}
if (-not (Test-Path (Join-Path $venvDir "Scripts\python.exe"))) {
    Write-ErrorLog "虚拟环境缺少 Scripts\python.exe：所选 Python 不是原生 Windows 构建（如 MSYS2/Cygwin 或 Microsoft Store 桩），请从 python.org 安装原生 Python"
    exit 1
}
$PythonExe = Join-Path $venvDir "Scripts\python.exe"
$PipExe = Join-Path $venvDir "Scripts\pip.exe"

# 安装依赖：捆绑 wheels 在 Linux 上构建，psutil 等二进制轮是 manylinux，Windows 无法安装，
# 检测到 manylinux 轮即降级为在线安装（--find-links 不加 --no-index：本地可用的纯 Python 轮照用，
# Windows 专用包从 PyPI 下载，需联网）。
$wheelsDir = Join-Path $AgentPkgDir "wheels"
$reqFile = Join-Path $AgentPkgDir "requirements.txt"
$needOnline = $false
if (-not (Test-Path $wheelsDir)) {
    $needOnline = $true
} else {
    $linuxWheel = Get-ChildItem -Path $wheelsDir -Filter "*.whl" -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "manylinux" }
    if ($linuxWheel) { $needOnline = $true }
}
if ($needOnline) {
    Write-Warn "捆绑 wheels 含 Linux 专用构建（如 psutil），将使用捆绑的纯 Python 包并从网络下载 Windows 专用包（需联网）..."
    & $PipExe install --disable-pip-version-check --find-links $wheelsDir -r $reqFile
} else {
    Write-Info "从捆绑离线 wheels 安装依赖..."
    & $PipExe install --disable-pip-version-check --no-index --find-links $wheelsDir -r $reqFile
}
if ($LASTEXITCODE -ne 0) {
    Write-ErrorLog "依赖安装失败"
    exit 1
}

# 注册 Agent（消耗 install_key）
Write-Info "向 Server 注册 Agent..."
$body = @{
    install_key = $InstallKey
    hostname    = $env:COMPUTERNAME
    os          = "windows"
    arch        = $env:PROCESSOR_ARCHITECTURE
    version     = "0.1.0"
} | ConvertTo-Json

try {
    $response = Invoke-RestMethod -Uri "$ServerUrl/api/v1/agents/register" -Method POST -ContentType "application/json" -Body $body -TimeoutSec 30
    $agentToken = $response.agent_token
    $nodeId = $response.node_id
    $heartbeatInterval = if ($response.PSObject.Properties.Name -contains "heartbeat_interval") { $response.heartbeat_interval } else { 10 }
}
catch {
    Write-ErrorLog "注册失败，请检查 ServerUrl 与 InstallKey 是否正确、未过期: $_"
    exit 1
}

# 写入配置
$config = @{
    server_url         = $ServerUrl
    agent_token        = $agentToken
    node_id            = $nodeId
    heartbeat_interval = $heartbeatInterval
    auto_update        = @{
        enabled              = $true
        mode                 = "manual"
        check_interval_hours = 24
        channel              = "stable"
    }
}
$configJson = $config | ConvertTo-Json -Depth 4
# 必须写 UTF-8 无 BOM：Agent 用 json.load(utf-8) 读配置，带 BOM 会直接报
# "Unexpected UTF-8 BOM" 启动即崩。PowerShell 5.1 的 Set-Content -Encoding UTF8
# 会写 BOM，故改用 UTF8Encoding($false) 显式无 BOM 写入。
[System.IO.File]::WriteAllText($ConfigFile, $configJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Info "配置已保存到: $ConfigFile"

# 注册计划任务（开机自启、SYSTEM 账户）；WorkingDirectory=InstallDir 保证 agent 包可解析
Write-Info "注册计划任务（开机自启）..."
$taskName = "ChaosOpsAgent"
$taskAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m $AgentModule --config-dir `"$InstallDir`"" -WorkingDirectory $InstallDir
$taskTrigger = New-ScheduledTaskTrigger -AtStartup
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

try {
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Info "计划任务 $taskName 已注册并启动（开机自启、SYSTEM 账户）"
}
catch {
    Write-Warn "计划任务注册失败（可能需管理员权限）。可手动启动: `"$PythonExe`" -m $AgentModule --config-dir `"$InstallDir`""
}

Write-Info "ChaosOps Agent 安装完成，节点 ID: $nodeId"
