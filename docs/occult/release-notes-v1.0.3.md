# Occult System v1.0.3

This hotfix supersedes the unpromoted `v1.0.2` release. Its public Windows
installer passed signature, checksum, dependency, and executable staging
verification, then exposed a Windows PowerShell 5 native-argument quoting
incompatibility in the inline Python state probe.

Version `v1.0.3` writes that fixed probe into the installer-owned temporary
directory and executes the file directly. This preserves the probe byte-for-byte
and the existing temporary-directory cleanup removes it after the installer
finishes.

Runtime contract `1.0.0`, Council state schema `3`, Hermes CLI package
`0.14.0`, and the pinned signed Agents Council `v0.5.2` release are unchanged.
No provider, credential, or runtime API behavior changed.

## Install

Follow the
[Occult local public v1 quickstart](https://github.com/SgtSlummy/hermes-agent/blob/v1.0.3/docs/occult/quickstart.md).

The release is published without the GitHub `latest` marker. Hermes `v1.0.3`
and Agents Council `v0.5.2` are promoted to latest only after the public
clean-install, local invocation, Council restart/resume, backup/restore, and
rollback canary passes.
