[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PowerFactoryMCP"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,
    [string]$PowerFactoryPydPath,
    [string]$RepositoryUrl = "https://github.com/bamiboy237/powerfactory-mcp.git",
    [string]$Ref = "main",
    [switch]$TestHarness
)

# Every side effect is scoped to an attempt until active.json is replaced.  This
# script intentionally does not accept a project or study case: engineering
# context is selected later through the authenticated MCP session.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$script:Stage = "preflight"
$script:Attempt = $null
$script:Prior = $null
$script:PriorWasStopped = $false
$script:CodexChanged = $false
$script:StagedServer = $null
$script:FinalServer = $null
$script:PriorCodexOwned = $false
$script:Mutex = $null
$script:PriorRegistration = $null
$script:Token = $null

. (Join-Path $PSScriptRoot "windows-installer-registration.ps1")

function Stop-Install {
    param([string]$Message, [string]$Category = "INSTALLATION_FAILED", [int]$ExitCode = 1)
    $exception = New-Object System.Exception($Message)
    $exception.Data["category"] = $Category
    $exception.Data["exit_code"] = $ExitCode
    throw $exception
}

function Get-RequiredCommand {
    param([string]$Name, [string]$Hint)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { Stop-Install "$Name is not available on PATH. $Hint" "PREREQUISITE_MISSING" }
    return $command.Source
}

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Action)
    $script:Stage = $Name
    if ($env:POWERFACTORY_MCP_FAIL_STAGE -eq $Name) {
        Stop-Install "Failure injection requested for $Name." "INJECTED_FAILURE"
    }
    & $Action
}

function Invoke-CheckedCommand {
    param([string]$FilePath, [string[]]$ArgumentList, [string]$Failure)
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) { Stop-Install "$Failure (exit code $LASTEXITCODE)." "COMMAND_FAILED" $LASTEXITCODE }
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) }
    catch { Stop-Install "Managed metadata is invalid JSON." "METADATA_INVALID" }
}

function Write-AtomicJson {
    param([string]$Path, [object]$Value)
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8 -NoNewline
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Set-AttemptPrivateAcl {
    param([string]$Directory, [string]$IcaclsPath)
    # The root is created empty, so inheritance protects all new descendants.
    # Never recurse into an existing release or managed root.
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & $IcaclsPath $Directory "/inheritance:r" "/grant:r" "*${sid}:(OI)(CI)F" "/grant:r" "*S-1-5-18:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Could not protect the new staged attempt." "ACL_FAILED" }
}

function Remove-Attempt {
    param([string]$Path, [string]$IcaclsPath)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return "not_present" }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        return "removed"
    }
    catch {
        # Repairing ACLs is allowed only for the disposable attempt being removed.
        & $IcaclsPath $Path "/grant" "*$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value):F" "/T" "/C" "/Q" | Out-Null
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return "removed_after_attempt_acl_repair"
        }
        catch { return "retained_cleanup_failed" }
    }
}

function Resolve-PowerFactoryRuntime {
    param([string]$RequestedPath)
    $paths = @()
    if ($RequestedPath) {
        $candidate = Get-Item -LiteralPath $RequestedPath -ErrorAction SilentlyContinue
        if (-not $candidate -or $candidate.PSIsContainer) { Stop-Install "The supplied powerfactory.pyd does not exist." "POWERFACTORY_NOT_FOUND" }
        $paths = @($candidate)
    } else {
        foreach ($root in @((Join-Path ${env:ProgramFiles} "DIgSILENT"), (Join-Path ${env:ProgramFiles(x86)} "DIgSILENT")) | Where-Object { $_ -and (Test-Path $_) }) {
            $paths += Get-ChildItem -LiteralPath $root -Filter "powerfactory.pyd" -File -Recurse -ErrorAction SilentlyContinue
        }
    }
    $candidates = @($paths | ForEach-Object {
        if ($_.FullName -match "PowerFactory 2026[^\\]*\\Python\\(?<version>\d+\.\d+)\\powerfactory\.pyd$") {
            [PSCustomObject]@{ Path=$_.FullName; PythonVersion=$Matches.version; ProductDirectory=($_.FullName -replace "\\Python\\\d+\.\d+\\powerfactory\.pyd$", "") }
        }
    })
    if ($candidates.Count -eq 0) { Stop-Install "No compatible PowerFactory 2026 Python module was found." "POWERFACTORY_NOT_FOUND" }
    return $candidates | Sort-Object @{Expression={[version]$_.PythonVersion};Descending=$true}, Path | Select-Object -First 1
}

