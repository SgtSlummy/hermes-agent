# Tarot Router v1.0.8

Tarot Router v1.0.8 is the local-first public launch patch for Intel macOS
verifier compatibility. It preserves Hermes CLI 0.14.0, Agents Council v0.5.5,
runtime contract 1.0.0, and Council state schema 3.

## Changes

- Pins the Sigstore verifier to `cryptography==48.0.0`, which provides a
  universal2 macOS wheel for clean Intel and Apple Silicon installations.
- Requires binary-only Sigstore verifier dependencies during installation.
- Adds an Intel macOS verifier-lock gate before immutable release assembly.
- Retains the reviewed Windows public-canary harness correction from PR #45.

Tarot Router remains disabled after installation. Local Ollama initialization
is explicit, and paid-provider fallback remains disabled.

## Promotion policy

Earlier v1.0.4 through v1.0.7 releases remain immutable and unpromoted because
public canaries found launch blockers. Promote v1.0.8 only after its downloaded
Windows assets pass the redacted public canary and all four signed POSIX
installer canaries pass from the exact published release.
