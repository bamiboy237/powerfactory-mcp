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
$script:AgentExecutable = $null
$script:LauncherChanged = $false
$script:PriorLauncherContent = $null
$script:LauncherPath = $null

function ConvertTo-CodexRegistrationFingerprint {
    param([object]$Value)

    if ($null -eq $Value -or -not $Value.PSObject.Properties["name"]) { return $null }
    $transport = if ($Value.PSObject.Properties["transport"]) { $Value.transport } else { $Value }
    if ($null -eq $transport -or -not $transport.PSObject.Properties["url"] -or -not $transport.PSObject.Properties["bearer_token_env_var"]) { return $null }
    if ($transport.PSObject.Properties["type"] -and $transport.type -ne "streamable_http") { return $null }
    if ($Value.name -ne "powerfactory-agent") { return $null }
    if ($transport.url -notmatch '^http://127\.0\.0\.1:\d+/mcp$') { return $null }
    if ($transport.bearer_token_env_var -ne "POWERFACTORY_AGENT_MCP_TOKEN") { return $null }
    return [PSCustomObject]@{
        name = [string]$Value.name
        endpoint = [string]$transport.url
        token_env_var = [string]$transport.bearer_token_env_var
    }
}

function Get-CodexRegistrationFingerprint {
    param([string]$Codex)

    # Listing lets us distinguish an absent target from a failed lookup. The
    # captured JSON is never printed or persisted because other registrations
    # may contain sensitive headers.
    $output = & $Codex mcp list --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        return [PSCustomObject]@{ state = "query_failed"; fingerprint = $null }
    }
    try { $registrations = @(ConvertFrom-Json -InputObject ($output -join [Environment]::NewLine)) }
    catch { return [PSCustomObject]@{ state = "unparseable"; fingerprint = $null } }
    $matches = @($registrations | Where-Object { $_.name -eq "powerfactory-agent" })
    if ($matches.Count -eq 0) { return [PSCustomObject]@{ state = "absent"; fingerprint = $null } }
    if ($matches.Count -ne 1) { return [PSCustomObject]@{ state = "ambiguous"; fingerprint = $null } }
    $fingerprint = ConvertTo-CodexRegistrationFingerprint $matches[0]
    if ($null -eq $fingerprint) { return [PSCustomObject]@{ state = "unknown_schema"; fingerprint = $null } }
    return [PSCustomObject]@{ state = "present"; fingerprint = $fingerprint }
}

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
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8 -NoNewline
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($temporary, $Path, $null)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Write-AtomicText {
    param([string]$Path, [string]$Value)
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        Set-Content -LiteralPath $temporary -Value $Value -Encoding UTF8 -NoNewline
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($temporary, $Path, $null)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Get-Sha256Hex {
    param([string]$Value)
    $algorithm = New-Object Security.Cryptography.SHA256Managed
    try {
        $bytes = $algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
        return [System.BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
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
    $fullPath = [IO.Path]::GetFullPath($Path)
    $insideManagedRoot = $false
    foreach ($managedRoot in @($script:ManagedCleanupRoots)) {
        $prefix = [IO.Path]::GetFullPath($managedRoot).TrimEnd('\') + '\'
        if ($fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            $insideManagedRoot = $true
            break
        }
    }
    if (-not $insideManagedRoot) { return "refused_outside_managed_roots" }
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return "refused_reparse_point" }
    try {
        Remove-Item -LiteralPath $fullPath -Recurse -Force -ErrorAction Stop
        return "removed"
    }
    catch {
        # Repairing ACLs is allowed only for the disposable attempt being removed.
        & $IcaclsPath $fullPath "/grant" "*$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value):F" "/T" "/C" "/Q" | Out-Null
        try {
            Remove-Item -LiteralPath $fullPath -Recurse -Force -ErrorAction Stop
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
    param(
        [string]$AgentExecutable,
        [string]$Source,
        [string]$Config,
        [int]$ListenPort,
        [string]$Token,
        [string]$OwnershipPath,
        [string]$Scope
    )
    $arguments = "serve --config `"$Config`" --port $ListenPort"
    $process = Start-Process -FilePath $AgentExecutable -ArgumentList $arguments -WorkingDirectory $Source -WindowStyle Hidden -PassThru
    $endpoint = "http://127.0.0.1:$ListenPort/mcp"
    try {
        Write-AtomicJson $OwnershipPath @{
            schema_version = "powerfactory-mcp-process-ownership/v1"
            pid = $process.Id
            process_start_ticks = [int64]$process.StartTime.ToUniversalTime().Ticks
            command_kind = "mcp_server"
            scope = $Scope
            config_path = $Config
            endpoint = $endpoint
            codex_name = "powerfactory-agent"
            token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN"
            token_identity = Get-Sha256Hex $Token
            persistent_engine = "none"
        }
    } catch {
        Stop-CreatedProcess $process | Out-Null
        throw
    }
    if (-not (Wait-McpReady -Endpoint $endpoint -Token $Token -Process $process)) {
        Stop-CreatedProcess $process | Out-Null
        Stop-Install "The staged authenticated MCP endpoint was not ready." "MCP_HEALTH_FAILED"
    }
    return $process
}

function Stop-CreatedProcess {
    param([object]$Process)
    if (-not $Process -or $Process.HasExited) { return "already_stopped" }
    & $script:Taskkill /PID "$($Process.Id)" /T /F *> $null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) { return "stop_failed" }
    return "stopped"
}

function Invoke-AcquisitionProbe {
    param([string]$AgentExecutable, [string]$Source, [string]$Config, [string]$OwnershipPath)
    $arguments = "probe-acquisition --config `"$Config`""
    $process = Start-Process -FilePath $AgentExecutable -ArgumentList $arguments -WorkingDirectory $Source -WindowStyle Hidden -PassThru
    try {
        Write-AtomicJson $OwnershipPath @{
            schema_version = "powerfactory-mcp-process-ownership/v1"
            pid = $process.Id
            process_start_ticks = [int64]$process.StartTime.ToUniversalTime().Ticks
            command_kind = "acquisition_probe"
            scope = "disposable_probe"
            config_path = $Config
            persistent_engine = "disposable_child_only"
        }
    } catch {
        Stop-CreatedProcess $process | Out-Null
        throw
    }
    $deadline = (Get-Date).AddSeconds(240)
    while ((Get-Date) -lt $deadline -and -not $process.HasExited) { Start-Sleep -Milliseconds 250 }
    if (-not $process.HasExited) {
        Stop-CreatedProcess $process | Out-Null
        Stop-Install "PowerFactory acquisition validation timed out." "POWERFACTORY_PROBE_TIMEOUT"
    }
    $exitCode = $process.ExitCode
    Remove-Item -LiteralPath $OwnershipPath -Force -ErrorAction SilentlyContinue
    if ($exitCode -ne 0) { Stop-Install "PowerFactory acquisition validation failed." "POWERFACTORY_PROBE_FAILED" $exitCode }
}

function Stop-OwnedProcess {
    param([object]$Ledger)
    if (-not $Ledger -or -not $Ledger.pid) { return "not_recorded" }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($Ledger.pid)" -ErrorAction SilentlyContinue
    if (-not $process) { return "already_stopped" }
    $commandPattern = if ($Ledger.command_kind -eq "mcp_server") {
        'powerfactory-agent(?:\.exe)?["'']?\s+serve'
    } elseif ($Ledger.command_kind -eq "acquisition_probe") {
        'powerfactory-agent(?:\.exe)?["'']?\s+probe-acquisition'
    } else {
        return "not_owned"
    }
    if (-not $process.CommandLine -or $process.CommandLine -notmatch $commandPattern) { return "not_owned" }
    if ($Ledger.config_path -and $process.CommandLine -notmatch [regex]::Escape([string]$Ledger.config_path)) { return "identity_mismatch" }
    try { $startTicks = (Get-Process -Id $Ledger.pid -ErrorAction Stop).StartTime.ToUniversalTime().Ticks } catch { return "identity_mismatch" }
    if (-not $Ledger.process_start_ticks -or [int64]$Ledger.process_start_ticks -ne [int64]$startTicks) { return "identity_mismatch" }
    & $script:Taskkill /PID "$($Ledger.pid)" /T /F *> $null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $Ledger.pid -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Id $Ledger.pid -ErrorAction SilentlyContinue) { return "stop_failed" }
    return "stopped"
}

function Write-CodexLauncher {
    param([string]$Path, [string]$Root, [string]$Codex)
    $rootLiteral = $Root.Replace("'", "''"); $codexLiteral = $Codex.Replace("'", "''")
    $content = @"
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=`$true)][string[]]`$CodexArguments)
`$ErrorActionPreference = "Stop"
`$active = Get-Content -LiteralPath (Join-Path '$rootLiteral' "active.json") -Raw | ConvertFrom-Json
`$tokenPath = Join-Path `$active.release_path "state\\mcp-token"
if (-not (Test-Path -LiteralPath `$tokenPath)) { throw "PowerFactory MCP active credential is missing." }
`$env:POWERFACTORY_AGENT_MCP_TOKEN = (Get-Content -LiteralPath `$tokenPath -Raw).Trim()
& '$codexLiteral' @CodexArguments
exit `$LASTEXITCODE
"@
    $existing = if (Test-Path -LiteralPath $Path) { Get-Content -LiteralPath $Path -Raw } else { $null }
    if ($existing -ceq $content) { return "unchanged" }
    $script:PriorLauncherContent = $existing
    Write-AtomicText $Path $content
    $script:LauncherChanged = $true
    if ($null -eq $existing) { return "created" }
    return "updated"
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
    $tokenHash = Get-Sha256Hex $Token
    if ($ledger.token_identity -ne $tokenHash) { return $false }
    return $true
}

function Register-Codex {
    param([string]$Codex, [string]$Endpoint, [bool]$ReplacingExisting)
    if ($ReplacingExisting) {
        & $Codex mcp remove powerfactory-agent *> $null
        if ($LASTEXITCODE -ne 0) { Stop-Install "Could not remove the owned Codex registration." "CODEX_REGISTRATION_FAILED" $LASTEXITCODE }
        $script:CodexChanged = $true
    }
    & $Codex mcp add powerfactory-agent --url $Endpoint --bearer-token-env-var POWERFACTORY_AGENT_MCP_TOKEN
    if ($LASTEXITCODE -ne 0) {
        $observed = Get-CodexRegistrationFingerprint $Codex
        if ($observed.state -eq "present" -and $observed.fingerprint.endpoint -eq $Endpoint) {
            $script:CodexChanged = $true
        }
        Stop-Install "Codex registration failed." "CODEX_REGISTRATION_FAILED" $LASTEXITCODE
    }
    $script:CodexChanged = $true
}

function Remove-InstallerCodexRegistration {
    param([string]$Codex)
    & $Codex mcp remove powerfactory-agent *> $null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Codex registration cleanup failed" "CODEX_ROLLBACK_FAILED" $LASTEXITCODE }
}

function Restart-PriorRelease {
    param([object]$Prior)
    if (-not $Prior -or -not $script:PriorWasStopped) { return "not_needed" }
    try {
        $config = Join-Path $Prior.release_path "state\\powerfactory-agent.json"
        $token = (Get-Content -LiteralPath (Join-Path $Prior.release_path "state\\mcp-token") -Raw).Trim()
        $source = Join-Path $Prior.release_path "source"
        $agentExecutable = Join-Path $source ".venv\\Scripts\\powerfactory-agent.exe"
        $ownershipPath = Join-Path $Prior.release_path "ownership.json"
        $process = Start-McpServer -AgentExecutable $agentExecutable -Source $source -Config $config -ListenPort ([int]$Prior.port) -Token $token -OwnershipPath $ownershipPath -Scope "active_release"
        return "restarted:$($process.Id)"
    } catch { return "restart_failed" }
}

function Write-FailureReport {
    param([string]$Reports, [object]$ErrorRecord, [object]$Runtime, [System.Collections.IDictionary]$Rollback)
    $category = if ($ErrorRecord.Exception.Data["category"]) { $ErrorRecord.Exception.Data["category"] } else { "UNHANDLED" }
    $exitCode = if ($ErrorRecord.Exception.Data["exit_code"]) { [int]$ErrorRecord.Exception.Data["exit_code"] } else { 1 }
    $reportId = if ($script:Attempt) { $script:Attempt.id } else { "preflight-$([guid]::NewGuid().ToString('N'))" }
    $architecture = if ([Environment]::Is64BitProcess) { "AMD64" } else { "x86" }
    $report = [ordered]@{
        schema_version = "powerfactory-mcp-install-failure/v1"; attempt_id = $reportId
        stage = $script:Stage; category = $category; exception_type = $ErrorRecord.Exception.GetType().Name; exit_code = $exitCode
        commit = if ($script:Attempt) { $script:Attempt.commit } else { "unknown" }
        environment = @{ powerfactory_release="2026"; python_abi=if ($Runtime) { $Runtime.PythonVersion } else { "unknown" }; architecture=$architecture }
        rollback = $Rollback
    }
    New-Item -ItemType Directory -Path $Reports -Force -ErrorAction Stop | Out-Null
    Write-AtomicJson (Join-Path $Reports "$reportId.json") $report
}

function Invoke-InstallerRollback {
    param([object]$ErrorRecord, [string]$Reports, [object]$Runtime)

    $rollback = [ordered]@{}
    if ($script:StagedServer) { $rollback.staged_mcp = Stop-CreatedProcess $script:StagedServer }
    if ($script:FinalServer) { $rollback.final_mcp = Stop-CreatedProcess $script:FinalServer }
    if ($script:CodexChanged) {
        try {
            if ($script:Prior) {
                Register-Codex $script:Codex "http://127.0.0.1:$($script:Prior.port)/mcp" $true
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
    $rollback.prior_release = Restart-PriorRelease $script:Prior
    if ($script:LauncherChanged -and $script:LauncherPath) {
        try {
            if ($null -eq $script:PriorLauncherContent) {
                Remove-Item -LiteralPath $script:LauncherPath -Force -ErrorAction Stop
                $rollback.launcher = "removed_new_launcher"
            } else {
                Write-AtomicText $script:LauncherPath $script:PriorLauncherContent
                $rollback.launcher = "restored"
            }
        } catch { $rollback.launcher = "restore_failed" }
    } else {
        $rollback.launcher = "untouched"
    }
    # The installer only executes disposable acquisition probes. It never starts
    # a persistent PowerFactory owner, so rollback has no broad engine kill path.
    $rollback.powerfactory = "no_persistent_engine_created; acquisition workers are disposable"
    if ($script:Attempt) { $rollback.attempt = Remove-Attempt $script:Attempt.path $script:Icacls }
    try { Write-FailureReport $Reports $ErrorRecord $Runtime $rollback } catch { }
    return $rollback
}

$script:Uv = $null; $script:Git = $null; $script:Codex = $null; $script:Icacls = $null; $script:Taskkill = $null
$script:Runtime = $null
$root = [IO.Path]::GetFullPath($InstallRoot)
$attempts = Join-Path $root "attempts"; $releases = Join-Path $root "releases"; $reports = Join-Path $root "failure-reports"; $activePath = Join-Path $root "active.json"
$script:ManagedCleanupRoots = @($attempts, $releases)

if ($TestHarness) { return }

try {
    Invoke-Stage "preflight" {
        New-Item -ItemType Directory -Force -Path $attempts, $releases, $reports | Out-Null
        if ($env:OS -ne "Windows_NT") { Stop-Install "This installer supports Windows only." "PLATFORM_UNSUPPORTED" }
        if ($PSVersionTable.PSVersion -lt [version]"5.1") { Stop-Install "PowerShell 5.1 or newer is required." "PLATFORM_UNSUPPORTED" }
        $mutexDigest = (Get-Sha256Hex $root).Substring(0, 24)
        $script:Mutex = New-Object System.Threading.Mutex -ArgumentList @($false, "Local\\PowerFactoryMCP-$mutexDigest")
        try { $mutexAcquired = $script:Mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $mutexAcquired = $true }
        if (-not $mutexAcquired) { Stop-Install "Another PowerFactory MCP installation is already running." "INSTALLER_BUSY" }
        $script:Uv = Get-RequiredCommand "uv" "Install uv and rerun."
        $script:Git = Get-RequiredCommand "git" "Install Git and rerun."
        $script:Codex = Get-RequiredCommand "codex" "Install/authenticate Codex CLI and rerun."
        $script:Icacls = Get-RequiredCommand "icacls.exe" "Use a standard Windows installation with icacls.exe."
        $script:Taskkill = Get-RequiredCommand "taskkill.exe" "Use a standard Windows installation with taskkill.exe."
        $script:Runtime = Resolve-PowerFactoryRuntime $PowerFactoryPydPath
        $script:Prior = Read-JsonFile $activePath
        if ($script:Prior -and -not (Test-Path -LiteralPath $script:Prior.release_path)) { Stop-Install "The active release manifest points to a missing managed release." "ACTIVE_RELEASE_INVALID" }
        # Stale attempts never become active.  Their ownership ledgers are read
        # only to stop their recorded server, then their own directory is removed.
        Get-ChildItem -LiteralPath $attempts -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $attemptDirectory = $_.FullName
            Get-ChildItem -LiteralPath $attemptDirectory -Filter "*ownership.json" -File -ErrorAction SilentlyContinue | ForEach-Object {
                $ledger = Read-JsonFile $_.FullName
                if ($ledger) { Stop-OwnedProcess $ledger | Out-Null }
            }
            Remove-Attempt $attemptDirectory $script:Icacls | Out-Null
        }
        Get-ChildItem -LiteralPath $releases -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $releaseDirectory = $_.FullName
            $pendingMarker = Join-Path $releaseDirectory "install-pending.json"
            if (Test-Path -LiteralPath $pendingMarker) {
                if ($script:Prior -and $script:Prior.release_path -eq $releaseDirectory) {
                    Remove-Item -LiteralPath $pendingMarker -Force -ErrorAction SilentlyContinue
                } else {
                    Get-ChildItem -LiteralPath $releaseDirectory -Filter "*ownership.json" -File -ErrorAction SilentlyContinue | ForEach-Object {
                        $ledger = Read-JsonFile $_.FullName
                        if ($ledger) {
                            $stopResult = Stop-OwnedProcess $ledger
                            if ($stopResult -notin @("stopped", "already_stopped")) {
                                Stop-Install "A pending release process cannot be proven installer-owned." "OWNERSHIP_UNPROVEN"
                            }
                        }
                    }
                    Remove-Attempt $releaseDirectory $script:Icacls | Out-Null
                }
            }
        }
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
        New-Item -ItemType Directory -Path $script:Attempt.source -Force | Out-Null
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "init") "Source initialization failed"
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "remote", "add", "origin", $RepositoryUrl) "Source remote setup failed"
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "fetch", "--depth", "1", "origin", $Ref) "Source fetch failed"
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "checkout", "--detach", "FETCH_HEAD") "Source checkout failed"
        $script:Attempt.commit = (& $script:Git -C $script:Attempt.source rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $script:Attempt.commit -notmatch '^[0-9a-fA-F]{40}$') { Stop-Install "Could not determine staged commit." "SOURCE_INVALID" }
        if ($Ref -match '^[0-9a-fA-F]{40}$' -and $script:Attempt.commit -ne $Ref.ToLowerInvariant()) { Stop-Install "Staged commit does not match the bootstrap revision." "SOURCE_INVALID" }
    }

    Invoke-Stage "environment" {
        Push-Location $script:Attempt.source
        try { Invoke-CheckedCommand $script:Uv @("sync", "--locked", "--python", $script:Runtime.PythonVersion) "Python environment setup failed" }
        finally { Pop-Location }
        $script:AgentExecutable = Join-Path $script:Attempt.source ".venv\\Scripts\\powerfactory-agent.exe"
        if (-not (Test-Path -LiteralPath $script:AgentExecutable)) { Stop-Install "Staged PowerFactory MCP executable was not created." "ENVIRONMENT_INVALID" }
    }

    Invoke-Stage "secure_configuration" {
        New-Item -ItemType Directory -Path $script:Attempt.state -Force | Out-Null
        Set-AttemptPrivateAcl $script:Attempt.state $script:Icacls
        Push-Location $script:Attempt.source
        try {
            Invoke-CheckedCommand $script:AgentExecutable @("init", "--state-dir", $script:Attempt.state, "--port", "$Port") "MCP initialization failed"
            Invoke-CheckedCommand $script:AgentExecutable @("configure-probe", "--config", (Join-Path $script:Attempt.state "powerfactory-agent.json"), "--pyd-path", $script:Runtime.Path, "--python-version", $script:Runtime.PythonVersion, "--session-ownership", "product_owned") "PowerFactory installation configuration failed"
        } finally { Pop-Location }
        $script:Token = (Get-Content -LiteralPath (Join-Path $script:Attempt.state "mcp-token") -Raw).Trim()
        if ($script:Token.Length -lt 32) { Stop-Install "Staged MCP credential is invalid." "CREDENTIAL_INVALID" }
    }

    Invoke-Stage "temporary_mcp_health" {
        $temporaryPort = Get-FreeLoopbackPort
        $ownershipPath = Join-Path $script:Attempt.path "ownership.json"
        $script:StagedServer = Start-McpServer -AgentExecutable $script:AgentExecutable -Source $script:Attempt.source -Config (Join-Path $script:Attempt.state "powerfactory-agent.json") -ListenPort $temporaryPort -Token $script:Token -OwnershipPath $ownershipPath -Scope "staged_attempt"
    }

    Invoke-Stage "codex_registration" {
        $endpoint = "http://127.0.0.1:$Port/mcp"
        if ($script:Prior) {
            if (-not $script:PriorCodexOwned) {
                Stop-Install "Existing Codex registration is not provably owned. Resolve it manually with: codex mcp remove powerfactory-agent" "CODEX_OWNERSHIP_UNPROVEN"
            }
        } else {
            Register-Codex $script:Codex $endpoint $false
        }
    }

    Invoke-Stage "cutover_prior_service_drain" {
        if ($script:Prior) {
            $oldLedger = Read-JsonFile (Join-Path $script:Prior.release_path "ownership.json")
            $result = Stop-OwnedProcess $oldLedger
            if ($result -notin @("stopped", "already_stopped")) { Stop-Install "The active server cannot be proven installer-owned." "OWNERSHIP_UNPROVEN" }
            $script:PriorWasStopped = $result -eq "stopped"
        }
    }

    Invoke-Stage "acquisition_probe" {
        Push-Location $script:Attempt.source
        try { Invoke-AcquisitionProbe $script:AgentExecutable $script:Attempt.source (Join-Path $script:Attempt.state "powerfactory-agent.json") (Join-Path $script:Attempt.path "acquisition-ownership.json") }
        finally { Pop-Location }
    }

    Invoke-Stage "promotion" {
        if ($script:StagedServer) {
            $stagedStop = Stop-CreatedProcess $script:StagedServer
            if ($stagedStop -notin @("stopped", "already_stopped")) { Stop-Install "The staged MCP server did not stop for cutover." "PROCESS_STOP_FAILED" }
        }
        Write-AtomicJson (Join-Path $script:Attempt.path "install-pending.json") @{ schema_version="powerfactory-mcp-pending/v1"; attempt_id=$script:Attempt.id; commit=$script:Attempt.commit }
        $releaseName = "release-$($script:Attempt.commit.Substring(0, 12))-$($script:Attempt.id.Substring(8, 12))"
        $releasePath = Join-Path $releases $releaseName
        Move-Item -LiteralPath $script:Attempt.path -Destination $releasePath
        $script:Attempt.path = $releasePath; $script:Attempt.source = Join-Path $releasePath "source"; $script:Attempt.state = Join-Path $releasePath "state"
        $script:AgentExecutable = Join-Path $script:Attempt.source ".venv\\Scripts\\powerfactory-agent.exe"
        $script:FinalServer = Start-McpServer -AgentExecutable $script:AgentExecutable -Source $script:Attempt.source -Config (Join-Path $script:Attempt.state "powerfactory-agent.json") -ListenPort $Port -Token $script:Token -OwnershipPath (Join-Path $releasePath "ownership.json") -Scope "pending_release"
        Invoke-AcquisitionProbe $script:AgentExecutable $script:Attempt.source (Join-Path $script:Attempt.state "powerfactory-agent.json") (Join-Path $releasePath "acquisition-ownership.json")
        $script:LauncherPath = Join-Path $root "Start-PowerFactoryCodex.ps1"
        Write-CodexLauncher $script:LauncherPath $root $script:Codex | Out-Null
        Write-AtomicJson $activePath @{ schema_version="powerfactory-mcp-active/v1"; release_path=$releasePath; commit=$script:Attempt.commit; port=$Port }
        Remove-Item -LiteralPath (Join-Path $releasePath "install-pending.json") -Force -ErrorAction SilentlyContinue
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
