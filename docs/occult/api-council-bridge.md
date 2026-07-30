# Occult API, CLI, and Agents Council bridge

This slice exposes the validated Occult core through an explicitly assembled,
authenticated Hermes surface. It remains disabled by default.

## Assembly

Create the package manager, Mythos router, runtime policy, virtual-token
authority, and reading store in the trusted Hermes composition root. Then:

```python
occult_service = OccultService(
    package_manager=package_manager,
    router=mythos_router,
    token_authority=token_authority,
    runtime_policy=runtime_policy,
)
occult_http = OccultHTTPAdapter(
    service=occult_service,
    readings=reading_store,
    reading_executor=council_executor,
)
api_server.attach_occult_http(occult_http)
```

Attachment must happen before `APIServerAdapter.connect()`. No Occult route is
registered when no adapter is attached.

## Authentication and authorization

Every `/v1/occult/*` route requires `Authorization: Bearer <virtual-token>`.
An Occult virtual token can restrict:

- Major Arcana agent IDs;
- Minor Arcana card IDs;
- tools and memory namespaces;
- requests per minute;
- total cost budget; and
- expiration.

Only a SHA-256 digest is retained. The plaintext token is returned once when
issued. Provider credentials never cross this boundary.

## Native endpoints

```text
GET  /v1/occult/major-arcana
GET  /v1/occult/minor-arcana
POST /v1/occult/invoke
POST /v1/occult/readings
GET  /v1/occult/readings/{reading_id}
GET  /v1/occult/readings/{reading_id}/events
POST /v1/occult/readings/{reading_id}/resume
POST /v1/occult/readings/{reading_id}/cancel
```

The standard Hermes transcript/composer remains unchanged.

## Council boundary

`POST /v1/occult/invoke` is the cross-repository, per-node Council bridge.
It returns the strict profile expected by Agents Council:

```json
{
  "contract_version": "1.0.0",
  "invocation_id": "invocation-id",
  "status": "completed",
  "summary": "Node output.",
  "route_summary": {
    "contract_version": "1.0.0",
    "invocation_id": "invocation-id",
    "selected_card_id": "minor.pentacles.ace.local",
    "provider_id": "local",
    "model_id": "model",
    "fallback_count": 0,
    "explanation": "selected by validated Mythos policy"
  },
  "artifacts": [],
  "error": null
}
```

Failed validated requests use the same envelope with `status: "failed"`, a
null route summary, and a redacted contract error. Contract mismatch is
rejected before a provider call.

Agents Council remains the authority for a remote Council reading graph and
its durable scheduler state. Hermes owns Major Arcana composition, Mythos,
provider credentials, memory/tool policy, and invocation audit data.

`ReadingStore` is an optional Hermes-native/offline workflow harness. It
persists readings, nodes, artifacts, and ordered events in the profile-scoped
`occult/readings.db`. When used, its executor receives:

- the pinned contract version;
- reading, node, and agent identifiers;
- the node task;
- a stable per-node idempotency key; and
- opaque input artifact references.

It does not receive provider credentials or dependency artifact bodies.
Hermes stores artifact content and emits only artifact references plus
secret-checked route summaries.

A completed node is never executed again after restart. A node that was
running at process loss returns to pending with the same idempotency key, so
the downstream executor must also honor that key. Cancellation, failure, and
completion are protected by a unique terminal-event index. Do not run both
the Council scheduler and this optional local scheduler as authorities for the
same reading.

## CLI

Set:

```text
OCCULT_API_URL=http://127.0.0.1:8642
OCCULT_API_KEY=<scoped-occult-virtual-token>
```

Then use:

```text
hermes occult status
hermes occult agents
hermes occult routes
hermes occult invoke --agent occult.major.magician --message "Build it"
hermes occult reading-status <reading-id>
hermes occult reading-resume <reading-id>
hermes occult reading-cancel <reading-id>
```

CLI output is JSON for scripting and recovery.

## Agents Council compatibility

Hermes and Agents Council must use `contract_version: 1.0.0`. A mismatch is
rejected before provider invocation. Council should implement its executor as
an authenticated bridge that resolves only opaque references through a
separate authorized artifact channel.

## Current production gate

Before enabling this adapter in a release composition root:

1. provision trusted package signers;
2. register reviewed Mythos routes and legitimate credentials;
3. issue least-privilege virtual tokens;
4. provide an idempotent Council executor;
5. back up `occult/readings.db`; and
6. run the cross-repository contract fixtures and restart-resume scenario.
