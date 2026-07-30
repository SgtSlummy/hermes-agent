# Occult System Production Plan for Hermes Agent

Status: proposal
Target branch: `main`
Scope of this change: planning only; no runtime behavior, provider access, credentials, or deployment is enabled

## 1. Outcome

The Occult System will make Hermes the local-first execution authority for a
portable agent-and-model runtime:

- **Major Arcana** define stable agent identity, behavior, permissions, memory
  policy, tool policy, and upright/reversed operating stance.
- **Minor Arcana** identify normalized provider/model routes and their current
  capabilities, health, quota, privacy, latency, and cost characteristics.
- **Mythos** selects an eligible Minor Arcana route for each invocation and
  performs bounded fallback.
- **Agents Council** coordinates multi-agent readings and spread workflows
  without owning provider credentials.
- **Hermes** composes the effective prompt, owns memory and tool enforcement,
  invokes the selected route, validates the result, and records the audit trail.

When finished, a user can invoke the same Major Arcana through the Hermes CLI,
TUI, dashboard, gateway, or API, then switch between local and authorized cloud
models without changing the agent's identity or exposing provider credentials.
Existing Hermes behavior remains the default until the user explicitly enables
Occult.

Example finished request:

```json
{
  "agent": "occult.major.magician",
  "orientation": "upright",
  "minor_arcana": "auto",
  "deck": "occult.deck.development",
  "input": "Design and validate a Discord relay service.",
  "routing": {
    "mode": "local_first",
    "free_only": true,
    "maximum_fallbacks": 2
  }
}
```

The result includes the answer, artifacts, tool activity, selected route,
fallback history, usage, and an invocation ID. It never includes a provider
secret.

## 2. Product boundaries

### In scope

- A versioned Occult contract shared with Agents Council.
- Uploadable, validated Major Arcana packages.
- A normalized Minor Arcana model registry built on Hermes provider plugins.
- Local-first and free-first routing, health checks, quota awareness, circuit
  breakers, and bounded fallback.
- Provider-independent agent memory and tool permissions.
- OpenAI-compatible access plus an Occult-native invocation API.
- CLI/TUI/dashboard controls that complement the existing Hermes chat surface.
- A feature-gated Agents Council bridge for readings and spreads.
- Reproducible builds, release assembly, staging, production rollout, updates,
  backup, restore, and rollback.

### Out of scope

- Automated third-party account creation.
- CAPTCHA, email-verification, phone-verification, or identity bypass.
- Disposable accounts, promotional-credit farming, scraped/leaked/shared keys,
  reverse-engineered consumer sessions, or quota evasion.
- Silent paid-provider use.
- Provider secrets in prompts, Council state, logs, packages, or artifacts.
- Public network exposure by default.
- Replacing Hermes' current CLI/TUI/dashboard chat implementation.
- Activating unreviewed provider packages solely because a public catalog lists
  them.

### Default safety posture

```yaml
occult:
  enabled: false
  routing:
    mode: local_first
    free_only: true
    paid_fallback:
      enabled: false
  council_bridge:
    enabled: false
  provider_discovery:
    auto_activate: false
```

## 3. Architectural fit with Hermes

Occult should extend the existing Hermes plugin and provider surfaces instead of
creating a second agent loop.

```text
CLI / TUI / Dashboard / Gateway / OpenAI-compatible client
                         |
                         v
                 Hermes AIAgent runtime
                         |
            +------------+-------------+
            |                          |
            v                          v
  Major Arcana composition       Memory and tool policy
            |                          |
            +------------+-------------+
                         |
                         v
                    Mythos router
                         |
             +-----------+-----------+
             |           |           |
             v           v           v
          Local       Free/owned   Approved paid
         providers     providers      fallback
                         |
                         v
                 Normalized response
                         |
                         v
              Audit / usage / artifacts
```

Required Hermes invariants:

- Use the existing model-provider plugin discovery system; do not hardcode each
  provider into core.
- Expand generic plugin hooks when a capability is missing; do not add
  Occult-specific branches throughout `run_agent.py`, `cli.py`, or gateway core.
- Use `get_hermes_home()` for all persistent Occult paths and
  `display_hermes_home()` for user-facing paths.
- Preserve profile isolation. Each Hermes profile has its own agent registry,
  route policy, quotas, readings, and credential references unless explicitly
  shared through an approved backend.
- Preserve prompt caching. Mid-session agent, tool, memory, or model-policy
  changes are deferred by default; an explicit restart/new-session action is
  required for immediate recomposition.
- Keep provider credentials in Hermes secret stores or a credential-broker
  boundary. `.env` remains secrets-only; non-secret Occult configuration belongs
  in `config.yaml`.
