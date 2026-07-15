# Buildout 00 Windows Handoff

This handoff prepares, but does not accept, the PowerFactory 2026 external-engine
lifecycle. The adapter and its API assumptions are **UNVALIDATED** until the
commands below pass against a supported Windows installation and a safe,
non-confidential fixture at the exact tested commit.

## Required environment record

Before running the probe, record the following outside the probe configuration:

- exact Git commit SHA;
- PowerFactory 2026 release and service pack;
- licence capability, without licence keys or server details;
- compatible CPython major/minor and process architecture;
- absolute `powerfactory.pyd` path;
- non-confidential fixture identifier;
- whether the session is attached or demonstrably product-owned;
- any GUI interaction, stale process, crash, hang, licence failure, or command
  deviation.

Do not commit credentials, licence data, customer paths or models, database
details, or unsanitized PowerFactory output.

## Probe configuration

Create a local JSON file outside version control. `POWERFACTORY_PROBE_CONFIG`
must point to it. The file is strict and secret-free:

```json
{
  "pyd_path": "C:\\Program Files\\DIgSILENT\\PowerFactory 2026\\Python\\3.13\\powerfactory.pyd",
  "python_version": "3.13",
  "project_selector": "Approved Non-Confidential Project",
  "study_case": "Approved Read-Only Study Case",
  "sample_limit": 10,
  "cardinality_ceiling": 10000,
  "include_out_of_service": false,
  "session_ownership": "attached",
  "ini_path": "C:\\Path\\To\\Approved\\powerfactory.ini",
  "user_profile_env_var": "PF_PROBE_USER",
  "password_env_var": "PF_PROBE_PASSWORD"
}
```

Use the CPython version actually shipped for the selected `powerfactory.pyd`;
the example is not a compatibility claim. Project and study-case selectors are
exact matches against `loc_name` or `GetFullName()` observations. Duplicate
matches fail closed. Keep `session_ownership` as `attached` unless the process
was explicitly created for and is owned by this probe. Attached mode never
posts an exit command.

`ini_path` is optional, must name an existing `.ini` file, and is passed as the
third `GetApplicationExt` argument in `/ini "<path>"` form. The optional user
profile and password values are read only from the named environment variables
at `CONNECT_APPLICATION`. The adapter always calls `GetApplicationExt` with
exactly three arguments, using `None` for omitted profile, password, or INI.
Do not put credentials in the JSON or command line. Omit both `*_env_var` fields
when default authentication is sufficient.

## Exact Windows commands

Run from PowerShell in a clean checkout. Replace placeholders only; do not
modify the probe or test commands.

```powershell
git checkout <exact-commit-sha>
git rev-parse HEAD
$env:POWERFACTORY_PROBE_CONFIG = (Resolve-Path <local-config-json>).Path
$env:PF_PROBE_USER = <profile-from-approved-secret-source>
$env:PF_PROBE_PASSWORD = <password-from-approved-secret-source>

uv run python -m unittest discover -s tests/contract -p "test_powerfactory2026_probe_adapter.py" -v
uv run python -m unittest discover -s tests/integration -p "test_powerfactory_*.py" -v
uv run python -m compileall -q src scripts tests
uv run python scripts/probes/powerfactory_2026_lifecycle.py --repeat 3 --output artifacts/buildout-00/lifecycle.json
$probeExitCode = $LASTEXITCODE
Get-FileHash artifacts/buildout-00/lifecycle.json -Algorithm SHA256
$probeExitCode
```

If default authentication is used, do not set `PF_PROBE_USER` or
`PF_PROBE_PASSWORD`. Record every command's exit code. A probe exit code of `1`
is a failed lifecycle stage, not acceptance evidence. Do not rerun with manual
GUI steps or altered fixture/configuration without recording the deviation and
producing a separate artifact.

## Evidence return and sanitization

Return the exact commit SHA, environment record, command output and exit codes,
the sanitized lifecycle JSON, SHA-256, and engineering observations. Before
returning evidence:

1. inspect all paths, project/study-case names, errors, logs, and native values;
2. replace confidential fixture or customer identifiers with stable sanitized
   labels and record that sanitization occurred;
3. remove credentials, user identities when sensitive, licence keys/server
   details, database/INI paths, and confidential model/results data;
4. retain stage names, return codes, counts, callable booleans, units, version,
   architecture, service-pack label, and sanitized locator structure needed to
   evaluate the gate;
5. do not commit the local JSON configuration or raw vendor logs.

## Acceptance status

Expected current status is **BLOCKED - Windows validation required**. Keep
implementation checklist item 5 unchecked and Buildout 0 blocked until all
matrix commands pass at the recorded commit, three lifecycle runs succeed
without GUI interaction, cleanup does not poison later runs, and returned
evidence is reviewed and accepted. macOS fake-vendor tests are preparation, not
PowerFactory compatibility proof.

## Unresolved empirical risks

The Windows run must determine, rather than assume:

- whether the selected `powerfactory.pyd` loads through the standard-library
  extension loader for the recorded ABI and architecture;
- the real `GetApplicationExt` authentication signature and whether it attaches
  or creates a process in the selected environment;
- `GetProjectFolder`, recursive `GetContents`, `ActivateProject`,
  `GetActiveProject`, `GetActiveStudyCase`, and study-case activation semantics;
- `GetCalcRelevantObjects` out-of-service argument semantics and large-fixture
  cardinality behavior;
- `ComLdf.Execute()` return-code meaning and convergence evidence;
- availability, numeric meaning, and units of terminal `m:u` and line
  `c:loading`;
- which observed callables are usable, not merely present;
- uniqueness and stability limits of read-only names and full-name candidate
  locators; no identity stability claim is made by this probe;
- whether `PostCommand('exit')` is correct and sufficient for an explicitly
  product-owned process, and whether cleanup is repeatable after failure.
