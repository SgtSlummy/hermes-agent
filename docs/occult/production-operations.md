# Occult production build and operations

This is the start-to-finish production contract for the Occult runtime in
Hermes Agent and its Agents Council peer. It describes the finished system,
the immutable build, the release gates, and the operating procedures after
release. It does not authorize deployment by itself.

## Finished product

When enabled, Hermes is the Occult executive interface. A scoped virtual token
selects a Major Arcana agent, a deck constrains its tools and memory, and
Mythos selects a compatible Minor Arcana route. Agents Council executes
resumable spreads across the same versioned contract. Provider credentials stay
inside Hermes and never cross the Council transport.

```text
Hermes clients / OpenAI-compatible clients
                    |
              Occult HTTP API
                    |
     Major Arcana + decks + virtual tokens
                    |
               Mythos Router
                    |
       local/free provider adapters
                    |
              normalized result

Agents Council <--- redacted v1 transport ---> Occult HTTP API
```

The normal Hermes path remains unchanged while `occult.enabled` is false.
Production defaults are local-first, free-only, no paid fallback, and a
zero-dollar request ceiling.

## Clean local installation

Install Ollama and pull at least one chat model, then run:

```text
ollama pull qwen2.5:3b
hermes occult init --model qwen2.5:3b
hermes gateway restart
hermes occult status
```

`hermes occult init` is idempotent. It validates a loopback-only provider,
installs the bundled signed Major Arcana packages, registers a local zero-cost
Minor Arcana route, creates `occult.deck.starter`, and issues scoped API
credentials without printing them. The active profile owns all Occult state.

The starter agents are The Magician, Justice, Temperance, Judgement, and The
World. Their source prompts and package builder remain in the repository; only
the public signing key and deterministic signed packages ship in the build.
Regenerating official packages requires a separately controlled signing key.

## Source and branch assembly

1. Review the dedicated Hermes and Agents Council pull-request stacks.
2. Merge each stack in dependency order into `main`.
3. Select immutable commit SHAs for Hermes and Council.
4. Run the `Occult production gate` with an explicit version, channel, and
   reviewed Council ref.
5. Never substitute a branch head after the run begins.

The build consumes `uv.lock`, the three npm lockfiles, `flake.lock`, the
Dockerfile, and the reviewed source commit. GitHub actions are pinned to full
commit SHAs. Provider credentials are deliberately blank during validation.

## Build and compilation sequence

The production workflow performs these steps:

1. On Linux, macOS, and Windows, install the frozen Python environment, run the
   full Occult test suite, and build the wheel and source distribution.
2. Install the reviewed Agents Council ref with frozen Bun dependencies, run
   its typecheck/tests, run the Council restart/approval spread, then run the
   live Hermes-to-Council HTTP gate.
3. Run `nix flake check`, build the Nix package, and archive its immutable
   output.
4. Compile the Hermes web dashboard, terminal UI, and documentation site from
   frozen npm lockfiles.
5. Build an OCI image from the same source.
6. Collect compiled outputs in one staging directory.
7. Run `scripts/occult_release.py assemble` once. The assembler copies compiled
   bytes, rejects secret-shaped files/content, emits compatibility and migration
   metadata, creates a CycloneDX SBOM and SLSA/in-toto provenance, and writes
   SHA-256 checksums.
8. Run verification before uploading the staged artifact.

Local preview:

```powershell
$commit = git rev-parse HEAD
$epoch = git show -s --format=%ct $commit
python scripts/occult_release.py assemble `
  --artifacts compiled `
  --output staged `
  --version 1.0.0-preview `
  --commit $commit `
  --channel preview `
  --source-date-epoch $epoch
python scripts/occult_release.py verify staged
```

The output contains:

```text
staged/
├── artifacts/
│   ├── platform packages
│   ├── dashboard
│   ├── TUI
│   ├── documentation
│   ├── Nix package
│   └── OCI image
├── occult-compatibility.json
├── occult-migrations.json
├── occult-release-manifest.json
├── occult-sbom.cdx.json
├── occult-provenance.intoto.jsonl
└── SHA256SUMS.txt
```

## Release gates

A release is blocked unless all of these pass:

- Linux, macOS, and Windows Occult tests and package builds.
- Dashboard, TUI, and documentation typechecks/tests/builds.
- No critical production npm advisory in the dashboard, TUI, or documentation
  dependency trees; high findings require a recorded maintainer review.
- Nix flake check and package build.
- OCI image build.
- Provider outage, timeout, rate-limit, cancellation, fallback, circuit-breaker,
  and bounded-capacity tests.
