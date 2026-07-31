# Tarot Router v1.0.4

This patch release publishes **Tarot Router** as the user-facing name of the
local-first Hermes and Agents Council runtime. The compatibility identifiers
remain unchanged so existing installations, automation, and stored readings
continue to work.

Version `v1.0.4` adds the public `hermes tarot` command, the authoritative
Tarot Router quickstart, updated operator-facing labels, and the signed Agents
Council `v0.5.4` dependency. The installer remains fail-closed and leaves Tarot
Router disabled until local initialization is explicitly requested.

Runtime contract `1.0.0`, Council state schema `3`, Hermes CLI package
`0.14.0`, `/v1/occult/*` routes, `OCCULT_*` environment variables, state paths,
and signed asset filenames are unchanged. No provider, credential, paid-route,
or runtime API behavior changed.

## Install

Follow the
[Tarot Router local public v1 quickstart](https://github.com/SgtSlummy/hermes-agent/blob/v1.0.4/docs/tarot-router/quickstart.md).

The release is published without the GitHub `latest` marker. Hermes `v1.0.4`
and Agents Council `v0.5.4` are promoted to latest only after the public
clean-install, local invocation, Council restart/resume, backup/restore, and
rollback canary passes.
