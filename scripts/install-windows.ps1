[CmdletBinding()]
param(
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA "PowerFactoryAgent"),
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "PowerFactory MCP installation is supported only on Windows."
}

$repository = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $repository "pyproject.toml"))) {
    throw "Run this script from a PowerFactory MCP repository checkout."
}

$roots = @(
    (Join-Path ${env:ProgramFiles} "DIgSILENT"),
    (Join-Path ${env:ProgramFiles(x86)} "DIgSILENT")
) | Where-Object { $_ -and (Test-Path $_) }

$candidates = @(
    foreach ($root in $roots) {
        Get-ChildItem -Path $root -Filter "powerfactory.pyd" -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "PowerFactory 2026.*\\Python\\(?<version>\d+\.\d+)\\powerfactory\.pyd$" } |
            ForEach-Object {
                [PSCustomObject]@{
                    Path = $_.FullName
                    PythonVersion = $Matches.version
                }
            }
    }
)

if ($candidates.Count -eq 0) {
    throw "No PowerFactory 2026 powerfactory.pyd was found under Program Files."
}

$candidate = $candidates |
    Sort-Object @{ Expression = { [version]$_.PythonVersion }; Descending = $true }, Path |
    Select-Object -First 1

Push-Location $repository
try {
    & uv sync --python $candidate.PythonVersion
    if ($LASTEXITCODE -ne 0) { throw "uv could not prepare Python $($candidate.PythonVersion)." }

    $configPath = Join-Path $StateDir "powerfactory-agent.json"
    if (-not (Test-Path $configPath)) {
        & uv run powerfactory-agent init --state-dir $StateDir --port $Port
        if ($LASTEXITCODE -ne 0) { throw "PowerFactory MCP initialization failed." }
    }

    & uv run powerfactory-agent configure-probe `
        --config $configPath `
        --pyd-path $candidate.Path `
        --python-version $candidate.PythonVersion `
        --project "@active" `
        --study-case "@active"
    if ($LASTEXITCODE -ne 0) { throw "PowerFactory probe configuration failed." }

    & uv run powerfactory-agent probe --config $configPath --repeat 2
    if ($LASTEXITCODE -ne 0) {
        throw "The real PowerFactory probe failed. Send the generated evidence file; do not substitute a fake configuration."
    }

    $env:POWERFACTORY_AGENT_MCP_TOKEN = (Get-Content -Raw (Join-Path $StateDir "mcp-token")).Trim()
    $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen `
        -ErrorAction SilentlyContinue
    if (-not $listener) {
        Start-Process -FilePath "uv" `
            -ArgumentList @("run", "powerfactory-agent", "serve", "--config", $configPath) `
            -WorkingDirectory $repository `
            -WindowStyle Hidden
        Start-Sleep -Seconds 2
    }

    & codex mcp remove powerfactory-agent 2>$null
    & codex mcp add powerfactory-agent `
        --url "http://127.0.0.1:$Port/mcp" `
        --bearer-token-env-var POWERFACTORY_AGENT_MCP_TOKEN
    if ($LASTEXITCODE -ne 0) { throw "Codex MCP registration failed." }

    Write-Host "PowerFactory MCP is installed and registered. Start Codex from this PowerShell session."
}
finally {
    Pop-Location
}