- Keep the existing TUI as the primary chat experience. React surfaces may add
  inspectors, route status, pairing controls, and reading views, but not a second
  transcript/composer.

## 4. Component ownership

| Capability | Owner | Contract boundary |
|---|---|---|
| Agent loop, prompts, memory, tools | Hermes | Existing `AIAgent` and plugin hooks |
| Major Arcana packages and runtime | Hermes | Versioned package and invocation schemas |
| Provider/model adapters | Hermes | Existing model-provider plugin interface |
| Minor Arcana index and route scoring | Mythos inside the Hermes boundary | Normalized route candidate/result schemas |
| Provider credentials | Hermes credential broker | Opaque credential references only |
| Council sessions and spread orchestration | Agents Council | Versioned reading events and invocation requests |
| Human chat surface | Existing Hermes TUI/dashboard | Additive inspectors and controls |
| Cross-repository compatibility | Both repositories | Pinned Occult contract version |

Agents Council may store:

- Major Arcana ID and version.
- Orientation.
- Deck and spread IDs.
- Requested route constraints.
- Selected Minor Arcana ID.
- Invocation, reading, artifact, and audit references.
- Status, timestamps, usage summaries, and redacted errors.

Agents Council must not store:

- API keys, refresh tokens, cookies, authorization headers, or secret material.
- Raw credential-pool identifiers that reveal provider account structure.
- Full sensitive prompts or memory unless explicitly allowed by the Hermes
  invocation policy.

## 5. Contract baseline

Phase 0 must publish JSON Schemas and fixtures for:

- `OccultInvocationRequest`
- `OccultInvocationResult`
- `MajorArcanaManifest`
- `MinorArcanaDescriptor`
- `DeckManifest`
- `SpreadManifest`
- `ReadingEvent`
- `RouteDecisionSummary`
- `OccultError`

Every cross-process object includes:

- `contract_version`
- stable ID
- producer component and version
- creation timestamp
- correlation/invocation ID
- sensitivity classification where data can cross a trust boundary

Compatibility rules:

1. Unknown optional fields are ignored.
2. Unknown required capabilities cause a controlled rejection.
3. Secrets are represented only by opaque internal references.
4. Writes that can be retried require an idempotency key.
5. Streaming events are ordered per invocation and include a terminal event.
6. Breaking schema changes require a new contract version and a coordinated
   Hermes/Council release.

Proposed native endpoints, subject to the Phase 0 ADR:

```text
POST /v1/occult/invoke
GET  /v1/occult/invocations/{id}
GET  /v1/occult/major-arcana
GET  /v1/occult/minor-arcana
GET  /v1/occult/decks
GET  /v1/occult/router/health
```

The existing OpenAI-compatible endpoints remain available. Occult metadata is
selected through a virtual model/agent mapping or explicit extension fields
without breaking ordinary clients.

## 6. Start-to-finish execution plan

### Phase 0 — Baseline, governance, and compatibility contract

Deliverables:

- Record the architecture decision and ownership table.
- Inventory current provider, routing, memory, tool, gateway, profile, plugin,
  TUI, build, and release surfaces.
- Define and version the shared Hermes/Council contract.
- Define threat model, data classification, authorization levels, and audit
  requirements.
- Define feature flags and the migration/rollback policy.
- Create contract fixtures that both repositories can validate independently.

Exit gate:

- Both repositories validate the same fixtures.
- The design identifies no provider secret path into Council or model prompts.
- Current Hermes startup, configuration, and provider selection are unchanged
  while `occult.enabled` is false.

### Phase 1 — Offline vertical slice

Deliverables:

- Add an Occult runtime plugin boundary using existing generic hooks.
- Implement one built-in Major Arcana fixture and upright/reversed prompt
  composition.
- Implement a mock Minor Arcana route backed by a deterministic test provider.
- Return a normalized invocation result and route explanation.
- Store test data beneath a temporary profile-scoped Hermes home.

Validation:

- No network or real credential is required.
- Agent identity stays stable across two mock model profiles.
- Orientation changes behavior but cannot weaken system/tool policy.
- Disabling Occult returns Hermes to the unchanged legacy path.

Exit gate:

- One CLI or API request completes end to end with deterministic tests and a
  complete redacted audit record.

### Phase 2 — Minor Arcana registry and Mythos routing

Deliverables:

- Normalize current provider-plugin model metadata into Minor Arcana
  descriptors.
- Classify routes by capabilities, locality, privacy, context, structured
  output, tool support, latency, health, quota, and cost.
