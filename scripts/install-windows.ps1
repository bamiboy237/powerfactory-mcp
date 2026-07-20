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
$script:CompatibleLegacyCodexRegistration = $false
$script:Token = $null
$script:AgentExecutable = $null
$script:LauncherChanged = $false
$script:PriorLauncherContent = $null
$script:LauncherPath = $null
$script:CredentialCreated = $false

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

    # Query only the product registration. A content-blind list command is used
    # only as a CLI liveness check when the target is absent; its version-specific
    # JSON collection shape is never parsed or persisted. Windows PowerShell 5.1
    # promotes native stderr to an ErrorRecord; Codex writes its normal not-found
    # result to stderr, so native exit codes must be collected with Continue.
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $output = & $Codex mcp get powerfactory-agent --json 2>$null
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    } catch {
        return [PSCustomObject]@{ state = "query_failed"; fingerprint = $null }
    }

    if ($exitCode -ne 0) {
        try {
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                & $Codex mcp list --json *> $null
                $listExitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
        } catch {
            return [PSCustomObject]@{ state = "query_failed"; fingerprint = $null }
        }
        if ($exitCode -eq 1 -and $listExitCode -eq 0) {
            return [PSCustomObject]@{ state = "absent"; fingerprint = $null }
        }
        return [PSCustomObject]@{ state = "query_failed"; fingerprint = $null }
    }
    if (-not $output) { return [PSCustomObject]@{ state = "unparseable"; fingerprint = $null } }
    try { $registration = ConvertFrom-Json -InputObject ($output -join [Environment]::NewLine) }
    catch { return [PSCustomObject]@{ state = "unparseable"; fingerprint = $null } }
    $fingerprint = ConvertTo-CodexRegistrationFingerprint $registration
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
    param([string]$Name, [scriptblock]$Action, [switch]$SkipPostActionInjection)
    $script:Stage = $Name
    & $Action
    if (-not $SkipPostActionInjection -and $env:POWERFACTORY_MCP_FAIL_STAGE -eq $Name) {
        Stop-Install "Failure injection requested after $Name." "INJECTED_FAILURE"
    }
}

function Invoke-PromotionCheckpoint {
    param([string]$Name)
    $script:Stage = "promotion:$Name"
    if ($env:POWERFACTORY_MCP_FAIL_STAGE -eq $script:Stage) {
        Stop-Install "Failure injection requested after $script:Stage." "INJECTED_FAILURE"
    }
}

function Write-AttemptOwnership {
    param([object]$Attempt)
    Write-AtomicJson (Join-Path $Attempt.path "attempt-ownership.json") @{
        schema_version = "powerfactory-mcp-attempt-ownership/v1"
        attempt_id = $Attempt.id
        path = $Attempt.path
        commit = $Attempt.commit
    }
}

function Test-AttemptOwnership {
    param([string]$Path)
    $ledger = Read-JsonFile (Join-Path $Path "attempt-ownership.json")
    if (-not $ledger -or $ledger.schema_version -ne "powerfactory-mcp-attempt-ownership/v1") { return $false }
    if (-not $ledger.attempt_id -or -not $ledger.path -or -not $ledger.commit) { return $false }
    if ([string]$ledger.attempt_id -notmatch '^attempt-[0-9a-fA-F]{32}$') { return $false }
    if ((Split-Path -Leaf $Path) -ne [string]$ledger.attempt_id) { return $false }
    return [IO.Path]::GetFullPath([string]$ledger.path).TrimEnd('\\') -eq [IO.Path]::GetFullPath($Path).TrimEnd('\\')
}

