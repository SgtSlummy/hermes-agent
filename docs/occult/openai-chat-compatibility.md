# Occult OpenAI Chat Compatibility

The feature-gated Occult HTTP adapter exposes a strict OpenAI-compatible
surface for existing clients:

- `GET /v1/models`
- `POST /v1/chat/completions`

Both endpoints require a Hermes-issued Occult virtual token:

```text
Authorization: Bearer <occult-virtual-token>
```

Provider credentials remain inside Hermes and are never returned to clients.

## Model mapping

`GET /v1/models` returns the Major Arcana agents allowed by the caller's
virtual-token policy. A Chat Completions request selects that Major Arcana
agent through the standard `model` field:

```json
{
  "model": "occult.major.magician",
  "messages": [
    {"role": "user", "content": "Build the smallest safe implementation."}
  ]
}
```

Mythos chooses an eligible Minor Arcana route. An Occult-aware client may
request a permitted pairing through the optional extension:

```json
{
  "model": "occult.major.magician",
  "messages": [
    {"role": "user", "content": "Review the implementation."}
  ],
  "occult": {
    "minor_arcana": "minor.pentacles.ace.local.test",
    "orientation": "reversed",
    "deck_id": "occult.deck.development"
  }
}
```

The extension cannot widen virtual-token, deck, cost, privacy, or provider
permissions. Hermes validates and authorizes the request before a provider
call.

## Supported request subset

The initial compatibility slice accepts:

- `model`
- text-only `messages` using `assistant`, `developer`, `system`, and `user`
  roles
- `stream`
- `user`
- the optional `occult` extension

Streaming returns valid server-sent events with one complete content chunk, a
terminal chunk, and `[DONE]`. The current Mythos provider contract is
synchronous, so this endpoint does not claim token-by-token upstream
streaming.

Unsupported parameters and non-text content return an OpenAI-shaped `400`
error before any provider call. This is intentional: the compatibility layer
does not silently ignore sampling, tool, audio, or vision controls that the
current Occult contract cannot honor.

## Client configuration

```text
OPENAI_BASE_URL=http://127.0.0.1:8787/v1
OPENAI_API_KEY=<occult-virtual-token>
```

The Occult adapter remains inert until the existing Occult feature gate is
enabled and the adapter is registered by the host application.

When attached to Hermes' existing API server, Occult does not register a
second copy of `/v1/models` or `/v1/chat/completions`. The existing gateway
handlers dispatch only `Bearer occult_...` requests to the Occult adapter;
the configured Hermes API key and all non-Occult traffic continue through the
original gateway path. A standalone Occult-only application may use the
adapter's default route registration directly.

## Rollback

Remove the `OccultOpenAIAdapter` registration from `agent/occult/http.py`.
The Occult-native endpoints and underlying service remain unchanged; no
database migration or credential operation is involved.