- Implement candidate filtering before scoring.
- Implement local-only, local-first, free-only, free-first, quality-first,
  speed-first, privacy-first, and manual policies.
- Add health state, cooldowns, circuit breakers, quota-pool awareness, and
  bounded fallback.
- Record why candidates were selected or rejected without exposing secrets.

Validation:

- Local-only requests cannot call an external endpoint.
- Free-only requests cannot call a priced route.
- Multiple credentials sharing one account quota are treated as one quota pool.
- Authentication failures are not retried as transient failures.
- Rate-limit and outage fixtures select an eligible fallback and later recover
  through a half-open probe.

Exit gate:

- At least one local provider, one generic OpenAI-compatible test endpoint, and
  one disabled external fixture pass the same adapter contract suite.

### Phase 3 — Credential broker and provider enrollment

Deliverables:

- Define an isolated credential-broker API returning opaque references.
- Support keyless local routes and import of user-authorized existing secrets.
- Support refresh/rotation only through official provider mechanisms.
- Add validation, expiry, revocation, redaction, and account/quota association.
- Add provider trust states from discovered through active, suspended, and
  retired.
- Add an authorization inbox for providers requiring unavoidable user consent.

Validation:

- Full secrets never appear in logs, exceptions, audit events, prompts,
  Council state, or API responses.
- Invalid/revoked credentials are removed from routing.
- Provider discovery cannot activate an unreviewed route.
- Prohibited account/key acquisition behavior is covered by policy tests.

Exit gate:

- Local/keyless enrollment is zero-touch, and authorized secret import completes
  without exposing the secret outside the broker boundary.

### Phase 4 — Major Arcana package and pairing runtime

Deliverables:

- Define `.tarot` as a ZIP-compatible, versioned agent package.
- Validate archive size, required files, schemas, paths, signatures, tool
  references, memory policy, and model requirements in quarantine.
- Add versioned install, activate, deactivate, and rollback operations.
- Compose system policy, user policy, deck policy, Major Arcana identity,
  orientation, bounded Minor Arcana modifiers, memory, tools, task, and output
  schema in a deterministic order.
- Add compatibility checks before a Major/Minor pairing can run.

Validation:

- Path traversal, archive bombs, malformed YAML/JSON, unknown tools, invalid
  signatures, and excessive permissions are rejected.
- Minor Arcana modifiers remain inside the Major Arcana's allowed temperament
  ranges.
- Package policy cannot override global security, spending, or tool approval.
- Switching routes preserves agent identity and permitted memory.

Exit gate:

- A signed test agent can be installed, paired with two test routes, invoked,
  and rolled back without restarting Hermes.

### Phase 5 — Memory, tools, and execution policy

Deliverables:

- Define global, profile, project, deck, agent, reading, and invocation memory
  namespaces.
- Apply sensitivity and route-privacy filters before prompt injection.
- Reuse Hermes toolsets and approval enforcement; packages reference tools but
  do not bypass the tool registry.
- Add per-agent limits for risk, concurrency, context, toolsets, external data,
  and paid routes.
- Add audit correlation across model calls, tool calls, artifacts, and memory
  updates.

Validation:

- External routes receive only memory allowed by their privacy class.
- Tool authorization is checked outside the model on every call.
- Prompt/package content cannot elevate tool permissions.
- Tests never write to a real user Hermes home.
- Cache-sensitive changes take effect only at documented session boundaries.

Exit gate:

- A multi-turn test survives a route change, uses one approved tool, rejects one
  prohibited tool, and records a complete audit chain.

### Phase 6 — User and API surfaces

Deliverables:

- Add feature-gated CLI commands for status, agents, routes, pairings, decks,
  package validation, and test invocation.
- Add TUI/dashboard inspectors for active Major/Minor pairing, route reason,
  health, quota class, reading progress, and redacted errors.
- Add Occult-native API endpoints and OpenAI-compatible mapping.
- Add scoped Hermes virtual tokens with agent, route, tool, memory, rate, and
  budget limits.
- Add plain-language recovery guidance for unavailable routes or invalid
  packages.

Validation:

- The primary transcript/composer remains the existing Hermes TUI.
- All new controls perform real actions or are omitted.
- API authentication and authorization cover every new endpoint.
- Streaming and cancellation emit terminal events and release reservations.
- Keyboard, reduced-motion, and narrow-screen checks cover new dashboard views.

Exit gate:

- A user can install a test agent, compare two pairings, invoke it, inspect the
  route, and disable Occult without editing a source file.

### Phase 7 — Agents Council bridge and spread execution

Deliverables:

- Implement the shared versioned bridge client.
- Accept Council reading requests with an idempotency key.
- Map each spread node to a Major Arcana invocation and return redacted reading
  events.
