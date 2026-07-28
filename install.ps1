# ChaosOps Server installer wrapper for Windows (V2)
# Calls bin\chaosops-installer.exe with all arguments.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArgs
)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $scriptDir "bin\chaosops-installer.exe"
& $installer @InstallerArgs
exit $LASTEXITCODE
