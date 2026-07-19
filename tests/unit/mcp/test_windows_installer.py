from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[3]
INSTALLER = PROJECT_ROOT / "scripts" / "install-windows.ps1"
BOOTSTRAP = PROJECT_ROOT / "scripts" / "bootstrap-windows.ps1"


def _source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_one_command_bootstrap_downloads_a_complete_script_file() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "git" in source
    assert "clone --depth 1 --branch $Ref" in source
    assert "rev-parse HEAD" in source
    assert "-Ref $commit" in source
    assert 'Join-Path $bootstrapSource "scripts\\install-windows.ps1"' in source
    assert "| iex" not in source
    assert "StateDir" not in source

    for documentation in (PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs" / "friend-test.md"):
        text = documentation.read_text(encoding="utf-8")
        assert "Set-ExecutionPolicy -Scope Process Bypass -Force" in text
        assert "-OutFile $bootstrap; & $bootstrap" in text


def test_installer_stages_guid_attempts_and_promotes_an_atomic_active_manifest() -> None:
    source = _source()
    assert 'Join-Path $root "attempts"' in source
    assert 'Join-Path $root "releases"' in source
    assert 'Join-Path $root "failure-reports"' in source
    assert '"attempt-$([guid]::NewGuid().ToString(\'N\'))"' in source
    assert "function Write-AtomicJson" in source
    assert "[System.IO.File]::Replace($temporary, $Path, $backup)" in source
    assert "[System.IO.File]::Replace($temporary, $Path, $null)" not in source
    assert 'Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue' in source
    assert "active.json" in source
    assert "StateDir" not in source
    assert "Read-Host" not in source
    assert '"--project"' not in source
    assert '"--study-case"' not in source


def test_transaction_has_explicit_failure_injection_and_all_required_stages() -> None:
    source = _source()
    stages = (
        "preflight",
        "source",
        "environment",
        "secure_configuration",
        "temporary_mcp_health",
        "codex_registration",
        "cutover_prior_service_drain",
        "acquisition_probe",
        "promotion",
    )
    assert "POWERFACTORY_MCP_FAIL_STAGE" in source
    assert "function Invoke-InstallerTransaction" in source
    assert "param([string]$TransactionRoot = $InstallRoot)" in source
    assert "$root = [IO.Path]::GetFullPath($TransactionRoot)" in source
    assert "Failure injection requested after" in source
    positions = [source.index(f'Invoke-Stage "{stage}"') for stage in stages]
    assert positions == sorted(positions)
    for checkpoint in (
        "directory_move",
        "final_mcp_health",
        "final_acquisition_probe",
        "launcher_update",
        "before_active_manifest",
    ):
        assert f'Invoke-PromotionCheckpoint "{checkpoint}"' in source
    assert source.index('Invoke-PromotionCheckpoint "before_active_manifest"') < source.index(
        "Write-AtomicJson $activePath"
    )
    assert source.index("Write-AtomicJson $activePath") < source.index(
        'Remove-Item -LiteralPath (Join-Path $releasePath "install-pending.json")'
    )
    assert "function Write-FailureReport" in source
    assert "function Restart-PriorRelease" in source
    assert "function Remove-Attempt" in source
    assert "schema_version = \"powerfactory-mcp-install-failure/v1\"" in source


def test_attempt_cleanup_and_acl_work_never_target_existing_managed_releases() -> None:
    source = _source()
    acl = source[source.index("function Set-AttemptPrivateAcl") : source.index("function Remove-Attempt")]
    assert "Never recurse into an existing release or managed root" in acl
    assert '"/T"' not in acl
    cleanup = source[source.index("function Remove-Attempt") : source.index("function Resolve-PowerFactoryRuntime")]
    assert "attempt being removed" in cleanup
    assert "Remove-Item -LiteralPath $fullPath -Recurse -Force" in cleanup
    assert "refused_outside_managed_roots" in cleanup
    assert "refused_reparse_point" in cleanup
    assert "$script:Attempt.path" in source


def test_health_checks_are_authenticated_and_do_not_start_powerfactory_from_serve() -> None:
    source = _source()
    assert 'method="initialize"' in source
    assert 'Authorization="Bearer $Token"' in source
    assert "function Start-McpServer" in source
    assert '"probe-acquisition --config' in source
    assert '"serve", "--config"' not in source  # arguments remain a single Start-Process string
    assert '"run powerfactory-agent serve' not in source
    assert "powerfactory-agent.exe" in source
    assert source.index('Invoke-Stage "temporary_mcp_health"') < source.index('Invoke-Stage "cutover_prior_service_drain"')
    assert source.index('Invoke-Stage "cutover_prior_service_drain"') < source.index('Invoke-Stage "acquisition_probe"')


def test_promotion_rebases_absolute_mcp_state_paths_before_final_health_check() -> None:
    source = _source()
    assert "function Rebase-McpInstallationPaths" in source
    assert "function Get-RebasedReleaseStatePath" in source
    assert "function Test-PathWithinRoot" in source
    assert "new-object -typename system.text.utf8encoding -argumentlist $false" in source.lower()
    move = source.index("Move-Item -LiteralPath $script:Attempt.path -Destination $releasePath")
    rebase = source.index("Rebase-McpInstallationPaths (Join-Path $script:Attempt.state \"powerfactory-agent.json\")")
    final_server = source.index("$script:FinalServer = Start-McpServer")
    assert move < rebase < final_server
    assert 'foreach ($field in @(\"token_file\", \"log_file\", \"probe_config_file\"))' in source
    assert "Promoted MCP installation $field is missing." in source
    assert "log_file must be rooted in the release state directory" in source


def test_codex_registration_requires_all_owned_evidence_before_mutation() -> None:
    source = _source()
    assert "function Test-OwnedCodexRegistration" in source
    for proof in ("codex_name", "endpoint", "token_env_var", "Test-McpInitialize", "token_identity"):
        assert proof in source
    assert "CODEX_OWNERSHIP_UNPROVEN" in source
    assert "codex mcp remove powerfactory-agent" in source
    assert "--bearer-token-env-var" in source
    assert "SetEnvironmentVariable" not in source
    assert "Get-CodexRegistrationFingerprint" in source
    assert "mcp list --json" in source
    assert "unknown_schema" in source
    assert "streamable_http" in source


def test_installer_uses_one_hash_helper_for_powershell_parse_safety() -> None:
    source = _source()
    assert "function Get-Sha256Hex" in source
    assert "$mutexDigest = (Get-Sha256Hex $root).Substring(0, 24)" in source
    assert '"Local\\PowerFactoryMCP-$mutexDigest"' in source
    assert '"Local\\\\PowerFactoryMCP-$mutexDigest"' not in source
    assert "token_identity = Get-Sha256Hex $Token" in source


def test_interrupted_promotion_is_recoverable_and_registration_rollback_is_armed() -> None:
    source = _source()
    assert '"install-pending.json"' in source
    assert 'release_path=$releasePath' in source
    assert "function Test-AttemptOwnership" in source
    assert "function Test-PendingReleaseOwnership" in source
    assert "attempt.attempt_id -ne $marker.attempt_id" in source
    assert "attempt.commit -ne $marker.commit" in source
    assert 'command_kind = "mcp_server"' in source
    assert "$script:CodexChanged = $true" in source
    assert "Register-Codex $script:Codex $endpoint $false" in source
    assert "Stop-CreatedProcess $script:StagedServer" in source


def test_windows_ci_preserves_detailed_transaction_results() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(
        encoding="utf-8"
    )
    assert 'shell: powershell' in workflow
    assert 'Parse every PowerShell release artifact' in workflow
    assert 'Install-Module Pester -RequiredVersion 5.7.1' in workflow
    assert 'Import-Module Pester -RequiredVersion 5.7.1' in workflow
    assert '$config.Output.Verbosity = "Detailed"' in workflow
    assert '$config.TestResult.OutputFormat = "NUnitXml"' in workflow
    assert 'actions/upload-artifact@v4' in workflow