- Support sequential nodes, bounded parallel nodes, retry, evaluator,
  synthesizer, cancellation, and explicit approval gates.
- Persist resumable reading state while Hermes remains the execution authority.

Validation:

- Council never receives a credential or direct provider client.
- A reading resumes after a Council or Hermes restart without duplicating a
  completed node.
- Cancellation prevents new nodes and attempts to cancel active inference.
- An evaluator can be required to use a different model/provider family from
  the author.
- Contract mismatch fails before any provider call.

Exit gate:

- A three-node build/review/synthesis reading completes across both repositories
  using only mock/local routes.

### Phase 8 — Reliability, observability, and security hardening

Deliverables:

- Add metrics for route selection, latency, failures, fallbacks, queue depth,
  tool failures, token usage, quota class, and cost.
- Add structured redacted logs and trace correlation.
- Add queue/concurrency limits, timeouts, dead-letter inspection, and recovery.
- Add package, dependency, container, and secret scans.
- Add backup/restore for registries, packages, policy, readings, audit records,
  and encrypted credential references.
- Add load, failure-injection, and long-running soak tests.

Exit gate:

- Provider outage does not take down Hermes.
- Restore and rollback are demonstrated from a clean temporary environment.
- No critical security finding remains open.
- Unexpected paid-provider selection is both blocked and alertable.

### Phase 9 — Build, assembly, staging, and production release

Deliverables:

- Build all Python, TUI, dashboard/docs, container, and Nix artifacts from
  pinned inputs.
- Assemble a versioned release manifest and software bill of materials.
- Generate checksums and signatures for distributable artifacts.
- Deploy the exact candidate artifacts to staging.
- Pass migration, smoke, security, load, backup, restore, and rollback gates.
- Promote the same immutable artifacts to the stable channel.

Exit gate:

- The release installs on a clean supported machine.
- Local-only operation works without internet.
- Existing Hermes operation works with Occult disabled.
- The production feature remains opt-in until the staged acceptance run is
  approved.

## 7. Build and compilation

The implementation PRs must verify the current canonical commands before
changing CI. The expected build sequence is:

1. Create or reuse the repository-supported Python environment.
2. Install from locked/pinned dependencies.
3. Run Hermes' required test wrapper:

   ```bash
   scripts/run_tests.sh
   ```

4. Run configured lint/type checks.
5. Build Python wheel and source distribution with the repository's packaging
   metadata.
6. Build and test the Ink TUI from `ui-tui/`.
7. Build the dashboard/docs assets using their existing package lockfiles.
8. Validate Docker and Nix outputs through the existing workflows.
9. Run contract tests against the pinned Agents Council contract fixture.
10. Generate artifact hashes, SBOM, provenance, and a build manifest.

No build step may download an unpinned executable or execute code from a
provider catalog. New dependencies follow Hermes' upper-bound and commit-SHA
pinning policy.

## 8. Release assembly

The assembled release should contain:

```text
occult-hermes-release/
├── packages/              # Hermes Python artifacts
├── ui/                    # TUI/dashboard assets
├── agents/                # signed starter Major Arcana packages
├── contracts/             # shared JSON Schemas and fixtures
├── config/                # non-secret examples and migration metadata
├── migrations/            # reversible registry/state migrations
├── containers/            # image references and digests
├── docs/                  # install, operations, security, recovery
├── licenses/
├── sbom/
├── manifest.json
└── SHA256SUMS
```

`manifest.json` records:

- Hermes version and commit.
- Occult contract version.
- Agents Council compatibility range.
- Package, schema, migration, and image versions.
- Build platform and timestamp.
- Artifact hashes and signatures.
- Minimum supported configuration version.

Credentials, live tokens, private memory, and user state are never included.

## 9. Environment progression

### Development

- Temporary profile-scoped Hermes home.
- Mock/local routes only by default.
- Deterministic contract fixtures.
- No real credentials in test processes.

### Integration

- Hermes and Agents Council built from pinned commits.
- Mock provider plus local inference.
- Cross-repository contract and restart/resume tests.

### Staging

- Separate state, secrets, virtual tokens, and provider projects.
- Production build flags and immutable artifacts.
- Limited quotas and paid fallback disabled.
- Full logging, metrics, backups, and rollback rehearsal.

### Production

- Bind locally by default.
- Remote access only through an authenticated private network or gateway.
- Administrative endpoints require elevated authorization.
- Feature rollout is explicit, reversible, and profile-scoped.

## 10. Test and release gates

