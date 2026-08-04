# Occult System v1.0.1

This patch makes the local Occult runtime installable from signed GitHub
release assets while preserving runtime contract `1.0.0` and Council state
schema `3`.

## Included

- Hermes-owned PowerShell and POSIX installers for Hermes and the pinned Agents
  Council `v0.5.2` dependency.
- Per-user installation without administrator rights.
- Exact Sigstore workflow-identity verification plus SHA-256 verification
  before application files are installed.
- Explicit `--version`, `--install-root`, `--initialize-local`,
  `--enable-keyless-mesh`, `--skip-council`, and `--verify-only` options.
- A signed minimal wheel/install-asset set alongside the complete SBOM,
  provenance, Nix, OCI, dashboard, TUI, and documentation bundle.
- One authoritative public quickstart for installation, Ollama initialization,
  invocation, Council recovery, backup, disablement, and rollback.
- A redacted operator canary gate that contains no prompts, credentials,
  tokens, telemetry, or signed download URLs.

## Secure defaults

Occult remains disabled after installation. Local initialization happens only
when explicitly requested. The starter route is local-only, free-only, and
capped at zero dollars. No cloud provider credential is required or acquired.
When explicitly combined with local initialization, `--enable-keyless-mesh`
enrolls reviewed keyless/free catalog routes without creating accounts or
handling provider credentials. Credentialed providers remain pending.

## Install

Follow the
[Occult local public v1 quickstart](https://github.com/SgtSlummy/hermes-agent/blob/v1.0.1/docs/occult/quickstart.md).

The release is initially published without the GitHub `latest` marker. Hermes
`v1.0.1` and Agents Council `v0.5.2` are promoted to latest only after the
clean-machine installation, invocation, restart/resume, backup/restore, and
rollback canary passes.
