# Occult System v1.0.2

This hotfix supersedes the unpromoted `v1.0.1` release. The public Windows
canary found that its staged `hermes.exe` and `council.exe` paths did not end
in `.exe`, so Windows PowerShell refused to execute them before activation.
Version `v1.0.2` preserves `.exe` as the final suffix and adds a regression
guard.

Runtime contract `1.0.0`, Council state schema `3`, Hermes CLI package
`0.14.0`, and the pinned signed Agents Council `v0.5.2` release are unchanged.
No provider, credential, or runtime API behavior changed.

## Install

Follow the
[Occult local public v1 quickstart](https://github.com/SgtSlummy/hermes-agent/blob/v1.0.2/docs/occult/quickstart.md).

The release is published without the GitHub `latest` marker. Hermes `v1.0.2`
and Agents Council `v0.5.2` are promoted to latest only after the public
clean-install, local invocation, Council restart/resume, backup/restore, and
rollback canary passes.
