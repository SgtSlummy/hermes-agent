# Mythos routing foundation

Status: offline production foundation, disabled by default.

## Purpose

Mythos is the Hermes-owned routing boundary for the Occult System. It selects
an eligible Minor Arcana route after checking contract compatibility,
capabilities, privacy, cost, quota, health, provider trust, and opaque
credential availability.

The implementation lives in:

- `agent/occult/mythos.py`
- `agent/occult/credential_broker.py`

Importing either module has no side effects. The existing Hermes provider path
does not call Mythos, and `occult.enabled` remains `false` by default.

## Runtime boundary

```text
Validated Occult invocation
        |
        v
Mythos policy and candidate filtering
        |
        +-- provider trust
        +-- required capabilities
        +-- local/free/cost policy
        +-- quota-pool availability
        +-- circuit-breaker state
        +-- opaque credential availability
        |
        v
Selected provider adapter
        |
        v
Normalized response and redacted RouteSummary
```

Hermes remains the sole owner of provider execution. Agents Council receives
only the versioned `RouteSummary`; it never receives credential references,
secret values, authorization headers, upstream response bodies, or raw
provider exceptions.

## Route lifecycle

Provider and model discovery is not authorization. Every discovered route is
forced into `discovered`, even if an importer supplies a different state.

```text
discovered --approve--> active
     |
     +--reject-------> quarantined

active --auth failure--> suspended
active --retire-------> retired
```

Only `active` routes are eligible. There is no method that automatically turns
a discovered route into an active route.

`descriptor_from_provider()` normalizes an existing Hermes
`ProviderProfile`. It intentionally copies provider and model identifiers,
not headers, API keys, environment values, or authorization state.

## Routing policies

The v1 contract supports:

- `local_only`
- `local_first`
- `free_only`
- `quality_first`
- `speed_first`
- `privacy_first`
- `manual`

Hard filters run before scoring:

1. active trust state;
2. explicit manual card, when requested;
3. local-only and free-only requirements;
4. maximum request cost;
5. required capabilities;
6. route circuit health;
7. shared quota-pool availability;
8. credential-reference availability.

Scoring is deterministic and uses quality, latency, privacy, free status, and
locality. Card ID is the stable tie-breaker. One route is attempted at most
once per invocation, and total attempts are bounded by
`maximum_fallbacks + 1`.

## Provider adapters

`ProviderAdapter` accepts:

- an `AdapterRequest`;
- a `MinorArcanaDescriptor`;
- a `SecretValue` or `None` for a keyless local route.

The initial offline adapters are:

- `MockProviderAdapter`;
- `LocalProviderAdapter`;
- `OpenAICompatibleAdapter`.

They share one callable contract, which lets tests exercise identical request
and response normalization without network access. A future live adapter must
normalize upstream failures to `ProviderFailure` and must not expose response
bodies. Unexpected exceptions are converted to the safe `unknown` failure
kind at the router boundary.

## Credential boundary

`InMemoryCredentialBroker` demonstrates the required ownership model:

- route descriptors contain only an opaque reference;
- `SecretValue.__str__` and `__repr__` are always redacted;
- a secret is revealed only to the final adapter call;
- expiry and revocation are enforced before selection;
- metadata export contains no secret;
- secrets are never written to Mythos state.

Production wiring must resolve opaque references through existing Hermes
authentication stores. It must not create a second plaintext credential
database.

Allowed acquisition lifecycle actions are:

- keyless discovery;
- import of a user-authorized secret;
- OAuth refresh after authorization;
- service-account use;
- provider-supported official rotation.

The policy rejects automated account creation, CAPTCHA or verification bypass,
leaked or shared keys, and quota evasion.

## Quota pools

Quota belongs to the provider account, organization, or project that enforces
it—not necessarily to an individual credential.

```text
provider
  `-- quota pool
        |-- credential reference A
        `-- credential reference B
```

Routes with the same `quota_pool_id` share remaining-request and cooldown
state. A rate limit on one credential cools the entire pool immediately, so a
second key cannot be used to evade an account-level limit.

## Health and fallback

Failures are normalized as:

- `authentication`: suspend the route; never transiently retry it;
- `rate_limit`: cool the shared quota pool for the provider reset interval;
- `unavailable` or `unknown`: increment failures and open the circuit at the
  configured threshold;
- `invalid_request`: stop because another provider cannot repair the request;
- `invalid_response`: allow bounded fallback.

A successful invocation resets the route failure count and decrements the
known quota estimate. Open circuits become eligible after their cooldown.

## Profile-safe state

Non-secret runtime metadata is stored at:

```text
${HERMES_HOME}/occult/mythos-state.json
```

The default path comes from `get_hermes_home()`, so every Hermes profile has an
independent state file. Writes use a same-directory temporary file followed by
an atomic replace. The persisted schema contains only:

- state version;
- per-card health counters and timestamps;
- per-pool remaining request estimates and cooldown timestamps.

Secret-shaped keys and values are rejected before every write and after every
read. Prompts, outputs, credential references, endpoints, headers, and provider
response bodies are not persisted.

## Rollout and rollback

This slice is not connected to `AIAgent`, CLI, TUI, gateway, dashboard, or API
execution. Rollout requires a later feature-gated Occult entry boundary.

Rollback is therefore:

1. leave `occult.enabled: false`;
2. stop constructing `MythosRouter`;
3. optionally remove the non-secret profile state file.

Existing Hermes provider selection and credential pools remain unchanged.

## Verification

Run the repository test wrapper:

```text
scripts/run_tests.sh tests/agent/occult/test_mythos.py -q
```

On native Windows where `/bin/bash` is unavailable, use the documented
fallback:

```text
venv\Scripts\python.exe -m pytest tests/agent/occult/test_mythos.py -q -n 4
```

The suite covers adapter parity, explicit trust review, local/free policy,
shared quota pools, auth handling, circuit recovery, bounded fallback,
credential expiry and revocation, secret redaction, profile-safe persistence,
and prohibited acquisition actions.
