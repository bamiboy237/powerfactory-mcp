from __future__ import annotations

from pathlib import Path
import re


INSTALLER = Path(__file__).parents[3] / "scripts" / "install-windows.ps1"


def _source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_has_explicit_prerequisite_and_runtime_checks() -> None:
    source = _source()

    assert 'Get-RequiredCommand "uv"' in source
    assert 'Get-RequiredCommand "codex"' in source
    assert 'Get-RequiredCommand "icacls.exe"' in source
    assert 'Get-Command "Get-NetTCPConnection"' in source
    assert 'Get-ChildItem -LiteralPath $pythonRoot -Filter "powerfactory.pyd"' in source
    assert '"sync", "--locked", "--python", $runtime.PythonVersion' in source
    assert 'platform.architecture()[0]' in source
    assert '"@active"' in source


def test_installer_runs_real_probe_before_server_and_codex_registration() -> None:
    source = _source()

    configure = source.index('Invoke-CheckedCommand "PowerFactory probe configuration"')
    probe = source.index('Invoke-CheckedCommand "real PowerFactory connectivity probe"')
    server = source.index('$listener = Get-NetTCPConnection')
    register = source.index('Invoke-CheckedCommand "Codex MCP registration"')

    assert configure < probe < server < register
    assert '"probe", "--config", $configPath, "--repeat", "2"' in source
    assert "fake" not in source.lower()


def test_installer_verifies_existing_or_new_server_with_authenticated_initialize() -> None:
    source = _source()

    assert "function Test-McpInitialize" in source
    assert 'method = "initialize"' in source
    assert 'Authorization = "Bearer $Token"' in source
    assert "function Wait-McpReady" in source
    assert 'owner.CommandLine -notmatch "powerfactory-agent\\s+serve"' in source
    assert 'did not accept this installation credential' in source


def test_installer_keeps_token_out_of_codex_config_and_persistent_environment() -> None:
    source = _source()

    assert '--bearer-token-env-var", "POWERFACTORY_AGENT_MCP_TOKEN"' in source
    assert "SetEnvironmentVariable" not in source
    assert "Start-PowerFactoryCodex.ps1" in source
    assert "Get-Content -LiteralPath `$tokenPath -Raw" in source
    assert re.search(r'icacls.*?/inheritance:r', source, flags=re.DOTALL | re.IGNORECASE)
    assert 'Unexpected accounts retain access' in source


def test_installer_is_idempotent_without_rotating_existing_credentials() -> None:
    source = _source()

    assert 'if (-not (Test-Path -LiteralPath $configPath))' in source
    assert 'if (-not (Test-Path -LiteralPath $tokenPath))' in source
    assert "Existing installation uses port" in source
    assert "Reusing the healthy PowerFactory MCP server" in source
    assert "Remove-Item" not in source