function Test-McpInitialize {
    param([string]$Endpoint, [string]$Token)
    $headers = @{ Authorization="Bearer $Token"; Accept="application/json, text/event-stream" }
    $body = @{ jsonrpc="2.0"; id=1; method="initialize"; params=@{ protocolVersion="2025-06-18"; capabilities=@{}; clientInfo=@{name="powerfactory-agent-installer";version="0.1.0"} } } | ConvertTo-Json -Depth 5 -Compress
    try {
        $response = Invoke-RestMethod -Method Post -Uri $Endpoint -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec 3
        return ($null -ne $response.result -and $null -ne $response.result.serverInfo)
    } catch { return $false }
}

function Wait-McpReady {
    param([string]$Endpoint, [string]$Token, [System.Diagnostics.Process]$Process)
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if ($Process -and $Process.HasExited) { return $false }
        if (Test-McpInitialize -Endpoint $Endpoint -Token $Token) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

function Get-FreeLoopbackPort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start(); $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port; $listener.Stop()
    return $port
}

function Start-McpServer {
    param([string]$Uv, [string]$Source, [string]$Config, [int]$ListenPort, [string]$Token)
    $arguments = "run powerfactory-agent serve --config `"$Config`" --port $ListenPort"
    $process = Start-Process -FilePath $Uv -ArgumentList $arguments -WorkingDirectory $Source -WindowStyle Hidden -PassThru
    $endpoint = "http://127.0.0.1:$ListenPort/mcp"
    if (-not (Wait-McpReady -Endpoint $endpoint -Token $Token -Process $process)) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        Stop-Install "The staged authenticated MCP endpoint was not ready." "MCP_HEALTH_FAILED"
    }
    return $process
}

function Stop-OwnedProcess {
    param([object]$Ledger)
    if (-not $Ledger -or -not $Ledger.pid) { return "not_recorded" }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($Ledger.pid)" -ErrorAction SilentlyContinue
    if (-not $process) { return "already_stopped" }
    if (-not $process.CommandLine -or $process.CommandLine -notmatch "powerfactory-agent\s+serve") { return "not_owned" }
    if ($Ledger.config_path -and $process.CommandLine -notmatch [regex]::Escape([string]$Ledger.config_path)) { return "identity_mismatch" }
    try { $startTicks = (Get-Process -Id $Ledger.pid -ErrorAction Stop).StartTime.ToUniversalTime().Ticks } catch { return "identity_mismatch" }
    if (-not $Ledger.process_start_ticks -or [int64]$Ledger.process_start_ticks -ne [int64]$startTicks) { return "identity_mismatch" }
    Stop-Process -Id $Ledger.pid -Force -ErrorAction Stop
    return "stopped"
}

function Write-CodexLauncher {
    param([string]$Path, [string]$Root, [string]$Codex)
    $rootLiteral = $Root.Replace("'", "''"); $codexLiteral = $Codex.Replace("'", "''")
    @"
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=`$true)][string[]]`$CodexArguments)
`$ErrorActionPreference = "Stop"
`$active = Get-Content -LiteralPath (Join-Path '$rootLiteral' "active.json") -Raw | ConvertFrom-Json
`$tokenPath = Join-Path `$active.release_path "state\\mcp-token"
if (-not (Test-Path -LiteralPath `$tokenPath)) { throw "PowerFactory MCP active credential is missing." }
`$env:POWERFACTORY_AGENT_MCP_TOKEN = (Get-Content -LiteralPath `$tokenPath -Raw).Trim()
& '$codexLiteral' @CodexArguments
exit `$LASTEXITCODE
"@ | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Test-OwnedCodexRegistration {
    param([object]$Registration, [object]$Prior, [string]$Endpoint, [string]$Token)
    if ($Registration.state -eq "absent") { return -not $Prior }
    if ($Registration.state -ne "present" -or -not $Prior) { return $false }
    $ledger = Read-JsonFile (Join-Path $Prior.release_path "ownership.json")
    $fingerprint = $Registration.fingerprint
    if (-not $ledger -or $ledger.codex_name -ne $fingerprint.name -or $ledger.endpoint -ne $fingerprint.endpoint -or $ledger.token_env_var -ne $fingerprint.token_env_var -or $ledger.endpoint -ne $Endpoint) { return $false }
    # Name, loopback endpoint, token-env binding, credential identity, and live
    # authenticated MCP contract are all required before this installer changes it.
    if (-not (Test-McpInitialize -Endpoint $Endpoint -Token $Token)) { return $false }
    $tokenHash = ([System.BitConverter]::ToString((New-Object Security.Cryptography.SHA256Managed).ComputeHash([Text.Encoding]::UTF8.GetBytes($Token))).Replace("-", "").ToLowerInvariant())
    if ($ledger.token_identity -ne $tokenHash) { return $false }
    return $true
}

function Register-Codex {
    param([string]$Codex, [string]$Endpoint)
    & $Codex mcp remove powerfactory-agent *> $null
    Invoke-CheckedCommand $Codex @("mcp", "add", "powerfactory-agent", "--url", $Endpoint, "--bearer-token-env-var", "POWERFACTORY_AGENT_MCP_TOKEN") "Codex registration failed"
}

function Remove-InstallerCodexRegistration {
    param([string]$Codex)
    & $Codex mcp remove powerfactory-agent *> $null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Codex registration cleanup failed" "CODEX_ROLLBACK_FAILED" $LASTEXITCODE }
}

function Restart-PriorRelease {
    param([object]$Prior, [string]$Uv)
    if (-not $Prior -or -not $script:PriorWasStopped) { return "not_needed" }
    try {
        $config = Join-Path $Prior.release_path "state\\powerfactory-agent.json"
        $token = (Get-Content -LiteralPath (Join-Path $Prior.release_path "state\\mcp-token") -Raw).Trim()
        $process = Start-McpServer -Uv $Uv -Source (Join-Path $Prior.release_path "source") -Config $config -ListenPort ([int]$Prior.port) -Token $token
        return "restarted:$($process.Id)"
    } catch { return "restart_failed" }
}

function Write-FailureReport {
    param([string]$Reports, [object]$ErrorRecord, [object]$Runtime, [System.Collections.IDictionary]$Rollback)
    $category = if ($ErrorRecord.Exception.Data["category"]) { $ErrorRecord.Exception.Data["category"] } else { "UNHANDLED" }
    $exitCode = if ($ErrorRecord.Exception.Data["exit_code"]) { [int]$ErrorRecord.Exception.Data["exit_code"] } else { 1 }
    $report = [ordered]@{
        schema_version = "powerfactory-mcp-install-failure/v1"; attempt_id = if ($script:Attempt) { $script:Attempt.id } else { "none" }
        stage = $script:Stage; category = $category; exception_type = $ErrorRecord.Exception.GetType().Name; exit_code = $exitCode
        commit = if ($script:Attempt) { $script:Attempt.commit } else { "unknown" }
        environment = @{ powerfactory_release="2026"; python_abi=if ($Runtime) { $Runtime.PythonVersion } else { "unknown" }; architecture=[System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString() }
        rollback = $Rollback
    }
    New-Item -ItemType Directory -Path $Reports -Force -ErrorAction Stop | Out-Null
    Write-AtomicJson (Join-Path $Reports "$($report.attempt_id).json") $report
}

function Invoke-InstallerRollback {
    param([object]$ErrorRecord, [string]$Reports, [object]$Runtime)

    $rollback = [ordered]@{}
    if ($script:StagedServer -and -not $script:StagedServer.HasExited) {
        Stop-Process -Id $script:StagedServer.Id -Force -ErrorAction SilentlyContinue
        $rollback.staged_mcp = "stopped"
    }
    if ($script:FinalServer -and -not $script:FinalServer.HasExited) {
        Stop-Process -Id $script:FinalServer.Id -Force -ErrorAction SilentlyContinue
        $rollback.final_mcp = "stopped"
    }
    if ($script:CodexChanged) {
        try {
            if ($script:Prior) {
            Register-Codex $script:Codex "http://127.0.0.1:$($script:Prior.port)/mcp"
            $rollback.codex_registration = "restored"
            } else {
                Remove-InstallerCodexRegistration $script:Codex
                $rollback.codex_registration = "removed_new_registration"
            }
        } catch {
            $rollback.codex_registration = "restore_or_removal_failed"
        }
    } else {
        $rollback.codex_registration = "untouched"
    }
    $rollback.prior_release = Restart-PriorRelease $script:Prior $script:Uv
    # The installer only executes disposable acquisition probes. It never starts
    # a persistent PowerFactory owner, so rollback has no broad engine kill path.
    $rollback.powerfactory = "no_persistent_engine_created; acquisition workers are disposable"
    if ($script:Attempt) { $rollback.attempt = Remove-Attempt $script:Attempt.path $script:Icacls }
    try { Write-FailureReport $Reports $ErrorRecord $Runtime $rollback } catch { }
    return $rollback
}

$script:Uv = $null; $script:Git = $null; $script:Codex = $null; $script:Icacls = $null
$script:Runtime = $null
$root = [IO.Path]::GetFullPath($InstallRoot)
$attempts = Join-Path $root "attempts"; $releases = Join-Path $root "releases"; $reports = Join-Path $root "failure-reports"; $activePath = Join-Path $root "active.json"

if ($TestHarness) { return }

try {
    Invoke-Stage "preflight" {
        New-Item -ItemType Directory -Force -Path $attempts, $releases, $reports | Out-Null
        $mutexDigest = ([System.BitConverter]::ToString((New-Object Security.Cryptography.SHA256Managed).ComputeHash([Text.Encoding]::UTF8.GetBytes($root))).Replace("-", "").ToLowerInvariant().Substring(0, 24)
        $script:Mutex = New-Object System.Threading.Mutex($false, "Local\\PowerFactoryMCP-$mutexDigest")
        if (-not $script:Mutex.WaitOne(0)) { Stop-Install "Another PowerFactory MCP installation is already running." "INSTALLER_BUSY" }
        if ($env:OS -ne "Windows_NT") { Stop-Install "This installer supports Windows only." "PLATFORM_UNSUPPORTED" }
        if ($PSVersionTable.PSVersion -lt [version]"5.1") { Stop-Install "PowerShell 5.1 or newer is required." "PLATFORM_UNSUPPORTED" }
        $script:Uv = Get-RequiredCommand "uv" "Install uv and rerun."
        $script:Git = Get-RequiredCommand "git" "Install Git and rerun."
        $script:Codex = Get-RequiredCommand "codex" "Install/authenticate Codex CLI and rerun."
        $script:Icacls = Get-RequiredCommand "icacls.exe" "Use a standard Windows installation with icacls.exe."
        $script:Runtime = Resolve-PowerFactoryRuntime $PowerFactoryPydPath
        # Stale attempts never become active.  Their ownership ledgers are read
        # only to stop their recorded server, then their own directory is removed.
        Get-ChildItem -LiteralPath $attempts -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $ledger = Read-JsonFile (Join-Path $_.FullName "ownership.json")
            if ($ledger) { Stop-OwnedProcess $ledger | Out-Null }
            Remove-Attempt $_.FullName $script:Icacls | Out-Null
        }
        $script:Prior = Read-JsonFile $activePath
        if ($script:Prior -and -not (Test-Path -LiteralPath $script:Prior.release_path)) { Stop-Install "The active release manifest points to a missing managed release." "ACTIVE_RELEASE_INVALID" }
        if ($script:Prior) {
            $priorToken = (Get-Content -LiteralPath (Join-Path $script:Prior.release_path "state\\mcp-token") -Raw).Trim()
            $script:PriorRegistration = Get-CodexRegistrationFingerprint $script:Codex
            $script:PriorCodexOwned = Test-OwnedCodexRegistration $script:PriorRegistration $script:Prior "http://127.0.0.1:$($script:Prior.port)/mcp" $priorToken
        } else {
            $script:PriorRegistration = Get-CodexRegistrationFingerprint $script:Codex
            if ($script:PriorRegistration.state -ne "absent") { Stop-Install "Existing Codex registration is not provably owned. Resolve it manually with: codex mcp remove powerfactory-agent" "CODEX_OWNERSHIP_UNPROVEN" }
        }
    }

    Invoke-Stage "source" {
        $id = "attempt-$([guid]::NewGuid().ToString('N'))"; $path = Join-Path $attempts $id
        New-Item -ItemType Directory -Path $path -Force | Out-Null; Set-AttemptPrivateAcl $path $script:Icacls
        $script:Attempt = [PSCustomObject]@{ id=$id; path=$path; source=(Join-Path $path "source"); state=(Join-Path $path "state"); commit="unknown" }
        Invoke-CheckedCommand $script:Git @("clone", "--depth", "1", "--branch", $Ref, $RepositoryUrl, $script:Attempt.source) "Source checkout failed"
        $script:Attempt.commit = (& $script:Git -C $script:Attempt.source rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) { Stop-Install "Could not determine staged commit." "SOURCE_INVALID" }
    }

    Invoke-Stage "environment" {
        Push-Location $script:Attempt.source
        try { Invoke-CheckedCommand $script:Uv @("sync", "--locked", "--python", $script:Runtime.PythonVersion) "Python environment setup failed" }
        finally { Pop-Location }
        $python = Join-Path $script:Attempt.source ".venv\\Scripts\\python.exe"
        if (-not (Test-Path -LiteralPath $python)) { Stop-Install "Staged Python environment was not created." "ENVIRONMENT_INVALID" }
    }

    Invoke-Stage "secure_configuration" {
        New-Item -ItemType Directory -Path $script:Attempt.state -Force | Out-Null
        Set-AttemptPrivateAcl $script:Attempt.state $script:Icacls
        Push-Location $script:Attempt.source
        try {
            Invoke-CheckedCommand $script:Uv @("run", "powerfactory-agent", "init", "--state-dir", $script:Attempt.state, "--port", "$Port") "MCP initialization failed"
            Invoke-CheckedCommand $script:Uv @("run", "powerfactory-agent", "configure-probe", "--config", (Join-Path $script:Attempt.state "powerfactory-agent.json"), "--pyd-path", $script:Runtime.Path, "--python-version", $script:Runtime.PythonVersion, "--session-ownership", "product_owned") "PowerFactory installation configuration failed"
        } finally { Pop-Location }
        $script:Token = (Get-Content -LiteralPath (Join-Path $script:Attempt.state "mcp-token") -Raw).Trim()
        if ($script:Token.Length -lt 32) { Stop-Install "Staged MCP credential is invalid." "CREDENTIAL_INVALID" }
    }

    Invoke-Stage "temporary_mcp_health" {
        $temporaryPort = Get-FreeLoopbackPort
        $script:StagedServer = Start-McpServer $script:Uv $script:Attempt.source (Join-Path $script:Attempt.state "powerfactory-agent.json") $temporaryPort $script:Token
    }

    Invoke-Stage "codex_registration" {
        $endpoint = "http://127.0.0.1:$Port/mcp"
        if ($script:Prior -and -not $script:PriorCodexOwned) {
            Stop-Install "Existing Codex registration is not provably owned. Resolve it manually with: codex mcp remove powerfactory-agent" "CODEX_OWNERSHIP_UNPROVEN"
        }
        Register-Codex $script:Codex $endpoint; $script:CodexChanged = $true
    }

    Invoke-Stage "cutover_prior_service_drain" {
        if ($script:Prior) {
            $oldLedger = Read-JsonFile (Join-Path $script:Prior.release_path "ownership.json")
            $result = Stop-OwnedProcess $oldLedger
            if ($result -eq "identity_mismatch" -or $result -eq "not_owned") { Stop-Install "The active server cannot be proven installer-owned." "OWNERSHIP_UNPROVEN" }
            $script:PriorWasStopped = $true
        }
    }

    Invoke-Stage "acquisition_probe" {
        Push-Location $script:Attempt.source
        try { Invoke-CheckedCommand $script:Uv @("run", "powerfactory-agent", "probe-acquisition", "--config", (Join-Path $script:Attempt.state "powerfactory-agent.json")) "PowerFactory acquisition validation failed" }
        finally { Pop-Location }
    }

    Invoke-Stage "promotion" {
        if ($script:StagedServer -and -not $script:StagedServer.HasExited) { Stop-Process -Id $script:StagedServer.Id -Force -ErrorAction SilentlyContinue }
        $releaseName = "release-$($script:Attempt.commit.Substring(0, 12))-$($script:Attempt.id.Substring(8, 12))"
        $releasePath = Join-Path $releases $releaseName
        Move-Item -LiteralPath $script:Attempt.path -Destination $releasePath
        $script:Attempt.path = $releasePath; $script:Attempt.source = Join-Path $releasePath "source"; $script:Attempt.state = Join-Path $releasePath "state"
        $script:FinalServer = Start-McpServer $script:Uv $script:Attempt.source (Join-Path $script:Attempt.state "powerfactory-agent.json") $Port $script:Token
        Write-AtomicJson (Join-Path $releasePath "ownership.json") @{ pid=$script:FinalServer.Id; process_start_ticks=[int64]$script:FinalServer.StartTime.ToUniversalTime().Ticks; config_path=(Join-Path $script:Attempt.state "powerfactory-agent.json"); endpoint="http://127.0.0.1:$Port/mcp"; codex_name="powerfactory-agent"; token_env_var="POWERFACTORY_AGENT_MCP_TOKEN"; token_identity=([System.BitConverter]::ToString((New-Object Security.Cryptography.SHA256Managed).ComputeHash([Text.Encoding]::UTF8.GetBytes($script:Token))).Replace("-", "").ToLowerInvariant(); persistent_engine="none" }
        Invoke-CheckedCommand $script:Uv @("run", "powerfactory-agent", "probe-acquisition", "--config", (Join-Path $script:Attempt.state "powerfactory-agent.json")) "Final PowerFactory acquisition validation failed"
        Write-AtomicJson $activePath @{ schema_version="powerfactory-mcp-active/v1"; release_path=$releasePath; commit=$script:Attempt.commit; port=$Port }
        Write-CodexLauncher (Join-Path $root "Start-PowerFactoryCodex.ps1") $root $script:Codex
    }

    Write-Host "PowerFactory MCP installation is ready at commit $($script:Attempt.commit)."
    Write-Host "Start Codex with: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $root 'Start-PowerFactoryCodex.ps1')`""
}
catch {
    Invoke-InstallerRollback $_ $reports $script:Runtime | Out-Null
    throw
}
finally {
    if ($script:Mutex) { try { $script:Mutex.ReleaseMutex() } catch { }; $script:Mutex.Dispose() }
}
