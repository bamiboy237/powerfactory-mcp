[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PowerFactoryMCP"),
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA "PowerFactoryAgent"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,
    [string]$PowerFactoryPydPath,
    [string]$Project,
    [string]$StudyCase
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$repositoryUrl = "https://github.com/bamiboy237/powerfactory-mcp.git"
$sourceDir = Join-Path $InstallRoot "source"
$git = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $git) {
    throw "Git is required. Install it with 'winget install --id Git.Git -e', reopen PowerShell, and rerun."
}

if (Test-Path -LiteralPath (Join-Path $sourceDir ".git")) {
    $origin = (& $git.Source -C $sourceDir remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $origin -ne $repositoryUrl) {
        throw "Existing source directory has an unexpected Git origin: $sourceDir"
    }
    $changes = (& $git.Source -C $sourceDir status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the existing PowerFactory MCP source."
    }
    if ($changes) {
        $backupDir = "$sourceDir.local-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item -LiteralPath $sourceDir -Destination $backupDir
        Write-Host "Preserved locally modified source at $backupDir"
    }
}

if (Test-Path -LiteralPath (Join-Path $sourceDir ".git")) {
    & $git.Source -C $sourceDir fetch --prune origin main
    if ($LASTEXITCODE -ne 0) { throw "Could not update the PowerFactory MCP source." }
    & $git.Source -C $sourceDir checkout main
    if ($LASTEXITCODE -ne 0) { throw "Could not select the main release branch." }
    & $git.Source -C $sourceDir merge --ff-only origin/main
    if ($LASTEXITCODE -ne 0) {
        throw "The local source has diverged. Move $sourceDir aside and rerun."
    }
} else {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    & $git.Source clone --depth 1 --branch main $repositoryUrl $sourceDir
    if ($LASTEXITCODE -ne 0) { throw "Could not download PowerFactory MCP from GitHub." }
}

$installer = Join-Path $sourceDir "scripts\install-windows.ps1"
$arguments = @{
    StateDir = $StateDir
    Port = $Port
}
if ($PowerFactoryPydPath) {
    $arguments.PowerFactoryPydPath = $PowerFactoryPydPath
}
if ($Project) {
    $arguments.Project = $Project
}
if ($StudyCase) {
    $arguments.StudyCase = $StudyCase
}
& $installer @arguments
