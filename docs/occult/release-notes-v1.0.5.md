# Tarot Router v1.0.5

This patch release fixes the public first-run status command discovered by the
v1.0.4 clean-install canary. On an uninitialized or explicitly disabled local
profile, `hermes tarot status` now reports local activation state and the next
safe step without requiring a virtual API token or a running gateway.

Enabled profiles retain the authenticated API-backed status response. The
runtime contract remains `1.0.0`, Agents Council remains pinned to signed
`v0.5.5`, Council state schema remains `3`, and Hermes CLI package version
remains `0.14.0`. No runtime API, credential, provider, or paid-route behavior
changed.

## Install

Follow the
[Tarot Router local public v1 quickstart](https://github.com/SgtSlummy/hermes-agent/blob/v1.0.5/docs/tarot-router/quickstart.md).

The release is published without the GitHub `latest` marker. Hermes `v1.0.5`
and Agents Council `v0.5.5` are promoted to latest only after the public
clean-install, local invocation, Council restart/resume, backup/restore, and
rollback canary passes.
