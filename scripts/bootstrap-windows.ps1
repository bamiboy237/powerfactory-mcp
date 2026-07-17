[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PowerFactoryMCP"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,
    [string]$PowerFactoryPydPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Downloading to a file ensures PowerShell parses the complete script as one
# unit. Direct pipeline evaluation is not used for this multi-statement installer.
$scriptPath = Join-Path $env:TEMP "powerfactory-mcp-install-$([guid]::NewGuid().ToString('N')).ps1"
try {
    Invoke-WebRequest "https://raw.githubusercontent.com/bamiboy237/powerfactory-mcp/main/scripts/install-windows.ps1" -OutFile $scriptPath
    & $scriptPath -InstallRoot $InstallRoot -Port $Port -PowerFactoryPydPath $PowerFactoryPydPath
}
finally {
    Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
}