| Gate | Required evidence |
|---|---|
| Unit | Routing, policy, package, prompt, memory, and permission invariants |
| Contract | Shared schemas and fixtures pass in Hermes and Council |
| Integration | Local/mock provider, registry, fallback, audit, restart |
| End to end | Client → Hermes → Council reading → Mythos → provider → result |
| Security | Secret redaction, archive traversal, authz, injection, tool escalation |
| Reliability | Timeout, 429, outage, circuit recovery, cancellation, queue pressure |
| Cross-platform | Supported Windows, macOS, and Linux install/smoke matrix |
| Recovery | Backup, clean restore, migration rollback, previous-version rollback |
| Release | SBOM, signatures, checksums, provenance, immutable promotion |

Documentation-only PRs run document/link/schema checks. Runtime implementation
PRs run the relevant focused tests plus the full Hermes suite before merge.

## 11. Production rollout and rollback

Rollout stages:

1. Merge contracts and inert feature flags.
2. Ship offline mock/local vertical slice to preview.
3. Enable local-only Occult for selected profiles.
4. Enable authorized free external routes in staging.
5. Enable the Council bridge with local/mock routes.
6. Run production canary with explicit operator approval.
7. Expand stable availability while paid fallback remains disabled.

Rollback order:

1. Stop accepting new readings.
2. Drain or cancel active invocations.
3. Disable `occult.enabled` or the narrower failing feature flag.
4. Preserve a redacted diagnostic bundle.
5. Restore the previous application artifact.
6. Roll back state only when the migration declares it safe; otherwise restore
   the pre-upgrade backup.
7. Run legacy Hermes and local-only smoke tests before reopening.

## 12. Updates after production

### Continuous

- Health, quota, latency, error, queue, and cost telemetry.
- Credential expiry checks and official refresh where authorized.
- Circuit-breaker recovery probes.

### Daily

- Discover provider/model metadata into a quarantined candidate catalog.
- Revalidate active route health with minimal canaries.
- Never auto-activate a new external provider or credential source.

### Weekly

- Run task-specific route benchmarks.
- Review security advisories and dependency drift.
- Test backup integrity and a sampled restore.
- Review provider terms/free-tier metadata before changing routing eligibility.

### Monthly

- Publish a signed stable or preview release from immutable artifacts.
- Review Major/Minor pairings, quality data, fallback behavior, and user
  corrections.
- Deprecate routes through a documented grace period.
- Rehearse full rollback and credential revocation.

### Emergency

- Revoke or quarantine affected credentials/providers.
- Disable compromised packages or routes through signed deny-list data.
- Publish a security advisory and patched signed artifact.
- Preserve audit evidence without preserving secret values.

## 13. Implementation workstreams

The implementation should be delivered as focused, reviewable issues/PRs:

1. **Hermes Occult contract and feature-gated architecture**
   - Owns Phase 0 and the inert contract/config baseline.
2. **Mythos route registry, policy, health, quota, and credential boundary**
   - Owns Phases 1–3.
3. **Major Arcana packages, pairing, memory, and tool policy**
   - Owns Phases 4–5.
4. **Occult API, CLI/TUI/dashboard controls, and Agents Council bridge**
   - Owns Phases 6–7.
5. **Production hardening, build, assembly, release, update, and rollback**
   - Owns Phases 8–9 and operational readiness.

Each issue includes its own tests and documentation; tests and docs are not
deferred into cleanup-only follow-ups.

## 14. Definition of production ready

Occult for Hermes is production ready only when:

1. The shared contract is versioned and validated by both repositories.
2. Existing Hermes behavior is unchanged with Occult disabled.
3. Local-only mode is proven not to make external inference calls.
4. Free-only mode is proven not to select priced routes.
5. Paid fallback is disabled by default and independently budget-gated.
6. Major Arcana packages install, validate, activate, and roll back safely.
7. Minor Arcana routes are capability-, privacy-, health-, and quota-aware.
8. Provider credentials remain inside the credential boundary.
9. Agent identity and permitted memory survive route changes.
10. Tool permissions are enforced outside model output.
11. Readings resume idempotently after restart.
12. Route decisions and failures are explainable without secrets.
13. Python, TUI, dashboard/docs, container, and Nix builds are reproducible.
14. Supported-platform smoke tests pass from clean installations.
15. Backup, restore, migration, application rollback, and credential revocation
    are demonstrated.
16. Release artifacts have checksums, signatures, SBOM, and provenance.
17. Staging passes security, reliability, load, and end-to-end gates.
18. Production rollout remains reversible and profile-scoped.

The foundational rule is:

> Major Arcana owns identity. Minor Arcana supplies intelligence. Mythos chooses
> the route. Hermes enforces execution. Agents Council coordinates the reading.
