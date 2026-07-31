# Occult local public v1 quickstart

This is the authoritative installation and first-run guide for the local
Occult System release. It installs the signed Hermes `v1.0.1` GitHub release
and the signed Agents Council `v0.5.2` GitHub release without administrator
rights. Occult remains disabled until you explicitly initialize a local
Ollama model.

The runtime contract remains `1.0.0`, the Council state schema remains `3`,
paid routing remains disabled, and no cloud-provider credential is required.

## Requirements

- Windows 10/11 x64, Linux x64/arm64, or macOS x64/arm64.
- Internet access for the initial GitHub downloads.
- Ollama only when you choose to initialize local inference.
- About 4 GB of free space for the starter model and application files.

The installer bootstraps an ephemeral, version-pinned `uv` verifier from its
official GitHub release and checks the verifier archive against a built-in
SHA-256 value. It then verifies the Hermes and Council checksum manifests
against their exact GitHub Actions Sigstore identities, verifies every selected
asset with SHA-256, and only then writes application files.

## Install on Windows

Open PowerShell as your normal user. Do not run it as Administrator.

```powershell
$installer = Join-Path $env:TEMP "install-occult.ps1"; $expected = "817969abc9a2d8302702ec168999377497954e892b78646aac1b9ed00f04a3b2"; Invoke-WebRequest "https://github.com/SgtSlummy/hermes-agent/releases/download/v1.0.1/install-occult.ps1" -OutFile $installer; if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { Remove-Item -LiteralPath $installer -Force; throw "Occult installer checksum verification failed" }; & $installer
```

This literal checksum is pinned in the immutable `v1.0.1` quickstart and
authenticates the installer before PowerShell executes it. The installer then
verifies the signed release manifest, dependency lock, wheel, and Council asset.

The default install root is `%LOCALAPPDATA%\Occult`. Choose another per-user
directory with:

```powershell
& $installer -InstallRoot "D:\Apps\Occult"
```

Verify all release assets without installing:

```powershell
& $installer -VerifyOnly
```

Install Hermes without Council:

```powershell
& $installer -SkipCouncil
```

## Install on Linux or macOS

Run as your normal user:

```bash
(
  set -eu
  installer="${TMPDIR:-/tmp}/install-occult.sh"
  expected="7d7b3c586a95707cc2ccaf9b9a4de6a46ec09656e596e6b6a9fa87491ee175c3"
  curl -fsSLo "$installer" "https://github.com/SgtSlummy/hermes-agent/releases/download/v1.0.1/install-occult.sh"
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$installer" | awk '{print $1}')
  else
    actual=$(shasum -a 256 "$installer" | awk '{print $1}')
  fi
  [ "$actual" = "$expected" ] || {
    rm -f -- "$installer"
    echo "Occult installer checksum verification failed" >&2
    exit 1
  }
  sh "$installer"
)
```

This performs the same pre-execution authentication. The POSIX installer then
verifies the signed release manifest, dependency lock, wheel, and Council asset.

The default install root is `${XDG_DATA_HOME:-$HOME/.local/share}/occult`.
The installer creates per-user command links in
`${XDG_BIN_HOME:-$HOME/.local/bin}`.

Available options:

```text
--version 1.0.1
--install-root /path/owned/by/you
--initialize-local
--skip-council
--verify-only
--model qwen2.5:3b
```

## Confirm the installed versions and disabled default

Windows:

```powershell
hermes --version
council --version
Get-Content "$env:LOCALAPPDATA\Occult\occult-install-receipt.json"
```

Linux or macOS:

```bash
export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
hermes --version
council --version
cat "${XDG_DATA_HOME:-$HOME/.local/share}/occult/occult-install-receipt.json"
```

These commands assume the normal installation. If you used `-SkipCouncil` or
`--skip-council`, omit `council --version`. The receipt identifies Occult
release `1.0.1`, Hermes CLI package `0.14.0`, contract `1.0.0`, Council state
schema `3`, and `occult_initialized: false`. A normal installation also records
Council `v0.5.2` and its archive hash; a skipped Council installation records
`null` for both fields. The Hermes package and the Occult release have separate
version lines by design.

On a fresh profile, installation does not create or enable an Occult
configuration. `hermes occult status` must therefore report that initialization
is required until the next section is completed.

## Initialize local Ollama explicitly

Install Ollama from its official distribution, then either rerun the installer
with explicit initialization:

```powershell
& $installer -InitializeLocal -Model "qwen2.5:3b"
```

