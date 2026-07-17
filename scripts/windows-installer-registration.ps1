Set-StrictMode -Version 2.0

function ConvertTo-CodexRegistrationFingerprint {
    param([object]$Value)

    # `codex mcp get --json` has used both a flat representation and a nested
    # transport object. Anything else is deliberately unowned.
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

    $output = & $Codex mcp get powerfactory-agent --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) { return [PSCustomObject]@{ state = "absent"; fingerprint = $null } }
    try { $parsed = $output | ConvertFrom-Json } catch { return [PSCustomObject]@{ state = "unparseable"; fingerprint = $null } }
    $fingerprint = ConvertTo-CodexRegistrationFingerprint $parsed
    if ($null -eq $fingerprint) { return [PSCustomObject]@{ state = "unknown_schema"; fingerprint = $null } }
    return [PSCustomObject]@{ state = "present"; fingerprint = $fingerprint }
}
