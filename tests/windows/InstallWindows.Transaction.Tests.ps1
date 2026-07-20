# These tests invoke the production transaction function. External commands and
# PowerFactory are mocked; filesystem state and rollback decisions are real.
$failureCases = @(
    @{ Name = "preflight"; Stage = "preflight"; Attempt = $false; Stops = 0; RegistrationRemoved = 0; CredentialState = "not_created" },
    @{ Name = "source"; Stage = "source"; Attempt = $true; Stops = 0; RegistrationRemoved = 0; CredentialState = "not_created" },
    @{ Name = "environment"; Stage = "environment"; Attempt = $true; Stops = 0; RegistrationRemoved = 0; CredentialState = "not_created" },
    @{ Name = "secure configuration"; Stage = "secure_configuration"; Attempt = $true; Stops = 0; RegistrationRemoved = 0; CredentialState = "removed_with_attempt" },
    @{ Name = "temporary MCP health"; Stage = "temporary_mcp_health"; Attempt = $true; Stops = 1; RegistrationRemoved = 0; CredentialState = "removed_with_attempt" },
    @{ Name = "Codex registration"; Stage = "codex_registration"; Attempt = $true; Stops = 1; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "prior service drain"; Stage = "cutover_prior_service_drain"; Attempt = $true; Stops = 1; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "acquisition probe"; Stage = "acquisition_probe"; Attempt = $true; Stops = 1; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "release move"; Stage = "promotion:directory_move"; Attempt = $true; Stops = 1; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "final MCP health"; Stage = "promotion:final_mcp_health"; Attempt = $true; Stops = 2; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "final acquisition"; Stage = "promotion:final_acquisition_probe"; Attempt = $true; Stops = 2; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "launcher update"; Stage = "promotion:launcher_update"; Attempt = $true; Stops = 2; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" },
    @{ Name = "active manifest commit"; Stage = "promotion:before_active_manifest"; Attempt = $true; Stops = 2; RegistrationRemoved = 1; CredentialState = "removed_with_attempt" }
)

Describe "PowerFactory MCP transactional installer" {
    BeforeAll {
        $script:InstallerPath = Join-Path $PSScriptRoot "..\..\scripts\install-windows.ps1"
        . $script:InstallerPath -TestHarness
    }

    BeforeEach {
        $script:CaseRoot = Join-Path $TestDrive "case-$([guid]::NewGuid().ToString('N'))"
        $script:attempts = Join-Path $script:CaseRoot "attempts"
        $script:releases = Join-Path $script:CaseRoot "releases"
        $script:reports = Join-Path $script:CaseRoot "failure-reports"
        $script:activePath = Join-Path $script:CaseRoot "active.json"
        $script:UnrelatedPath = Join-Path $TestDrive "unrelated"
        New-Item -ItemType Directory -Path $script:attempts, $script:releases, $script:reports, $script:UnrelatedPath -Force | Out-Null
        "preserve" | Set-Content -LiteralPath (Join-Path $script:UnrelatedPath "sentinel.txt")
        $script:OriginalOS = $env:OS
        $env:OS = "Windows_NT"

        Mock Get-RequiredCommand { "tool.exe" }
        Mock Resolve-PowerFactoryRuntime { [PSCustomObject]@{ PythonVersion = "3.14"; Path = "C:\PowerFactory\powerfactory.pyd" } }
        Mock Get-CodexRegistrationFingerprint { [PSCustomObject]@{ state = "absent"; fingerprint = $null } }
        Mock Invoke-CheckedCommand {
            param($FilePath, $ArgumentList, $Failure)
            if ($Failure -eq "Python environment setup failed") {
                $bin = Join-Path $script:Attempt.source ".venv\Scripts"
                New-Item -ItemType Directory -Path $bin -Force | Out-Null
                New-Item -ItemType File -Path (Join-Path $bin "powerfactory-agent.exe") -Force | Out-Null
            }
            if ($Failure -eq "MCP initialization failed") {
                New-Item -ItemType Directory -Path $script:Attempt.state -Force | Out-Null
                "0123456789012345678901234567890123456789" | Set-Content -LiteralPath (Join-Path $script:Attempt.state "mcp-token") -NoNewline
            }
            if ($Failure -eq "PowerFactory installation configuration failed") {
                $state = $script:Attempt.state
                New-Item -ItemType File -Path (Join-Path $state "powerfactory-probe.json") -Force | Out-Null
                [ordered]@{
                    schema_version = "powerfactory-agent-mcp-install/v1"
                    host = "127.0.0.1"
                    port = 8787
                    token_file = Join-Path $state "mcp-token"
                    probe_config_file = Join-Path $state "powerfactory-probe.json"
                    log_file = Join-Path $state "powerfactory-agent.log"
                } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $state "powerfactory-agent.json") -NoNewline
            }
        }
        Mock Get-StagedCommit { "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
        Mock Set-AttemptPrivateAcl {}
        Mock Start-McpServer { [PSCustomObject]@{ Id = 41001; HasExited = $false } }
        Mock Stop-CreatedProcess { "stopped" }
        Mock Stop-OwnedProcess { "already_stopped" }
        Mock Invoke-AcquisitionProbe {}
        Mock Register-Codex { $script:CodexChanged = $true }
        Mock Remove-InstallerCodexRegistration {}
        Mock Restart-PriorRelease { "not_needed" }
        Mock Get-FreeLoopbackPort { 41234 }
        Mock Test-McpInitialize { $true }
    }

    AfterEach {
        if ($null -eq $script:OriginalOS) { Remove-Item Env:OS -ErrorAction SilentlyContinue } else { $env:OS = $script:OriginalOS }
        Remove-Item Env:POWERFACTORY_MCP_FAIL_STAGE -ErrorAction SilentlyContinue
    }

    It "rolls back only resources created before <Name>" -ForEach $failureCases {
        param($Name, $Stage, $Attempt, $Stops, $RegistrationRemoved, $CredentialState)
        $env:POWERFACTORY_MCP_FAIL_STAGE = $Stage

        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught | Should -Not -BeNullOrEmpty
        $caught.Exception.Data["category"] | Should -Be "INJECTED_FAILURE"
        Test-Path -LiteralPath $script:activePath | Should -BeFalse
        Should -Invoke Stop-CreatedProcess -Times $Stops -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times $RegistrationRemoved -Exactly
        Should -Invoke Register-Codex -Times $(if ($RegistrationRemoved) { 1 } else { 0 }) -Exactly
        if ($Attempt) {
            @(Get-ChildItem -LiteralPath $script:attempts -Directory).Count | Should -Be 0
            @(Get-ChildItem -LiteralPath $script:releases -Directory).Count | Should -Be 0
        }
        Test-Path -LiteralPath (Join-Path $script:UnrelatedPath "sentinel.txt") | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $script:CaseRoot "Start-PowerFactoryCodex.ps1") | Should -BeFalse

        $reportFiles = @(Get-ChildItem -LiteralPath $script:reports -Filter "*.json")
        $reportFiles.Count | Should -Be 1
        $reportText = Get-Content -LiteralPath $reportFiles[0].FullName -Raw
        $report = $reportText | ConvertFrom-Json
        $report.stage | Should -Be $Stage
        $report.category | Should -Be "INJECTED_FAILURE"
        $report.rollback.credentials | Should -Be $CredentialState
        $reportText | Should -Not -Match "0123456789012345678901234567890123456789|Bearer|password|licen[cs]e|customer model"
    }

    It "leaves an owned prior registration untouched and restarts only the service it stopped" {
        $prior = Join-Path $script:releases "release-prior"
        New-Item -ItemType Directory -Path (Join-Path $prior "state"), (Join-Path $prior "source\.venv\Scripts") -Force | Out-Null
        "token" | Set-Content -LiteralPath (Join-Path $prior "state\mcp-token")
        '{"schema_version":"powerfactory-mcp-process-ownership/v1","pid":1,"process_start_ticks":1,"command_kind":"mcp_server","config_path":"config"}' | Set-Content -LiteralPath (Join-Path $prior "ownership.json")
        @{ schema_version = "powerfactory-mcp-active/v1"; release_path = $prior; commit = "known-good"; port = 8787 } | ConvertTo-Json | Set-Content -LiteralPath $script:activePath
        $expectedActiveManifest = Get-Content -LiteralPath $script:activePath -Raw
        Mock Get-CodexRegistrationFingerprint { [PSCustomObject]@{ state = "present"; fingerprint = [PSCustomObject]@{ name = "powerfactory-agent"; endpoint = "http://127.0.0.1:8787/mcp"; token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN" } } }
        Mock Test-OwnedCodexRegistration { $true }
        Mock Stop-OwnedProcess { "stopped" }
        Mock Restart-PriorRelease { "restarted:41003" }
        $env:POWERFACTORY_MCP_FAIL_STAGE = "acquisition_probe"

        { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } | Should -Throw

        (Get-Content -LiteralPath $script:activePath -Raw) | Should -Be $expectedActiveManifest
        Should -Invoke Register-Codex -Times 0 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
        Should -Invoke Restart-PriorRelease -Times 1 -Exactly
        Test-Path -LiteralPath $prior | Should -BeTrue
    }

    It "completes a fresh install and an idempotent rerun without rewriting Codex registration" {
        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot
        $first = Get-Content -LiteralPath $script:activePath -Raw | ConvertFrom-Json
        Test-Path -LiteralPath $first.release_path | Should -BeTrue
        $first.commit | Should -Be "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        Mock Get-CodexRegistrationFingerprint { [PSCustomObject]@{ state = "present"; fingerprint = [PSCustomObject]@{ name = "powerfactory-agent"; endpoint = "http://127.0.0.1:8787/mcp"; token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN" } } }
        Mock Test-OwnedCodexRegistration { $true }
        Mock Stop-OwnedProcess { "stopped" }
        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        $second = Get-Content -LiteralPath $script:activePath -Raw | ConvertFrom-Json
        $second.commit | Should -Be $first.commit
        Test-Path -LiteralPath $first.release_path | Should -BeTrue
        Test-Path -LiteralPath $second.release_path | Should -BeTrue
        Should -Invoke Register-Codex -Times 1 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
        @(Get-ChildItem -LiteralPath $script:attempts -Directory).Count | Should -Be 0
        @(Get-ChildItem -LiteralPath $script:reports -Filter "*.json").Count | Should -Be 0
    }

    It "rebases all MCP installation paths before starting the moved release" {
        $script:StartedServers = @()
        Mock Start-McpServer {
            param($AgentExecutable, $Source, $Config, $ListenPort, $Token, $OwnershipPath, $Scope)
            $script:StartedServers += [PSCustomObject]@{ Config = $Config; Scope = $Scope }
            [PSCustomObject]@{ Id = 41001; HasExited = $false }
        }

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        $staged = $script:StartedServers | Where-Object { $_.Scope -eq "staged_attempt" } | Select-Object -First 1
        $final = $script:StartedServers | Where-Object { $_.Scope -eq "pending_release" } | Select-Object -First 1
        $staged | Should -Not -BeNullOrEmpty
        $final | Should -Not -BeNullOrEmpty
        $stagedState = Split-Path -Parent $staged.Config
        $configurationText = Get-Content -LiteralPath $final.Config -Raw
        $configuration = $configurationText | ConvertFrom-Json
        foreach ($field in @("token_file", "log_file", "probe_config_file")) {
            $configuration.$field | Should -Match ([regex]::Escape((Split-Path -Parent $final.Config)))
            $configuration.$field | Should -Not -Match ([regex]::Escape($stagedState))
        }
        Test-Path -LiteralPath $configuration.token_file -PathType Leaf | Should -BeTrue
        Test-Path -LiteralPath $configuration.probe_config_file -PathType Leaf | Should -BeTrue
        (Split-Path -Parent $configuration.log_file) | Should -Be (Split-Path -Parent $final.Config)
        $configurationText | Should -Not -Match ([regex]::Escape($stagedState))
        Test-Path -LiteralPath $script:activePath | Should -BeTrue
    }

    It "recovers a valid stale attempt and a valid interrupted pending release" {
        $staleId = "attempt-11111111111111111111111111111111"
        $stalePath = Join-Path $script:attempts $staleId
        New-Item -ItemType Directory -Path $stalePath -Force | Out-Null
        @{ schema_version = "powerfactory-mcp-attempt-ownership/v1"; attempt_id = $staleId; path = $stalePath; commit = "unknown" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stalePath "attempt-ownership.json")

        $pendingId = "attempt-22222222222222222222222222222222"
        $pendingCommit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        $pendingName = "release-$($pendingCommit.Substring(0, 12))-$($pendingId.Substring(8, 12))"
        $pendingPath = Join-Path $script:releases $pendingName
        New-Item -ItemType Directory -Path $pendingPath -Force | Out-Null
        @{ schema_version = "powerfactory-mcp-attempt-ownership/v1"; attempt_id = $pendingId; path = (Join-Path $script:attempts $pendingId); commit = $pendingCommit } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $pendingPath "attempt-ownership.json")
        @{ schema_version = "powerfactory-mcp-pending/v1"; attempt_id = $pendingId; commit = $pendingCommit; release_path = $pendingPath } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $pendingPath "install-pending.json")

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        Test-Path -LiteralPath $stalePath | Should -BeFalse
        Test-Path -LiteralPath $pendingPath | Should -BeFalse
        Test-Path -LiteralPath $script:activePath | Should -BeTrue
    }

    It "adopts and removes an empty attempt created by the legacy transaction" {
        $legacyPath = Join-Path $script:attempts "attempt-55555555555555555555555555555555"
        New-Item -ItemType Directory -Path $legacyPath -Force | Out-Null

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        Test-Path -LiteralPath $legacyPath | Should -BeFalse
        Test-Path -LiteralPath $script:activePath | Should -BeTrue
        @(Get-ChildItem -LiteralPath $script:reports -Filter "*.json").Count | Should -Be 0
    }

    It "adopts a legacy staged clone only after its repository identity is verified" {
        $legacyPath = Join-Path $script:attempts "attempt-66666666666666666666666666666666"
        New-Item -ItemType Directory -Path (Join-Path $legacyPath "source\.git"), (Join-Path $legacyPath "state") -Force | Out-Null
        "credential" | Set-Content -LiteralPath (Join-Path $legacyPath "state\mcp-token")
        Mock Get-LegacyAttemptSourceCommit { "dddddddddddddddddddddddddddddddddddddddd" }

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        Test-Path -LiteralPath $legacyPath | Should -BeFalse
        Should -Invoke Get-LegacyAttemptSourceCommit -Times 1 -Exactly
        Test-Path -LiteralPath $script:activePath | Should -BeTrue
    }

    It "adopts the intermediate legacy attempt only after its process and pending ledgers are verified" {
        $legacyId = "attempt-99999999999999999999999999999999"
        $legacyCommit = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        $legacyPath = Join-Path $script:attempts $legacyId
        $statePath = Join-Path $legacyPath "state"
        $configPath = Join-Path $statePath "powerfactory-agent.json"
        New-Item -ItemType Directory -Path (Join-Path $legacyPath "source\.git"), $statePath -Force | Out-Null
        @{ schema_version = "powerfactory-mcp-process-ownership/v1"; pid = 12; process_start_ticks = 123; command_kind = "mcp_server"; scope = "staged_attempt"; config_path = $configPath; endpoint = "http://127.0.0.1:41234/mcp" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $legacyPath "ownership.json")
        @{ schema_version = "powerfactory-mcp-process-ownership/v1"; pid = 13; process_start_ticks = 124; command_kind = "acquisition_probe"; scope = "disposable_probe"; config_path = $configPath } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $legacyPath "acquisition-ownership.json")
        @{ schema_version = "powerfactory-mcp-pending/v1"; attempt_id = $legacyId; commit = $legacyCommit } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $legacyPath "install-pending.json")
        Mock Get-LegacyAttemptSourceCommit { $legacyCommit }

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        Test-Path -LiteralPath $legacyPath | Should -BeFalse
        Should -Invoke Stop-OwnedProcess -Times 2 -Exactly
        Test-Path -LiteralPath $script:activePath | Should -BeTrue
    }

    It "preserves an intermediate legacy attempt whose process ledger is not exactly bound" {
        $legacyPath = Join-Path $script:attempts "attempt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        $statePath = Join-Path $legacyPath "state"
        New-Item -ItemType Directory -Path (Join-Path $legacyPath "source\.git"), $statePath -Force | Out-Null
        @{ schema_version = "powerfactory-mcp-process-ownership/v1"; pid = 12; process_start_ticks = 123; command_kind = "mcp_server"; scope = "active_release"; config_path = (Join-Path $statePath "powerfactory-agent.json"); endpoint = "http://127.0.0.1:41234/mcp" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $legacyPath "ownership.json")
        Mock Get-LegacyAttemptSourceCommit { "ffffffffffffffffffffffffffffffffffffffff" }

        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught.Exception.Data["category"] | Should -Be "OWNERSHIP_UNPROVEN"
        Test-Path -LiteralPath $legacyPath | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $legacyPath "attempt-ownership.json") | Should -BeFalse
    }

    It "preserves a legacy-looking directory when repository ownership is not proven" {
        $legacyPath = Join-Path $script:attempts "attempt-77777777777777777777777777777777"
        New-Item -ItemType Directory -Path (Join-Path $legacyPath "source") -Force | Out-Null
        "unrelated" | Set-Content -LiteralPath (Join-Path $legacyPath "source\notes.txt")
        Mock Get-LegacyAttemptSourceCommit { $null }

        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught.Exception.Data["category"] | Should -Be "OWNERSHIP_UNPROVEN"
        Test-Path -LiteralPath $legacyPath | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $legacyPath "attempt-ownership.json") | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $legacyPath "source\notes.txt") | Should -BeTrue
    }

    It "preserves a legacy-looking directory with unexpected top-level content" {
        $legacyPath = Join-Path $script:attempts "attempt-88888888888888888888888888888888"
        New-Item -ItemType Directory -Path $legacyPath -Force | Out-Null
        "unrelated" | Set-Content -LiteralPath (Join-Path $legacyPath "do-not-delete.txt")

        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught.Exception.Data["category"] | Should -Be "OWNERSHIP_UNPROVEN"
        Test-Path -LiteralPath (Join-Path $legacyPath "do-not-delete.txt") | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $legacyPath "attempt-ownership.json") | Should -BeFalse
    }

    It "refuses an unknown Codex registration before creating an attempt" {
        Mock Get-CodexRegistrationFingerprint { [PSCustomObject]@{ state = "unknown_schema"; fingerprint = $null } }
        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught | Should -Not -BeNullOrEmpty
        $caught.Exception.Data["category"] | Should -Be "CODEX_OWNERSHIP_UNPROVEN"
        @(Get-ChildItem -LiteralPath $script:attempts -Directory).Count | Should -Be 0
        Should -Invoke Register-Codex -Times 0 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
    }

    It "preserves an exact legacy Codex registration without mutating it" {
        Mock Get-CodexRegistrationFingerprint {
            [PSCustomObject]@{
                state = "present"
                fingerprint = [PSCustomObject]@{
                    name = "powerfactory-agent"
                    endpoint = "http://127.0.0.1:8787/mcp"
                    token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN"
                }
            }
        }

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        Test-Path -LiteralPath $script:activePath | Should -BeTrue
        Should -Invoke Register-Codex -Times 0 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
    }

    It "preserves an exact legacy Codex registration when a later stage rolls back" {
        Mock Get-CodexRegistrationFingerprint {
            [PSCustomObject]@{
                state = "present"
                fingerprint = [PSCustomObject]@{
                    name = "powerfactory-agent"
                    endpoint = "http://127.0.0.1:8787/mcp"
                    token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN"
                }
            }
        }
        $env:POWERFACTORY_MCP_FAIL_STAGE = "codex_registration"

        { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } | Should -Throw

        Test-Path -LiteralPath $script:activePath | Should -BeFalse
        Should -Invoke Register-Codex -Times 0 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
    }

    It "refuses a complete but foreign Codex registration before creating an attempt" {
        Mock Get-CodexRegistrationFingerprint {
            [PSCustomObject]@{
                state = "present"
                fingerprint = [PSCustomObject]@{
                    name = "powerfactory-agent"
                    endpoint = "http://127.0.0.1:8788/mcp"
                    token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN"
                }
            }
        }
        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught.Exception.Data["category"] | Should -Be "CODEX_OWNERSHIP_UNPROVEN"
        @(Get-ChildItem -LiteralPath $script:attempts -Directory).Count | Should -Be 0
        Should -Invoke Register-Codex -Times 0 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
    }

    It "creates a Codex registration when no registration exists" {
        Mock Get-CodexRegistrationFingerprint { [PSCustomObject]@{ state = "absent"; fingerprint = $null } }

        Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot

        Test-Path -LiteralPath $script:activePath | Should -BeTrue
        Should -Invoke Register-Codex -Times 1 -Exactly
        Should -Invoke Remove-InstallerCodexRegistration -Times 0 -Exactly
    }

    It "preserves a stale attempt when process ownership cannot be proven" {
        $staleId = "attempt-33333333333333333333333333333333"
        $stalePath = Join-Path $script:attempts $staleId
        New-Item -ItemType Directory -Path $stalePath -Force | Out-Null
        @{ schema_version = "powerfactory-mcp-attempt-ownership/v1"; attempt_id = $staleId; path = $stalePath; commit = "unknown" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stalePath "attempt-ownership.json")
        @{ schema_version = "powerfactory-mcp-process-ownership/v1"; pid = 7; process_start_ticks = 1; command_kind = "mcp_server"; config_path = "config" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stalePath "ownership.json")
        Mock Stop-OwnedProcess { "identity_mismatch" }
        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught.Exception.Data["category"] | Should -Be "OWNERSHIP_UNPROVEN"
        Test-Path -LiteralPath $stalePath | Should -BeTrue
    }

    It "rejects a pending release whose marker is not bound to its exact path" {
        $pendingId = "attempt-44444444444444444444444444444444"
        $pendingCommit = "cccccccccccccccccccccccccccccccccccccccc"
        $pendingName = "release-$($pendingCommit.Substring(0, 12))-$($pendingId.Substring(8, 12))"
        $pendingPath = Join-Path $script:releases $pendingName
        New-Item -ItemType Directory -Path $pendingPath -Force | Out-Null
        @{ schema_version = "powerfactory-mcp-attempt-ownership/v1"; attempt_id = $pendingId; path = "wrong"; commit = $pendingCommit } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $pendingPath "attempt-ownership.json")
        @{ schema_version = "powerfactory-mcp-pending/v1"; attempt_id = $pendingId; commit = $pendingCommit; release_path = (Join-Path $script:releases "wrong") } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $pendingPath "install-pending.json")
        $caught = $null
        try { Invoke-InstallerTransaction -TransactionRoot $script:CaseRoot } catch { $caught = $_ }

        $caught.Exception.Data["category"] | Should -Be "OWNERSHIP_UNPROVEN"
        Test-Path -LiteralPath $pendingPath | Should -BeTrue
    }

    It "never recursively repairs ACLs outside the disposable path being deleted" {
        $source = Get-Content -LiteralPath $script:InstallerPath -Raw
        $acl = $source.Substring($source.IndexOf("function Set-AttemptPrivateAcl"), $source.IndexOf("function Remove-Attempt") - $source.IndexOf("function Set-AttemptPrivateAcl"))
        $acl | Should -Not -Match '"/T"'
        $source | Should -Match "attempt being removed"
        $source | Should -Match "refused_outside_managed_roots"
    }

    It "accepts only a complete matching Codex registration fingerprint" {
        $matching = [PSCustomObject]@{ name = "powerfactory-agent"; transport = [PSCustomObject]@{ type = "streamable_http"; url = "http://127.0.0.1:8787/mcp"; bearer_token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN" } }
        (ConvertTo-CodexRegistrationFingerprint $matching).endpoint | Should -Be "http://127.0.0.1:8787/mcp"
        $matching.transport.url = "https://example.invalid/mcp"
        ConvertTo-CodexRegistrationFingerprint $matching | Should -BeNullOrEmpty
        $matching.transport.url = "http://127.0.0.1:8787/mcp"
        $matching.transport.bearer_token_env_var = "UNTRUSTED_TOKEN"
        ConvertTo-CodexRegistrationFingerprint $matching | Should -BeNullOrEmpty
    }
}

Describe "PowerFactory MCP Codex registration query" {
    BeforeAll {
        $script:InstallerPath = Join-Path $PSScriptRoot "..\..\scripts\install-windows.ps1"
        . $script:InstallerPath -TestHarness

        function New-FakeCodexCommand {
            param([string[]]$OutputLines, [int]$ListExitCode = 0)
            $path = Join-Path $TestDrive "codex-$([guid]::NewGuid().ToString('N')).cmd"
            @(
                "@echo off",
                'if not "%~1"=="mcp" exit /b 91',
                "if `"%~2`"==`"list`" exit /b $ListExitCode",
                'if not "%~2"=="get" exit /b 92',
                'if not "%~3"=="powerfactory-agent" exit /b 93',
                'if not "%~4"=="--json" exit /b 94'
            ) + $OutputLines | Set-Content -LiteralPath $path -Encoding Ascii
            return $path
        }
    }

    It "returns the exact fingerprint from a targeted Codex query" {
        $codex = New-FakeCodexCommand @(
            'echo {"name":"powerfactory-agent","transport":{"type":"streamable_http","url":"http://127.0.0.1:8787/mcp","bearer_token_env_var":"POWERFACTORY_AGENT_MCP_TOKEN"}}',
            "exit /b 0"
        )

        $result = Get-CodexRegistrationFingerprint $codex

        $result.state | Should -Be "present"
        $result.fingerprint.name | Should -Be "powerfactory-agent"
        $result.fingerprint.endpoint | Should -Be "http://127.0.0.1:8787/mcp"
        $result.fingerprint.token_env_var | Should -Be "POWERFACTORY_AGENT_MCP_TOKEN"
    }

    It "returns absent only when the target is missing and the Codex CLI remains healthy" {
        $codex = New-FakeCodexCommand @(
            "exit /b 1"
        )

        $result = Get-CodexRegistrationFingerprint $codex

        $result.state | Should -Be "absent"
    }

    It "fails closed when the targeted Codex query fails for another reason" {
        $codex = New-FakeCodexCommand @(
            "echo Error: Codex configuration is unavailable. 1>&2",
            "exit /b 1"
        ) -ListExitCode 1

        $result = Get-CodexRegistrationFingerprint $codex

        $result.state | Should -Be "query_failed"
    }

    It "fails closed when Codex returns malformed JSON" {
        $codex = New-FakeCodexCommand @(
            "echo not-json",
            "exit /b 0"
        )

        $result = Get-CodexRegistrationFingerprint $codex

        $result.state | Should -Be "unparseable"
    }

    It "fails closed when Codex returns an unsupported registration schema" {
        $codex = New-FakeCodexCommand @(
            'echo {"transport":{"url":"http://127.0.0.1:8787/mcp"}}',
            "exit /b 0"
        )

        $result = Get-CodexRegistrationFingerprint $codex

        $result.state | Should -Be "unknown_schema"
    }
}
