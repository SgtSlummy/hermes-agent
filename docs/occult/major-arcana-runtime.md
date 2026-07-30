# Major Arcana package and pairing runtime

This foundation implements signed, uploadable Major Arcana agents without
changing Hermes behavior by default. Importing `agent.occult` does not install
packages, activate agents, alter prompts, or execute tools.

## Package lifecycle

A `.tarot` package is a ZIP-compatible, data-only archive containing:

```text
manifest.yaml
system_prompt.md
behavior.yaml
routing.yaml
memory.yaml
tools.yaml
signature.json
```

`TarotPackageManager` validates every member before writing anything:

- archive, expansion, entry-count, path-depth, and compression-ratio limits;
- no absolute paths, traversal, backslashes, drive prefixes, links, duplicate
  members, or executable file types;
- strict Pydantic models with unknown fields rejected;
- exact SHA-256 inventory signed with an Ed25519 key from the caller's trusted
  signer map;
- system ceilings for risk, external or paid routing, memory sensitivity, and
  available tools.

Versions install immutably under:

```text
${HERMES_HOME}/occult/major_arcana/packages/<agent-id>/<version>/
```

The active-version registry is written atomically. Activation and rollback
increment a generation counter; they do not mutate a running session.

## Pairing boundary

`PairingSession.start()` snapshots one active package version and generation.
That snapshot is the stable Major Arcana identity for the session. A Minor
Arcana route may change between calls, but it cannot replace the identity,
expand permissions, or expose credential references.

Pairing:

1. checks orientation, route capabilities, external access, and paid access;
2. clamps model temperament modifiers to the Major Arcana's declared range;
3. intersects package and system memory namespaces;
4. applies the strictest package, system, and route privacy ceiling;
5. returns deterministic, inspectable prompt material.

Public external routes receive only public memory. Private external routes may
receive internal memory only when both package and system policy permit it.
Local routes remain subject to the package and system ceilings.

## Tool authorization

Model output is never an authorization decision. The caller passes each
requested tool to `PairingSession.authorize_tool()` before normal Hermes tool
dispatch. A tool must:

- be allowed by both package and runtime policy;
- remain within both risk ceilings; and
- satisfy the union of system-required and package-required approval rules.

An `allowed` result permits the caller to continue through Hermes' existing
tool checks. `approval_required` must use the existing approval flow.
`denied` must not be dispatched.

## Session-boundary integration

To preserve prompt caching, construct a `PairingSession` when a Hermes session
starts and retain it until the session ends. Do not reload the active package
on every turn. Package activation and rollback then take effect on the next
session boundary.

## Validation

On supported POSIX environments:

```bash
scripts/run_tests.sh tests/agent/occult -q
```

Windows fallback:

```powershell
venv\Scripts\python.exe -m pytest tests\agent\occult -q -n 4
```
