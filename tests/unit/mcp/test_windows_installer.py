from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[3]
INSTALLER = PROJECT_ROOT / "scripts" / "install-windows.ps1"
BOOTSTRAP = PROJECT_ROOT / "scripts" / "bootstrap-windows.ps1"


def _source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_one_command_bootstrap_downloads_a_complete_script_file() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Invoke-WebRequest" in source
    assert "-OutFile $scriptPath" in source
    assert "& $scriptPath" in source
    assert "| iex" not in source
    assert "StateDir" not in source

    for documentation in (PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs" / "friend-test.md"):
        assert "-OutFile $bootstrap; & $bootstrap" in documentation.read_text(encoding="utf-8")


def test_installer_stages_guid_attempts_and_promotes_an_atomic_active_manifest() -> None:
    source = _source()
    assert 'Join-Path $root "attempts"' in source
    assert 'Join-Path $root "releases"' in source
    assert 'Join-Path $root "failure-reports"' in source
    assert '"attempt-$([guid]::NewGuid().ToString(\'N\'))"' in source
    assert "function Write-AtomicJson" in source
    assert "Move-Item -LiteralPath $temporary -Destination $Path -Force" in source
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
    positions = [source.index(f'Invoke-Stage "{stage}"') for stage in stages]
    assert positions == sorted(positions)
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
    assert "Remove-Item -LiteralPath $Path -Recurse -Force" in cleanup
    assert "$script:Attempt.path" in source


def test_health_checks_are_authenticated_and_do_not_start_powerfactory_from_serve() -> None:
    source = _source()
    assert 'method="initialize"' in source
    assert 'Authorization="Bearer $Token"' in source
    assert "function Start-McpServer" in source
    assert '"probe-acquisition"' in source
    assert '"serve", "--config"' not in source  # arguments remain a single Start-Process string
    assert source.index('Invoke-Stage "temporary_mcp_health"') < source.index('Invoke-Stage "cutover_prior_service_drain"')
    assert source.index('Invoke-Stage "cutover_prior_service_drain"') < source.index('Invoke-Stage "acquisition_probe"')


def test_codex_registration_requires_all_owned_evidence_before_mutation() -> None:
    source = _source()
    assert "function Test-OwnedCodexRegistration" in source
    for proof in ("codex_name", "endpoint", "token_env_var", "Test-McpInitialize", "token_identity"):
        assert proof in source
    assert "CODEX_OWNERSHIP_UNPROVEN" in source
    assert "codex mcp remove powerfactory-agent" in source
    assert "--bearer-token-env-var" in source
    assert "SetEnvironmentVariable" not in source
    helper = (PROJECT_ROOT / "scripts" / "windows-installer-registration.ps1").read_text(encoding="utf-8")
    assert "Get-CodexRegistrationFingerprint" in helper
    assert "unknown_schema" in helper
    assert "streamable_http" in helper


def test_installer_uses_intermediate_hash_values_for_powershell_parse_safety() -> None:
    source = _source()
    assert "$mutexHashBytes = " in source
    assert "$mutexDigest = [System.BitConverter]::ToString($mutexHashBytes)" in source
    assert "$tokenHashBytes = " in source
    assert "token_identity=$tokenIdentity" in source
