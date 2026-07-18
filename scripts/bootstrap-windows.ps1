[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PowerFactoryMCP"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,
    [string]$PowerFactoryPydPath,
    [string]$RepositoryUrl = "https://github.com/bamiboy237/powerfactory-mcp.git",
    [string]$Ref = "main"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$git = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $git) {
    throw "Git is required. Install it, reopen PowerShell, and rerun this command."
}

# Clone the bootstrap payload as one immutable revision. This makes the
# installer self-contained and pins the staged product source to the exact code
# that is currently executing.
$bootstrapRoot = Join-Path $env:TEMP "powerfactory-mcp-bootstrap-$([guid]::NewGuid().ToString('N'))"
$bootstrapSource = Join-Path $bootstrapRoot "source"
try {
    New-Item -ItemType Directory -Path $bootstrapRoot -Force | Out-Null
    & $git.Source clone --depth 1 --branch $Ref $RepositoryUrl $bootstrapSource
    if ($LASTEXITCODE -ne 0) { throw "Could not download PowerFactory MCP from GitHub." }
    $commit = (& $git.Source -C $bootstrapSource rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Could not determine the downloaded PowerFactory MCP revision."
    }
    $installer = Join-Path $bootstrapSource "scripts\install-windows.ps1"
    & $installer -InstallRoot $InstallRoot -Port $Port -PowerFactoryPydPath $PowerFactoryPydPath -RepositoryUrl $RepositoryUrl -Ref $commit
}
finally {
    Remove-Item -LiteralPath $bootstrapRoot -Recurse -Force -ErrorAction SilentlyContinue
}
