# Tarot Router provider and card control

The Occult dashboard now exposes two secret-free control-plane views:

- Major Arcana cards show the agent description, upright soul, and reversed soul.
- Minor Arcana cards show the live route or pending card, suit, rank, provider,
  model, capabilities, activation state, and public soul.
- Provider cards show free-access classification, enrollment mode, zero-cost
  model IDs, official authorization links, and the reason a route is pending.

## Adding a provider

Use **Arcana control room → Add provider card**. Only public metadata is
accepted. The form never accepts an API key, bearer token, refresh token, or
password. New entries are written to the profile-scoped `card-registry.json`
file with `source_state: operator_added`.

The router assigns enrollment mode automatically:

- `keyless`: local or anonymous routes can be probed by the unattended mesh.
- `preauthorized`: bearer routes wait for an authorized credential reference.
- `human_required`: OAuth or provider-specific authorization remains pending
  with the official provider link visible in the dashboard.

Adding metadata does not activate a route. Activation still requires the
normal HTTPS host, adapter, free-policy, terms, credential, quota, health, and
zero-cost model gates.

## Adding a Minor Arcana card

Use **Add Minor Arcana card** to register a public card description and soul.
The card is stored as `pending_review` and is not eligible for invocation until
its provider and model have been validated and explicitly promoted by Mythos.

## Credential boundary

The browser receives only normalized registry data. Provider credentials remain
in the router process or configured secret broker and are never returned by the
dashboard status endpoint, card registry, audit output, or agent context.

## Compatibility

The dashboard treats the cards endpoint as optional while an older Tarot Router
runtime is being upgraded. Existing routes continue to work, and the provider
catalog remains immutable and reviewable.
