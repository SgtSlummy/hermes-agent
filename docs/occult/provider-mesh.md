# Free provider mesh

The Tarot Router can activate a reviewed subset of the bundled free-provider
catalog. The feature is **off by default** and does not change the existing
local Ollama route. Enabling it is an operator action because each external
provider has its own terms, quota, region, and authorization requirements.

## What is activated

When enabled, Hermes:

- selects only the provider IDs listed in `occult.provider_mesh.provider_ids`;
- permits only catalog entries marked free and terms-compatible;
- supports the reviewed OpenAI-compatible adapter first;
- uses only catalog-listed zero-cost model IDs;
- accepts bearer credentials only from the named environment variables in the
  catalog; and
- keeps credentials in the process-local protected broker, never in route
  descriptors, state files, logs, prompts, Council messages, or API results.

The router does not create accounts, solve CAPTCHAs, scrape keys, use shared or
leaked credentials, or rotate accounts to evade quotas. A provider without an
authorized key remains `pending_authorization`.

## Configuration

Add this under the existing `occult` object in Hermes' profile configuration:

```yaml
occult:
  enabled: true
  contract_version: "1.0.0"
  local_base_url: "http://127.0.0.1:11434/v1"
  local_model: "qwen2.5:3b"

  provider_mesh:
    enabled: true
    provider_ids:
      - ai-horde
      - ovh
      - openrouter
      - groq
    allow_anonymous: true
    auto_enroll_keyless: true
    allow_external_routes: true
    discover_models: true
    max_models_per_provider: 2
    timeout_seconds: 8
```

`allow_external_routes` is a separate explicit gate. Leave it `false` to
catalog and health-check routes without allowing Major Arcana agents to send
data off the machine. External routes receive only public-classification
memory under the existing runtime policy.

With `auto_enroll_keyless: true` and no explicit `provider_ids`, the runtime
tries every reviewed, terms-compatible, zero-cost keyless catalog entry during
startup. This is the unattended path: it creates no account and stores no
credential. Bearer and OAuth providers remain pending until an authorized
credential is supplied through the protected environment or secret broker.

The signed GitHub installer exposes the same path as one explicit command:

```text
Windows:  install-occult.ps1 -InitializeLocal -EnableKeylessMesh
POSIX:    install-occult.sh --initialize-local --enable-keyless-mesh
```

Because this flag explicitly permits external routes, the starter token can
use successfully enrolled keyless cards. The installer still fails closed for
unverified, paid, credentialed, or unreachable providers and remains
idempotent on rerun.

Bearer credentials are supplied through the provider's official authorization
flow and then placed in the protected local environment using the exact
catalog reference. For example:

```text
OPENROUTER_API_KEY=<value obtained from the OpenRouter console>
GROQ_API_KEY=<value obtained from the Groq console>
```

Do not put keys in YAML, source control, command arguments, issue reports, or
Council messages. The catalog is metadata, not a key store.

## Activation and verification

After changing the profile, restart Hermes. Then inspect:

```text
hermes tarot status
hermes tarot routes
hermes tarot providers
```

The provider response distinguishes:

- `active`: a live, validated route is registered;
- `awaiting_authorized_credential`: the provider is allowed by policy but no
  official credential was supplied; and
- `blocked_by_free_policy`: the catalog entry is not eligible for free-only
  routing.

The live route list contains only redacted card, provider, and model IDs. It
never contains a key, bearer header, signed URL, prompt, or provider response
body.

## Routing behavior

The starter deck stays free-only. With external routing explicitly enabled, it
uses local-first scoring and can fall back across active free Minor Arcana
routes. A route is removed from candidates when its credential is unavailable,
its model is not on the zero-cost allowlist, its endpoint fails the HTTPS and
official-host checks, or Mythos opens its circuit breaker.

Council readings use the same policy. They remain local-only unless
`allow_external_routes` is explicitly true, and provider outages are reported
as retryable reading failures rather than silently spending money.

## Rollback

Set `occult.provider_mesh.enabled` to `false` or remove the block, restart
Hermes, and the original single-route local Ollama behavior returns. No
provider credentials are deleted by this rollback. Revoke them through each
provider's official console when they are no longer needed.
