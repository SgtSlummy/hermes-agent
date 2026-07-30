# Occult System contract v1

Status: implementation foundation, disabled by default.

## Purpose

This contract is the boundary between Hermes, Agents Council, and later
Occult components. It establishes data ownership and validation rules without
activating model routing, provider enrollment, credentials, tools, memory, or
Council workflows.

The runtime models live in `agent/occult/contracts.py`. The language-neutral
schema is checked in at `agent/occult/spec/v1/contract.schema.json`, and
portable fixtures live in `agent/occult/spec/v1/fixtures/`.

The inert Hermes-owned routing implementation is documented in
[`mythos-routing.md`](mythos-routing.md).

Regenerate or verify the schema with:

```text
python scripts/occult_contract_schema.py
python scripts/occult_contract_schema.py --check
```

Consumers can also call `contract_json_schema()` or
`load_contract_schema()` from Python.

## Feature gate

Hermes configuration contains:

```yaml
occult:
  enabled: false
  contract_version: "1.0.0"
```

No existing Hermes path reads this flag yet. Setting it does not activate a
provider, mutate a prompt, register a tool, change startup, or create state.
A later integration must check the flag at a single entry boundary and must
remain additive.

## Ownership boundary

| Capability | Owner | Rollback boundary |
|---|---|---|
| Provider credentials and authorization headers | Hermes credential broker | Disable the broker adapter; secrets never cross the contract |
| Provider/model discovery, health, quota, and route selection | Mythos inside Hermes | Disable Mythos and return to the existing Hermes provider path |
| Major Arcana package validation and prompt composition | Hermes Occult runtime | Disable the Occult entry boundary; existing prompts remain unchanged |
| Reading graph, node state, Council collaboration, and resume | Agents Council | Disable Council reading tools; ordinary Council sessions remain |
| Memory storage and tool authorization | Existing Hermes owners, exposed through scoped adapters | Remove the adapter without changing provider or Council state |
| Public API, CLI, TUI, and audit presentation | Hermes surfaces | Hide Occult-only surfaces while preserving existing clients |

Agents Council must not receive provider keys, access tokens, refresh tokens,
authorization headers, passwords, or credential objects. Route summaries may
contain provider and model identifiers, but never their authentication data.

## Compatibility rules

- `contract_version` is required on every top-level payload.
- Version `1.0.0` requires an exact match. Compatibility negotiation is a
  future contract change, not an implicit fallback.
- Unknown required capabilities fail during contract validation, before route
  selection or provider execution.
- Unknown fields are rejected so spelling mistakes and undeclared authority
  cannot silently pass through.
- Secret-shaped fields are rejected recursively before model parsing.
- Validation errors contain field locations only; they do not echo payload
  values.
- Breaking field, enum, or semantic changes require a new major contract
  version and parallel support during migration.

## Idempotency and events

- Each invocation supplies an `idempotency_key`.
- Repeating the same key and semantic request must return the existing reading
  or invocation result; reuse with different content must fail.
- Invocation result bodies are retained for seven days by default. Their
  token-scoped key fingerprints remain protected for four times the configured
  result-retention window. After that explicit identity horizon, a key is
  expired and may execute again; clients requiring longer deduplication must
  issue a fresh key or configure a longer horizon.
- Event sequences are contiguous and strictly increasing within one reading.
- An event stream belongs to exactly one reading.
- A completed stream ends in exactly one of `reading.completed`,
  `reading.failed`, or `reading.cancelled`.
- No event may follow a terminal event.
- Error events use `OccultError`, whose `redacted` field is always `true`.

Persistence and distributed idempotency are owned by later runtime work. The
v1 validator establishes the rules those stores must enforce.

## Authorization and data classes

The contract carries identifiers, instructions, policy, status, and redacted
results. It does not carry secrets.

Risk levels remain:

- 0: read-only
- 1: reversible local change
- 2: persistent or external change requiring the applicable Hermes policy
- 3: high-impact change requiring explicit authorization

A Major Arcana package or Minor Arcana pairing may narrow permissions. Neither
may expand system, user, deck, or tool authority.

## Migration and rollback

1. Add new fields as optional within a minor contract release.
2. Introduce breaking changes under a new major contract version.
3. Keep the previous validator and fixtures available during the migration
   window.
4. Reject mismatches before a reading node or provider call starts.
5. Roll back the feature boundary before rolling back shared storage.
6. Never make credential material part of a migration payload or audit event.

The readings v2 storage migration hashes legacy idempotency keys in place.
Downgrading to a binary that expects readings v1 therefore requires restoring
the matching pre-migration `readings.db` backup; disabling the feature alone
does not reverse that storage change.