function Get-LegacyAttemptSourceCommit {
    param([string]$Git, [string]$Source, [string]$ExpectedRepositoryUrl)

    $gitDirectory = Join-Path $Source ".git"
    $gitItem = Get-Item -LiteralPath $gitDirectory -Force -ErrorAction SilentlyContinue
    if (-not $gitItem -or -not $gitItem.PSIsContainer -or ($gitItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        return $null
    }

    $originOutput = & $Git -C $Source remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $origin = (($originOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim().TrimEnd('/')
    $expected = $ExpectedRepositoryUrl.Trim().TrimEnd('/')
    if (-not $origin.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) { return $null }

    $commitOutput = & $Git -C $Source rev-parse HEAD 2>$null
    $commit = (($commitOutput | ForEach-Object { [string]$_ }) -join "").Trim().ToLowerInvariant()
    if ($LASTEXITCODE -eq 0 -and $commit -match '^[0-9a-f]{40}$') { return $commit }
    # A failed fetch can leave a valid repository with no commit checked out.
    return "legacy-source-unresolved"
}

function Test-LegacyProcessOwnership {
    param([string]$Path, [string]$ExpectedCommandKind, [string]$ExpectedScope, [string]$ExpectedConfigPath)

    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $ledger = Read-JsonFile $Path
    if (-not $ledger -or $ledger.schema_version -ne "powerfactory-mcp-process-ownership/v1") { return $false }
    if (-not $ledger.pid -or -not $ledger.process_start_ticks -or -not $ledger.config_path) { return $false }
    if ($ledger.command_kind -ne $ExpectedCommandKind -or $ledger.scope -ne $ExpectedScope) { return $false }
    if ([IO.Path]::GetFullPath([string]$ledger.config_path).TrimEnd('\\') -ne [IO.Path]::GetFullPath($ExpectedConfigPath).TrimEnd('\\')) { return $false }
    if ($ExpectedCommandKind -eq "mcp_server" -and [string]$ledger.endpoint -notmatch '^http://127\.0\.0\.1:\d+/mcp$') { return $false }
    return $true
}

function Test-LegacyPendingMarker {
    param([string]$Path, [string]$ExpectedAttemptId, [string]$ExpectedCommit)

    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $marker = Read-JsonFile $Path
    if (-not $marker -or $marker.schema_version -ne "powerfactory-mcp-pending/v1") { return $false }
    if ($marker.attempt_id -ne $ExpectedAttemptId -or $marker.commit -ne $ExpectedCommit) { return $false }
    return [string]$marker.commit -match '^[0-9a-fA-F]{40}$'
}

function Initialize-LegacyAttemptOwnership {
    param(
        [string]$Path,
        [string]$AttemptsRoot,
        [string]$Git,
        [string]$ExpectedRepositoryUrl
    )

    try {
        $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\\')
        $fullRoot = [IO.Path]::GetFullPath($AttemptsRoot).TrimEnd('\\')
        $attemptId = Split-Path -Leaf $fullPath
        $attemptParent = Split-Path -Parent $fullPath
        if ($attemptId -notmatch '^attempt-[0-9a-fA-F]{32}$') { return $false }
        if (-not $attemptParent.Equals($fullRoot, [StringComparison]::OrdinalIgnoreCase)) { return $false }

        $attemptItem = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
        if (-not $attemptItem.PSIsContainer -or ($attemptItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }

        $entries = @(Get-ChildItem -LiteralPath $fullPath -Force -ErrorAction Stop)
        if ($entries.Count -eq 0) {
            $commit = "legacy-empty"
        } else {
            $allowedNames = @("source", "state", "ownership.json", "acquisition-ownership.json", "install-pending.json")
            $unexpected = @($entries | Where-Object { $_.Name -notin $allowedNames })
            if ($unexpected.Count -ne 0) { return $false }
            if (@($entries | Where-Object { $_.Name -in @("source", "state") -and -not $_.PSIsContainer }).Count -ne 0) { return $false }
            if (@($entries | Where-Object { $_.Name -notin @("source", "state") -and $_.PSIsContainer }).Count -ne 0) { return $false }
            if (@(Get-ChildItem -LiteralPath $fullPath -Force -Recurse -ErrorAction Stop | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count -ne 0) { return $false }

            $source = Join-Path $fullPath "source"
            if (-not (Test-Path -LiteralPath $source -PathType Container)) { return $false }
            $sourceEntries = @(Get-ChildItem -LiteralPath $source -Force -ErrorAction Stop)
            if ($sourceEntries.Count -eq 0 -and $entries.Count -eq 1) {
                $commit = "legacy-empty-source"
            } else {
                $commit = Get-LegacyAttemptSourceCommit $Git $source $ExpectedRepositoryUrl
                if (-not $commit) { return $false }
            }

            $state = Join-Path $fullPath "state"
            $config = Join-Path $state "powerfactory-agent.json"
            if (-not (Test-LegacyProcessOwnership (Join-Path $fullPath "ownership.json") "mcp_server" "staged_attempt" $config)) { return $false }
            if (-not (Test-LegacyProcessOwnership (Join-Path $fullPath "acquisition-ownership.json") "acquisition_probe" "disposable_probe" $config)) { return $false }
            if (-not (Test-LegacyPendingMarker (Join-Path $fullPath "install-pending.json") $attemptId $commit)) { return $false }
        }

        # Older transactional releases created this exact staged layout before
        # the durable ownership ledger existed. Adoption is recorded before any
        # cleanup so a later interruption remains recoverable.
        Write-AtomicJson (Join-Path $fullPath "attempt-ownership.json") @{
            schema_version = "powerfactory-mcp-attempt-ownership/v1"
            attempt_id = $attemptId
            path = $fullPath
            commit = $commit
            migration_source = "legacy-transaction-v0"
        }
        return Test-AttemptOwnership $fullPath
    }
    catch { return $false }
}

function Test-PendingReleaseOwnership {
    param([string]$Path)
    $marker = Read-JsonFile (Join-Path $Path "install-pending.json")
    if (-not $marker -or $marker.schema_version -ne "powerfactory-mcp-pending/v1") { return $false }
    $attempt = Read-JsonFile (Join-Path $Path "attempt-ownership.json")
    if (-not $attempt -or $attempt.schema_version -ne "powerfactory-mcp-attempt-ownership/v1") { return $false }
    if (-not $marker.attempt_id -or -not $marker.commit -or -not $marker.release_path) { return $false }
    if ([string]$marker.attempt_id -notmatch '^attempt-[0-9a-fA-F]{32}$' -or [string]$marker.commit -notmatch '^[0-9a-fA-F]{40}$') { return $false }
    if ($attempt.attempt_id -ne $marker.attempt_id -or $attempt.commit -ne $marker.commit) { return $false }
    $expectedName = "release-$(([string]$marker.commit).Substring(0, 12))-$(([string]$marker.attempt_id).Substring(8, 12))"
    if ((Split-Path -Leaf $Path) -ne $expectedName) { return $false }
    return [IO.Path]::GetFullPath([string]$marker.release_path).TrimEnd('\\') -eq [IO.Path]::GetFullPath($Path).TrimEnd('\\')
}

function Invoke-CheckedCommand {
    param([string]$FilePath, [string[]]$ArgumentList, [string]$Failure)
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) { Stop-Install "$Failure (exit code $LASTEXITCODE)." "COMMAND_FAILED" $LASTEXITCODE }
}

function Get-StagedCommit {
    param([string]$Git, [string]$Source)
    $commit = (& $Git -C $Source rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-fA-F]{40}$') {
        Stop-Install "Could not determine staged commit." "SOURCE_INVALID"
    }
    return $commit
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
    $backup = "$Path.$([guid]::NewGuid().ToString('N')).bak"
    try {
        $json = $Value | ConvertTo-Json -Depth 8
        # Windows PowerShell's UTF8 encoding writes a BOM, which Python's
        # strict UTF-8 JSON reader rejects for powerfactory-agent.json.
        $utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
        [System.IO.File]::WriteAllText($temporary, $json, $utf8NoBom)
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($temporary, $Path, $backup)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
}

function Test-PathWithinRoot {
    param([string]$Path, [string]$Root)
    $pathFull = [IO.Path]::GetFullPath($Path)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if ($pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    $separator = [IO.Path]::DirectorySeparatorChar
    return $pathFull.StartsWith("$rootFull$separator", [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-RebasedReleaseStatePath {
    param(
        [string]$Value,
        [string]$AttemptStatePath,
        [string]$ReleaseStatePath,
        [string]$FieldName
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        Stop-Install "MCP installation configuration is missing $FieldName." "CONFIG_INVALID"
    }
    try { $sourcePath = [IO.Path]::GetFullPath($Value) }
    catch { Stop-Install "MCP installation configuration has an invalid $FieldName path." "CONFIG_INVALID" }
    if (-not (Test-PathWithinRoot $sourcePath $AttemptStatePath)) {
        Stop-Install "MCP installation configuration $FieldName is outside the staged state directory." "CONFIG_INVALID"
    }
    $attemptStateFull = [IO.Path]::GetFullPath($AttemptStatePath).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $relativePath = $sourcePath.Substring($attemptStateFull.Length).TrimStart([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $rebasedPath = [IO.Path]::GetFullPath((Join-Path $ReleaseStatePath $relativePath))
    if (-not (Test-PathWithinRoot $rebasedPath $ReleaseStatePath)) {
        Stop-Install "MCP installation configuration $FieldName cannot be rebased into the release state directory." "CONFIG_INVALID"
    }
    return $rebasedPath
}

function Rebase-McpInstallationPaths {
    param([string]$ConfigPath, [string]$AttemptStatePath, [string]$ReleaseStatePath)
    $configuration = Read-JsonFile $ConfigPath
    if (-not $configuration) {
        Stop-Install "MCP installation configuration is missing after release promotion." "CONFIG_INVALID"
    }
    foreach ($field in @("token_file", "log_file", "probe_config_file")) {
        if (-not $configuration.PSObject.Properties[$field] -or $null -eq $configuration.$field) {
            Stop-Install "MCP installation configuration is missing $field." "CONFIG_INVALID"
        }
        $configuration.$field = Get-RebasedReleaseStatePath ([string]$configuration.$field) $AttemptStatePath $ReleaseStatePath $field
    }
    foreach ($field in @("token_file", "log_file", "probe_config_file")) {
        if (-not (Test-PathWithinRoot ([string]$configuration.$field) $ReleaseStatePath) -or (Test-PathWithinRoot ([string]$configuration.$field) $AttemptStatePath)) {
            Stop-Install "MCP installation configuration $field is not bound to the promoted state directory." "CONFIG_INVALID"
        }
    }
    foreach ($field in @("token_file", "probe_config_file")) {
        if (-not (Test-Path -LiteralPath $configuration.$field -PathType Leaf)) {
            Stop-Install "Promoted MCP installation $field is missing." "CONFIG_INVALID"
        }
    }
    $logParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($configuration.log_file))
    $releaseStateFull = [IO.Path]::GetFullPath($ReleaseStatePath).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if (-not $logParent.Equals($releaseStateFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Install "Promoted MCP installation log_file must be rooted in the release state directory." "CONFIG_INVALID"
    }
    Write-AtomicJson $ConfigPath $configuration
}

function Write-AtomicText {
    param([string]$Path, [string]$Value)
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    $backup = "$Path.$([guid]::NewGuid().ToString('N')).bak"
    try {
        Set-Content -LiteralPath $temporary -Value $Value -Encoding UTF8 -NoNewline
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($temporary, $Path, $backup)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
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
    if (-not $script:CredentialCreated) {
        $rollback.credentials = "not_created"
    } elseif ($rollback.attempt -in @("removed", "removed_after_attempt_acl_repair", "not_present")) {
        $rollback.credentials = "removed_with_attempt"
    } else {
        $rollback.credentials = "retained_attempt_cleanup_failed"
    }
    try { Write-FailureReport $Reports $ErrorRecord $Runtime $rollback } catch { }
    return $rollback
}

$script:Uv = $null; $script:Git = $null; $script:Codex = $null; $script:Icacls = $null; $script:Taskkill = $null
$script:Runtime = $null
$script:ManagedCleanupRoots = @()

function Invoke-InstallerTransaction {
    param([string]$TransactionRoot = $InstallRoot)

    # This is intentionally the sole orchestrator. Tests invoke it with the
    # same stage functions and rollback path as a real installation.
    $root = [IO.Path]::GetFullPath($TransactionRoot)
    $attempts = Join-Path $root "attempts"
    $releases = Join-Path $root "releases"
    $reports = Join-Path $root "failure-reports"
    $activePath = Join-Path $root "active.json"
    $script:ManagedCleanupRoots = @($attempts, $releases)
    $script:Stage = "preflight"
    $script:Attempt = $null
    $script:Prior = $null
    $script:PriorCodexOwned = $false
    $script:PriorRegistration = $null
    $script:CompatibleLegacyCodexRegistration = $false
    $script:PriorWasStopped = $false
    $script:CodexChanged = $false
    $script:StagedServer = $null
    $script:FinalServer = $null
    $script:Mutex = $null
    $script:Token = $null
    $script:AgentExecutable = $null
    $script:CredentialCreated = $false
    $script:LauncherChanged = $false
    $script:PriorLauncherContent = $null
    $script:LauncherPath = $null

try {
    Invoke-Stage "preflight" {
        New-Item -ItemType Directory -Force -Path $attempts, $releases, $reports | Out-Null
        if ($env:OS -ne "Windows_NT") { Stop-Install "This installer supports Windows only." "PLATFORM_UNSUPPORTED" }
        if ($PSVersionTable.PSVersion -lt [version]"5.1") { Stop-Install "PowerShell 5.1 or newer is required." "PLATFORM_UNSUPPORTED" }
        $mutexDigest = (Get-Sha256Hex $root).Substring(0, 24)
        $script:Mutex = New-Object System.Threading.Mutex -ArgumentList @($false, "Local\PowerFactoryMCP-$mutexDigest")
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
            if (-not (Test-AttemptOwnership $attemptDirectory)) {
                if (-not (Initialize-LegacyAttemptOwnership $attemptDirectory $attempts $script:Git $RepositoryUrl)) {
                    Stop-Install "A stale attempt cannot be proven installer-owned." "OWNERSHIP_UNPROVEN"
                }
            }
            Get-ChildItem -LiteralPath $attemptDirectory -Filter "*ownership.json" -File -ErrorAction SilentlyContinue | ForEach-Object {
                $ledger = Read-JsonFile $_.FullName
                if ($ledger -and $ledger.schema_version -eq "powerfactory-mcp-process-ownership/v1") {
                    $stopResult = Stop-OwnedProcess $ledger
                    if ($stopResult -notin @("stopped", "already_stopped")) {
                        Stop-Install "A stale attempt process cannot be proven installer-owned." "OWNERSHIP_UNPROVEN"
                    }
                }
            }
            $removeResult = Remove-Attempt $attemptDirectory $script:Icacls
            if ($removeResult -notin @("removed", "removed_after_attempt_acl_repair", "not_present")) {
                Stop-Install "A stale attempt could not be removed safely." "STALE_ATTEMPT_RECOVERY_FAILED"
            }
        }
        Get-ChildItem -LiteralPath $releases -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $releaseDirectory = $_.FullName
            $pendingMarker = Join-Path $releaseDirectory "install-pending.json"
            if (Test-Path -LiteralPath $pendingMarker) {
                if ($script:Prior -and $script:Prior.release_path -eq $releaseDirectory) {
                    Remove-Item -LiteralPath $pendingMarker -Force -ErrorAction SilentlyContinue
                } else {
                    if (-not (Test-PendingReleaseOwnership $releaseDirectory)) {
                        Stop-Install "A pending release cannot be proven installer-owned." "OWNERSHIP_UNPROVEN"
                    }
                    Get-ChildItem -LiteralPath $releaseDirectory -Filter "*ownership.json" -File -ErrorAction SilentlyContinue | ForEach-Object {
                        $ledger = Read-JsonFile $_.FullName
                        if ($ledger -and $ledger.schema_version -eq "powerfactory-mcp-process-ownership/v1") {
                            $stopResult = Stop-OwnedProcess $ledger
                            if ($stopResult -notin @("stopped", "already_stopped")) {
                                Stop-Install "A pending release process cannot be proven installer-owned." "OWNERSHIP_UNPROVEN"
                            }
                        }
                    }
                    $removeResult = Remove-Attempt $releaseDirectory $script:Icacls
                    if ($removeResult -notin @("removed", "removed_after_attempt_acl_repair", "not_present")) {
                        Stop-Install "A pending release could not be removed safely." "STALE_RELEASE_RECOVERY_FAILED"
                    }
                }
            }
        }
        if ($script:Prior) {
            $priorToken = (Get-Content -LiteralPath (Join-Path $script:Prior.release_path "state\\mcp-token") -Raw).Trim()
            $script:PriorRegistration = Get-CodexRegistrationFingerprint $script:Codex
            $script:PriorCodexOwned = Test-OwnedCodexRegistration $script:PriorRegistration $script:Prior "http://127.0.0.1:$($script:Prior.port)/mcp" $priorToken
        } else {
            $script:PriorRegistration = Get-CodexRegistrationFingerprint $script:Codex
            if ($script:PriorRegistration.state -eq "present" -and $script:PriorRegistration.fingerprint.endpoint -eq "http://127.0.0.1:$Port/mcp") {
                # Earlier product installers registered the same fixed local MCP
                # identity but did not create active.json or an ownership ledger.
                # Preserve that exact registration; do not infer or modify any
                # legacy files or processes from its presence.
                $script:CompatibleLegacyCodexRegistration = $true
            } elseif ($script:PriorRegistration.state -ne "absent") {
                Stop-Install "Existing Codex registration is not compatible with this PowerFactory MCP endpoint. Resolve it manually with: codex mcp remove powerfactory-agent" "CODEX_OWNERSHIP_UNPROVEN"
            }
        }
    }

    Invoke-Stage "source" {
        $id = "attempt-$([guid]::NewGuid().ToString('N'))"; $path = Join-Path $attempts $id
        $script:Attempt = [PSCustomObject]@{ id=$id; path=$path; source=(Join-Path $path "source"); state=(Join-Path $path "state"); commit="unknown" }
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        # Ownership is durable before ACL or source setup can fail.
        Write-AttemptOwnership $script:Attempt
        Set-AttemptPrivateAcl $path $script:Icacls
        New-Item -ItemType Directory -Path $script:Attempt.source -Force | Out-Null
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "init") "Source initialization failed"
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "remote", "add", "origin", $RepositoryUrl) "Source remote setup failed"
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "fetch", "--depth", "1", "origin", $Ref) "Source fetch failed"
        Invoke-CheckedCommand $script:Git @("-C", $script:Attempt.source, "checkout", "--detach", "FETCH_HEAD") "Source checkout failed"
        $script:Attempt.commit = Get-StagedCommit $script:Git $script:Attempt.source
        if ($Ref -match '^[0-9a-fA-F]{40}$' -and $script:Attempt.commit -ne $Ref.ToLowerInvariant()) { Stop-Install "Staged commit does not match the bootstrap revision." "SOURCE_INVALID" }
        Write-AttemptOwnership $script:Attempt
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
        $script:CredentialCreated = $true
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
        } elseif (-not $script:CompatibleLegacyCodexRegistration) {
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

    Invoke-Stage "promotion" -SkipPostActionInjection {
        if ($script:StagedServer) {
            $stagedStop = Stop-CreatedProcess $script:StagedServer
            if ($stagedStop -notin @("stopped", "already_stopped")) { Stop-Install "The staged MCP server did not stop for cutover." "PROCESS_STOP_FAILED" }
            $script:StagedServer = $null
        }
        $releaseName = "release-$($script:Attempt.commit.Substring(0, 12))-$($script:Attempt.id.Substring(8, 12))"
        $releasePath = Join-Path $releases $releaseName
        Write-AtomicJson (Join-Path $script:Attempt.path "install-pending.json") @{ schema_version="powerfactory-mcp-pending/v1"; attempt_id=$script:Attempt.id; commit=$script:Attempt.commit; release_path=$releasePath }
        $attemptStatePath = $script:Attempt.state
        Move-Item -LiteralPath $script:Attempt.path -Destination $releasePath
        $script:Attempt.path = $releasePath; $script:Attempt.source = Join-Path $releasePath "source"; $script:Attempt.state = Join-Path $releasePath "state"
        $script:AgentExecutable = Join-Path $script:Attempt.source ".venv\\Scripts\\powerfactory-agent.exe"
        Rebase-McpInstallationPaths (Join-Path $script:Attempt.state "powerfactory-agent.json") $attemptStatePath $script:Attempt.state
        Invoke-PromotionCheckpoint "directory_move"
        $script:FinalServer = Start-McpServer -AgentExecutable $script:AgentExecutable -Source $script:Attempt.source -Config (Join-Path $script:Attempt.state "powerfactory-agent.json") -ListenPort $Port -Token $script:Token -OwnershipPath (Join-Path $releasePath "ownership.json") -Scope "pending_release"
        Invoke-PromotionCheckpoint "final_mcp_health"
        Invoke-AcquisitionProbe $script:AgentExecutable $script:Attempt.source (Join-Path $script:Attempt.state "powerfactory-agent.json") (Join-Path $releasePath "acquisition-ownership.json")
        Invoke-PromotionCheckpoint "final_acquisition_probe"
        $script:LauncherPath = Join-Path $root "Start-PowerFactoryCodex.ps1"
        Write-CodexLauncher $script:LauncherPath $root $script:Codex | Out-Null
        Invoke-PromotionCheckpoint "launcher_update"
        Invoke-PromotionCheckpoint "before_active_manifest"
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
}

if (-not $TestHarness) {
    Invoke-InstallerTransaction
}