- Restart-safe Council reading, approval pause/resume, idempotency, terminal
  event, and live cross-repository HTTP tests.
- Credential and response redaction tests.
- Deterministic assembly, tamper detection, sensitive-artifact rejection,
  compatibility, migration, SBOM, provenance, and exact promotion tests.
- A clean preview verification with no external API keys.

The `occult-production` GitHub environment must require an operator approval.
Only a manual `stable` run can enter that environment.

## Stable signing and exact promotion

After approval, the workflow downloads the already-staged artifact. It does not
compile again. Sigstore signs `SHA256SUMS.txt` using the GitHub Actions OIDC
identity and immediately verifies the certificate identity and issuer. The
release verifier checks the bundle and every listed SHA-256 digest. Promotion
copies the staged tree and rejects any byte difference.

The Python verifier validates bundle structure and release inventory; the
workflow's Sigstore step is the cryptographic trust decision. Operators must
not bypass that step for the stable channel.

## Clean local-only installation

Before the first stable release, validate on a machine or VM with no configured
provider keys:

1. Install the promoted wheel, Nix package, or OCI artifact.
2. Bind the Occult API to loopback only.
3. Keep `occult.enabled=false` and confirm normal Hermes behavior.
4. Enable Occult and register only an approved local provider.
5. Issue one least-privilege virtual token.
6. Invoke one Major Arcana agent in `local_only` and `free_only` mode.
7. Disconnect outbound network access and repeat the invocation.
8. Confirm audit/status output contains no prompt, token, credential reference,
   API key, or raw provider error.
9. Restart Hermes and resume a stored Council reading.

Do not expose the raw local API publicly. Remote access requires an authenticated
private tunnel or VPN and a separately scoped virtual token.

## Backup and restore

Stop new invocations and drain active readings before a consistent backup. Copy:

- `occult/mythos-state.json`
- `occult/virtual_tokens.db`, including `-wal` and `-shm` when present
- `occult/readings.db`, including `-wal` and `-shm` when present
- `occult/decks.json`
- installed `.tarot` packages and their signature metadata
- encrypted credential storage, with its master key backed up separately
- release manifest, checksums, SBOM, provenance, and Sigstore bundle

Restore into a clean directory, apply only documented migrations, start with
`occult.enabled=false`, validate database integrity and package signatures, then
enable local-only routing. Run a canary invocation and resume a non-destructive
test reading before admitting normal traffic.

## Upgrade

1. Verify the candidate's Sigstore identity, checksum manifest, compatibility
   metadata, and migration plan.
2. Back up the current state and retain the prior promoted artifact.
3. Drain new readings.
4. Deploy the candidate with Occult disabled.
5. Run health, local-only, redaction, and restart/resume canaries.
6. Enable Occult for one scoped canary token.
7. Expand traffic gradually while watching route failures, fallback rate,
   latency, capacity rejections, quota state, and terminal reading events.
8. Keep paid fallback disabled unless a separate reviewed policy explicitly
   changes the zero-dollar ceiling.

## Rollback

Stop admissions, drain or cancel active readings, disable Occult, and capture
the failed state for diagnosis. Restore the previous promoted bytes. If the
migration metadata says the state schema changed, restore the matching backup;
never open newer state with an older binary by assumption. Verify checksums,
restart locally, run canaries, and reopen traffic gradually.

## Incident and credential response

For provider outage or overload, Mythos opens the relevant circuit, applies
cooldown/fallback rules, and returns a retryable redacted error when bounded
capacity is exhausted. Operators should lower admission concurrency or disable
the affected route rather than add unreviewed keys.

For suspected credential exposure:

1. Disable the route and revoke the virtual client token.
2. Revoke or rotate the provider credential through its official mechanism.
3. Search audit metadata by opaque credential/route ID; do not copy raw secrets
   into tickets or chat.
4. Revalidate the provider and quota pool.
5. Run redaction and canary tests before reactivation.
6. Record the incident and affected release hashes.

## Updates after production

- **Nightly:** discovery candidates, provider health, regression tests, route
  performance, and vulnerability scans. Discovery never activates a provider.
- **Weekly:** restore drill, dependency review, failed-route analysis, and
  Council workflow quality review.
- **Monthly:** preview release, compatibility review, benchmark reranking, new
  adapter/agent proposals, and documentation reconciliation.
- **Stable:** only reviewed commits, an approved production environment,
  cryptographic signing, exact staged-byte promotion, release notes, and a
  tested rollback artifact.

The release workflow creates artifacts and gates. Publishing packages,
advancing public tags, or deploying production remains an explicit operator
action after review.