```bash
sh "$installer" --initialize-local --model qwen2.5:3b
```

Or perform the same two steps directly:

```text
ollama pull qwen2.5:3b
hermes occult init --model qwen2.5:3b
```

Initialization is the only installer path that enables Occult. It validates a
loopback Ollama endpoint, installs the signed starter Major Arcana packages,
creates a zero-cost local route and starter deck, and creates scoped local
virtual tokens without printing them.

Restart the local gateway and check the route:

```text
hermes gateway restart
hermes occult status
```

The Occult API remains bound to `http://127.0.0.1:8642`. Do not expose this port
directly to the public internet.

## First zero-cost Major Arcana invocation

```text
hermes occult invoke --agent occult.major.magician --message "Return exactly: OCCULT LOCAL CANARY" --mode local_only
```

The response route must be the local Ollama Minor Arcana card, with a maximum
cost of zero. No cloud API key is needed.

## First Agents Council reading

Council uses a scoped Occult service token, not a provider credential. Issue
one local token, capture the one-time value in your local secret manager or
process environment, and never paste it into an issue, report, or chat:

```text
hermes occult token-issue --token-id council-local --allow-agent occult.major.magician --allow-agent occult.major.justice --allow-agent occult.major.temperance --allow-route minor.pentacles.ace.ollama.local --requests-per-minute 10 --maximum-budget 0
```

Start Council with these local environment values:

```text
OCCULT_ENABLED=true
OCCULT_HERMES_URL=http://127.0.0.1:8642
OCCULT_HERMES_SERVICE_TOKEN=<the one-time scoped virtual token>
```

Then start the MCP interface:

```text
council mcp --format json --agent-name local-operator
```

From the connected MCP client:

1. Call `start_council` with the local test request.
2. Call `occult_create_reading_v1` with a three-node build, approval-gated
   review, and synthesis spread using only the starter agents and
   `local_only`/`free_only` routing.
3. Confirm the reading pauses before the approval-gated review node.
4. Approve the node in Council Hall.
5. Close and reopen Council to simulate a restart.
6. Call `occult_resume_reading_v1` with the same reading ID and complete spread.
7. Confirm each node has exactly one successful attempt and there is one
   terminal `reading.completed` event.

The repository's protected release gate runs this same pause, restart, resume,
idempotency, and redaction scenario against the pinned Council release.

## Disable Occult

Disable new Occult invocations without deleting state:

```text
hermes config set occult.enabled false
hermes gateway restart
```

To disable the loopback API surface as well:

```text
hermes config set platforms.api_server.enabled false
hermes gateway restart
```

Re-enabling requires an explicit configuration change or another successful
`hermes occult init`.

## Backup and restore

Create a consistent backup before upgrades or rollback:

```text
hermes backup --output occult-local-backup.zip
```

The backup includes profile configuration, virtual-token state, readings,
invocation metadata, decks, and installed agent packages. Keep any external
secret-manager backup and its encryption key separately.

Restore into the intended profile only after stopping the gateway:

```text
hermes gateway stop
hermes import occult-local-backup.zip
hermes config set occult.enabled false
hermes gateway start
```

Validate `hermes occult status`, then explicitly re-enable local routing.

## Roll back to the previous checksummed releases

The previous immutable releases are:

- Hermes Occult `v1.0.0`
- Agents Council `v0.5.1`

Download them from their GitHub release pages, verify their published checksum
files before extraction, disable Occult, and retain the `v1.0.1` receipt and
backup. The Hermes `v1.0.0` universal archive contains its platform wheel; use
that wheel to replace the `uv` tool environment under the same install root.
Replace Council with the matching `v0.5.1` platform archive.

Runtime contract `1.0.0` and Council state schema `3` are unchanged in these
patch releases. State restoration is therefore not normally required, but the
pre-rollback backup remains mandatory. Full checksum and Sigstore commands are
in [production operations](production-operations.md).

After rollback:

```text
hermes --version
council --version
hermes occult status
```

Run one local invocation and one restart/resume reading before normal use.

## Safe failure behavior

The installer stops before application installation when it encounters:

- an unsupported operating system or architecture;
- an unavailable or interrupted download;
- a checksum mismatch;
- a Sigstore identity or issuer mismatch;
- a release manifest/version mismatch;
- a missing Ollama executable when explicit initialization was requested.

Rerunning the same verified version is idempotent. A failed verification never
enables Occult and never uploads telemetry, prompts, tokens, credentials, or
signed download URLs.
