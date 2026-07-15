[CmdletBinding()]
param(
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA "PowerFactoryAgent"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,
    [string]$PowerFactoryPydPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Stop-Install {
    param(
        [string]$Stage,
        [string]$Message,
        [string]$Remediation
    )

    $details = "PowerFactory MCP installation failed during ${Stage}: ${Message}"
    if ($Remediation) {
        $details += "`nNext step: $Remediation"
    }
    throw $details
}

function Get-RequiredCommand {
    param(
        [string]$Name,
        [string]$InstallHint
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) {
        Stop-Install "prerequisite check" "$Name is not available on PATH." $InstallHint
    }
    return $command.Source
}

function Invoke-CheckedCommand {
    param(
        [string]$Stage,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$Remediation
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        Stop-Install $Stage "'$FilePath' exited with code $LASTEXITCODE." $Remediation
    }
}

function Resolve-PowerFactoryRuntime {
    param([string]$RequestedPath)

    $paths = @()
    if ($RequestedPath) {
        $paths = @(Get-Item -LiteralPath $RequestedPath -ErrorAction SilentlyContinue)
        if ($paths.Count -eq 0 -or $paths[0].PSIsContainer) {
            Stop-Install "PowerFactory detection" "The supplied powerfactory.pyd does not exist: $RequestedPath" `
                "Pass -PowerFactoryPydPath with the full path to PowerFactory 2026\\Python\\<major.minor>\\powerfactory.pyd."
        }
    }
    else {
        $vendorRoots = @(
            (Join-Path ${env:ProgramFiles} "DIgSILENT"),
            (Join-Path ${env:ProgramFiles(x86)} "DIgSILENT")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

        foreach ($vendorRoot in $vendorRoots) {
            $installations = Get-ChildItem -LiteralPath $vendorRoot -Directory -Filter "PowerFactory 2026*" `
                -ErrorAction SilentlyContinue
            foreach ($installation in $installations) {
                $pythonRoot = Join-Path $installation.FullName "Python"
                if (Test-Path -LiteralPath $pythonRoot) {
                    $paths += Get-ChildItem -LiteralPath $pythonRoot -Filter "powerfactory.pyd" -File -Recurse `
                        -ErrorAction SilentlyContinue
                }
            }
        }
    }

    $candidates = @(
        foreach ($path in $paths) {
            if ($path.FullName -match "PowerFactory 2026[^\\]*\\Python\\(?<version>\d+\.\d+)\\powerfactory\.pyd$") {
                [PSCustomObject]@{
                    Path = $path.FullName
                    PythonVersion = $Matches.version
                    ProductDirectory = ($path.FullName -replace "\\Python\\\d+\.\d+\\powerfactory\.pyd$", "")
                }
            }
        }
    )

    if ($candidates.Count -eq 0) {
        Stop-Install "PowerFactory detection" "No supported PowerFactory 2026 Python module was found." `
            "Install the PowerFactory 2026 Python API, or rerun with -PowerFactoryPydPath '<full path to powerfactory.pyd>'."
    }

    $candidate = $candidates |
        Sort-Object @{ Expression = { [version]$_.PythonVersion }; Descending = $true }, Path |
        Select-Object -First 1

    if ($candidates.Count -gt 1 -and -not $RequestedPath) {
        Write-Host "Found $($candidates.Count) compatible PowerFactory Python modules; using the highest Python ABI."
        Write-Host "Use -PowerFactoryPydPath to select a different installation."
    }
    return $candidate
}

function Set-PrivateStateAcl {
    param(
        [string]$Directory,
        [string]$IcaclsPath
    )

    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & $IcaclsPath $Directory "/inheritance:r" "/grant:r" "*$currentSid`:(OI)(CI)F" `
        "/grant:r" "*S-1-5-18:(OI)(CI)F" "/T" "/C" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "credential protection" "Windows ACLs could not be restricted for $Directory." `
            "Run PowerShell as the engineer account that will run Codex, then rerun the installer."
    }

    $allowedSids = @($currentSid, "S-1-5-18")
    $aclTargets = @((Get-Item -LiteralPath $Directory)) + @(Get-ChildItem -LiteralPath $Directory -Force -Recurse)
    $unexpected = @(
        foreach ($target in $aclTargets) {
            foreach ($entry in (Get-Acl -LiteralPath $target.FullName).Access) {
                if ($entry.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) {
                    try {
                        $sid = $entry.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
                    }
                    catch {
                        $sid = $null
                    }
                    if ($sid -notin $allowedSids) {
                        "$($target.Name):$($entry.IdentityReference.Value)"
                    }
                }
            }
        }
    )
    if ($unexpected.Count -gt 0) {
        Stop-Install "credential protection" "Unexpected accounts retain access to $Directory: $($unexpected -join ', ')." `
            "Move the state directory aside, create a fresh one owned by this account, and rerun."
    }
}

function Test-McpInitialize {
    param(
        [string]$Endpoint,
        [string]$Token
    )

    $headers = @{
        Authorization = "Bearer $Token"
        Accept = "application/json, text/event-stream"
    }
    $body = @{
        jsonrpc = "2.0"
        id = 1
        method = "initialize"
        params = @{
            protocolVersion = "2025-06-18"
            capabilities = @{}
            clientInfo = @{ name = "powerfactory-agent-installer"; version = "0.1.0" }
        }
    } | ConvertTo-Json -Depth 5 -Compress

    try {
        $response = Invoke-RestMethod -Method Post -Uri $Endpoint -Headers $headers `
            -ContentType "application/json" -Body $body -TimeoutSec 3
        return ($null -ne $response.result -and $null -ne $response.result.serverInfo)
    }
    catch {
        return $false
    }
}

function Wait-McpReady {
    param(
        [string]$Endpoint,
        [string]$Token,
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            return $false
        }
        if (Test-McpInitialize -Endpoint $Endpoint -Token $Token) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Write-CodexLauncher {
    param(
        [string]$Target,
        [string]$TokenPath,
        [string]$CodexPath
    )

    $escapedTokenPath = $TokenPath.Replace("'", "''")
    $escapedCodexPath = $CodexPath.Replace("'", "''")
    $content = @"
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=`$true)][string[]]`$CodexArguments)
`$ErrorActionPreference = "Stop"
`$tokenPath = '$escapedTokenPath'
if (-not (Test-Path -LiteralPath `$tokenPath)) {
    throw "PowerFactory MCP token is missing. Rerun scripts\\install-windows.ps1."
}
`$env:POWERFACTORY_AGENT_MCP_TOKEN = (Get-Content -LiteralPath `$tokenPath -Raw).Trim()
& '$escapedCodexPath' @CodexArguments
exit `$LASTEXITCODE
"@
    Set-Content -LiteralPath $Target -Value $content -Encoding UTF8
}

if ($env:OS -ne "Windows_NT") {
    Stop-Install "platform check" "This installer supports Windows only." `
        "Run it on the PowerFactory workstation, not on macOS or Linux."
}
if ($PSVersionTable.PSVersion -lt [version]"5.1") {
    Stop-Install "platform check" "PowerShell 5.1 or newer is required." `
        "Open Windows PowerShell 5.1 or PowerShell 7 and rerun the command."
}

$repository = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $repository "pyproject.toml"))) {
    Stop-Install "repository check" "pyproject.toml was not found next to the installer." `
        "Run this script from an intact PowerFactory Agent repository checkout."
}

$uv = Get-RequiredCommand "uv" "Install uv (for example, 'winget install --id astral-sh.uv -e'), reopen PowerShell, and rerun."
$codex = Get-RequiredCommand "codex" "Install and authenticate the Codex CLI, reopen PowerShell, and rerun."
$icacls = Get-RequiredCommand "icacls.exe" "Use a standard Windows installation that includes icacls.exe."
if (-not (Get-Command "Get-NetTCPConnection" -ErrorAction SilentlyContinue)) {
    Stop-Install "prerequisite check" "Get-NetTCPConnection is unavailable." `
        "Use Windows PowerShell with the built-in NetTCPIP module."
}
$runtime = Resolve-PowerFactoryRuntime -RequestedPath $PowerFactoryPydPath

Write-Host "PowerFactory MCP guided installation"
Write-Host "  Product: $($runtime.ProductDirectory)"
Write-Host "  Python ABI: $($runtime.PythonVersion)"
Write-Host "  State: $StateDir"
Write-Host "The real probe will use the active project and active study case; it will not select another model."

Push-Location $repository
try {
    Invoke-CheckedCommand "Python environment setup" $uv `
        @("sync", "--locked", "--python", $runtime.PythonVersion) `
        "Confirm internet access and that uv can install 64-bit CPython $($runtime.PythonVersion)."

    $venvPython = Join-Path $repository ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Stop-Install "Python environment verification" "uv did not create $venvPython." `
            "Remove only the repository .venv directory, then rerun the installer."
    }
    $pythonIdentity = (& $venvPython -c "import platform,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.architecture()[0]}')").Trim()
    if ($LASTEXITCODE -ne 0 -or $pythonIdentity -ne "$($runtime.PythonVersion)|64bit") {
        Stop-Install "Python environment verification" "Expected $($runtime.PythonVersion)|64bit; found '$pythonIdentity'." `
            "Install the matching 64-bit CPython ABI or select another PowerFactory module with -PowerFactoryPydPath."
    }

    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    }
    Set-PrivateStateAcl -Directory $StateDir -IcaclsPath $icacls

    $configPath = Join-Path $StateDir "powerfactory-agent.json"
    $tokenPath = Join-Path $StateDir "mcp-token"
    if (-not (Test-Path -LiteralPath $configPath)) {
        if (Test-Path -LiteralPath $tokenPath) {
            Stop-Install "existing installation validation" "A token exists without its MCP configuration in $StateDir." `
                "Move this incomplete state directory aside and rerun to create a fresh installation."
        }
        Invoke-CheckedCommand "MCP initialization" $uv `
            @("run", "powerfactory-agent", "init", "--state-dir", $StateDir, "--port", "$Port") `
            "Check write access to $StateDir and rerun."
    }

    if (-not (Test-Path -LiteralPath $tokenPath)) {
        Stop-Install "existing installation validation" "The MCP token is missing from $StateDir." `
            "Move this incomplete state directory aside and rerun to create a fresh credential."
    }
    Set-PrivateStateAcl -Directory $StateDir -IcaclsPath $icacls

    try {
        $installation = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    }
    catch {
        Stop-Install "existing installation validation" "The MCP configuration is not valid JSON: $configPath" `
            "Move this incomplete state directory aside and rerun."
    }
    $configFields = @($installation.PSObject.Properties.Name)
    if ("host" -notin $configFields -or "port" -notin $configFields) {
        Stop-Install "existing installation validation" "The MCP configuration is missing its host or port." `
            "Move this incomplete state directory aside and rerun."
    }
    if ($installation.host -ne "127.0.0.1") {
        Stop-Install "existing installation validation" "Only the loopback host 127.0.0.1 is supported." `
            "Move the existing state directory aside and rerun."
    }
    $configuredPort = [int]$installation.port
    if ($configuredPort -ne $Port) {
        Write-Warning "Existing installation uses port $configuredPort; the -Port $Port argument is ignored on rerun."
    }
    $endpoint = "http://127.0.0.1:$configuredPort/mcp"

    Invoke-CheckedCommand "PowerFactory probe configuration" $uv `
        @(
            "run", "powerfactory-agent", "configure-probe",
            "--config", $configPath,
            "--pyd-path", $runtime.Path,
            "--python-version", $runtime.PythonVersion,
            "--project", "@active",
            "--study-case", "@active"
        ) `
        "Check the selected PowerFactory Python module and rerun."

    Invoke-CheckedCommand "real PowerFactory connectivity probe" $uv `
        @("run", "powerfactory-agent", "probe", "--config", $configPath, "--repeat", "2") `
        "Open the intended non-confidential project and study case, check the licence, then rerun. Keep the generated evidence file for diagnosis."

    $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
    if ($token.Length -lt 32) {
        Stop-Install "credential validation" "The MCP token is invalid." `
            "Move this incomplete state directory aside and rerun."
    }

    $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $configuredPort -State Listen `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    $serverProcess = $null
    if ($listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" `
            -ErrorAction SilentlyContinue
        if (-not $owner -or $owner.CommandLine -notmatch "powerfactory-agent\s+serve") {
            Stop-Install "MCP server startup" "Port $configuredPort is already used by another process." `
                "Stop that process or rerun with an unused -Port and a fresh -StateDir."
        }
        if (-not (Test-McpInitialize -Endpoint $endpoint -Token $token)) {
            Stop-Install "MCP server startup" "An existing PowerFactory MCP listener did not accept this installation credential." `
                "Stop the stale PowerFactory Agent process and rerun the installer."
        }
        Write-Host "Reusing the healthy PowerFactory MCP server on $endpoint"
    }
    else {
        $serveArguments = "run powerfactory-agent serve --config `"$configPath`""
        $serverProcess = Start-Process -FilePath $uv -ArgumentList $serveArguments `
            -WorkingDirectory $repository -WindowStyle Hidden -PassThru
        if (-not (Wait-McpReady -Endpoint $endpoint -Token $token -Process $serverProcess)) {
            Stop-Install "MCP server startup" "The authenticated endpoint was not ready within 20 seconds." `
                "Inspect $(Join-Path $StateDir 'powerfactory-agent.log'), stop any stale process, and rerun."
        }
        Write-Host "Started and verified the authenticated MCP server on $endpoint"
    }

    & $codex mcp remove powerfactory-agent *> $null
    Invoke-CheckedCommand "Codex MCP registration" $codex `
        @(
            "mcp", "add", "powerfactory-agent",
            "--url", $endpoint,
            "--bearer-token-env-var", "POWERFACTORY_AGENT_MCP_TOKEN"
        ) `
        "Run 'codex mcp list' to inspect existing registrations, then rerun."

    $launcherPath = Join-Path $StateDir "Start-PowerFactoryCodex.ps1"
    Write-CodexLauncher -Target $launcherPath -TokenPath $tokenPath -CodexPath $codex
    Set-PrivateStateAcl -Directory $StateDir -IcaclsPath $icacls

    Write-Host ""
    Write-Host "PowerFactory MCP installation is ready."
    Write-Host "Start Codex with:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$launcherPath`""
    Write-Host "The launcher supplies the protected bearer token only to that Codex process."
}
finally {
    Pop-Location
}
