# Run on Windows with Pester 5. The installer is dot-sourced in TestHarness
# mode, so every injected stage executes its production rollback function while
# git, uv, Codex, PowerFactory, and process effects remain mocked.
$installer = Join-Path $PSScriptRoot "..\\..\\scripts\\install-windows.ps1"
$stages = @(
    "preflight", "source", "environment", "secure_configuration",
    "temporary_mcp_health", "codex_registration", "cutover_prior_service_drain",
    "acquisition_probe", "promotion"
)

Describe "PowerFactory MCP transactional installer failure injection" {
    BeforeAll {
        . $installer -TestHarness
    }

    BeforeEach {
        $script:Stage = "preflight"
        $script:Attempt = [PSCustomObject]@{ id = "attempt-test"; path = (Join-Path $TestDrive "attempt-test"); commit = "test-sha" }
        $script:Prior = [PSCustomObject]@{ release_path = (Join-Path $TestDrive "prior-release"); port = 8787 }
        $script:PriorWasStopped = $true
        $script:CodexChanged = $true
        $script:LauncherChanged = $false
        $script:LauncherPath = $null
        $script:Uv = "uv"
        $script:Codex = "codex"
        $script:Icacls = "icacls.exe"
        $script:Runtime = [PSCustomObject]@{ PythonVersion = "3.14" }
        $script:StagedServer = [PSCustomObject]@{ Id = 41001; HasExited = $false }
        $script:FinalServer = [PSCustomObject]@{ Id = 41002; HasExited = $false }
        New-Item -ItemType Directory -Force -Path $script:Attempt.path, $script:Prior.release_path | Out-Null
        $activePath = Join-Path $TestDrive "active.json"
        '{"commit":"known-good","release_path":"prior-release"}' | Set-Content -LiteralPath $activePath -Encoding UTF8
        $script:ExpectedActiveManifest = Get-Content -LiteralPath $activePath -Raw

        Mock Stop-CreatedProcess { "stopped" }
        Mock Register-Codex {}
        Mock Remove-InstallerCodexRegistration {}
        Mock Restart-PriorRelease { "restarted:41003" }
        Mock Remove-Attempt { "removed" }
    }

    It "rolls back exactly after injected <stage> failure" -ForEach $stages {
        param($stage)
        $env:POWERFACTORY_MCP_FAIL_STAGE = $stage
        $reports = Join-Path $TestDrive "failure-reports"
        $activePath = Join-Path $TestDrive "active.json"

        try {
            Invoke-Stage $stage { throw "the injected hook must fail before this action" }
            throw "expected injected failure"
        } catch {
            $rollback = Invoke-InstallerRollback $_ $reports $script:Runtime
        } finally {
            Remove-Item Env:POWERFACTORY_MCP_FAIL_STAGE -ErrorAction SilentlyContinue
        }

        (Get-Content -LiteralPath $activePath -Raw) | Should -Be $script:ExpectedActiveManifest
        $rollback.attempt | Should -Be "removed"
        $rollback.codex_registration | Should -Be "restored"
        $rollback.prior_release | Should -Be "restarted:41003"
        $rollback.powerfactory | Should -Be "no_persistent_engine_created; acquisition workers are disposable"
        Should -Invoke -CommandName Stop-CreatedProcess -Times 2 -Exactly
        Should -Invoke -CommandName Register-Codex -Times 1 -Exactly
        Should -Invoke -CommandName Remove-Attempt -Times 1 -Exactly

        $reportsFound = @(Get-ChildItem -LiteralPath $reports -Filter "*.json")
        $reportsFound.Count | Should -Be 1
        $report = Get-Content -LiteralPath $reportsFound[0].FullName -Raw | ConvertFrom-Json
        $report.stage | Should -Be $stage
        $report.category | Should -Be "INJECTED_FAILURE"
        $report.rollback.attempt | Should -Be "removed"
        (Get-Content -LiteralPath $reportsFound[0].FullName -Raw) | Should -Not -Match "mcp-token|Bearer|licen[cs]e|password"
    }

    It "removes only the registration created by a failed fresh install" {
        $script:Prior = $null
        $script:PriorWasStopped = $false
        $reports = Join-Path $TestDrive "failure-reports-fresh"
        $env:POWERFACTORY_MCP_FAIL_STAGE = "promotion"

        try {
            Invoke-Stage "promotion" { throw "the injected hook must fail before this action" }
            throw "expected injected failure"
        } catch {
            $rollback = Invoke-InstallerRollback $_ $reports $script:Runtime
        } finally {
            Remove-Item Env:POWERFACTORY_MCP_FAIL_STAGE -ErrorAction SilentlyContinue
        }

        $rollback.codex_registration | Should -Be "removed_new_registration"
        Should -Invoke -CommandName Remove-InstallerCodexRegistration -Times 1 -Exactly
        Should -Invoke -CommandName Register-Codex -Times 0 -Exactly
    }

    It "does not recursively repair ACLs outside a disposable attempt" {
        $source = Get-Content -LiteralPath $installer -Raw
        $acl = $source.Substring($source.IndexOf("function Set-AttemptPrivateAcl"), $source.IndexOf("function Remove-Attempt") - $source.IndexOf("function Set-AttemptPrivateAcl"))
        $acl | Should -Not -Match '"/T"'
        $source | Should -Match "attempt being removed"
    }

    It "accepts only a complete matching Codex registration fingerprint" {
        $matching = [PSCustomObject]@{
            name = "powerfactory-agent"
            transport = [PSCustomObject]@{
                type = "streamable_http"
                url = "http://127.0.0.1:8787/mcp"
                bearer_token_env_var = "POWERFACTORY_AGENT_MCP_TOKEN"
            }
        }

        (ConvertTo-CodexRegistrationFingerprint $matching).endpoint | Should -Be "http://127.0.0.1:8787/mcp"
        $matching.transport.url = "https://example.invalid/mcp"
        ConvertTo-CodexRegistrationFingerprint $matching | Should -BeNullOrEmpty
        $matching.transport.url = "http://127.0.0.1:8787/mcp"
        $matching.transport.bearer_token_env_var = "UNTRUSTED_TOKEN"
        ConvertTo-CodexRegistrationFingerprint $matching | Should -BeNullOrEmpty
    }
}
