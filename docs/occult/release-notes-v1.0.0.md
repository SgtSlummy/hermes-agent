# Occult System v1.0.0

This is the first GitHub-ready Occult release for Hermes Agent and Agents
Council.

## Included

- A real feature-gated runtime composition root in the Hermes gateway.
- Five deterministic, signed starter Major Arcana packages.
- A local Ollama Minor Arcana route and zero-cost starter deck.
- Idempotent `hermes occult init` onboarding with scoped, profile-local tokens.
- OpenAI-compatible and Occult-native loopback APIs.
- Durable Council readings with restart, cancellation, approval, and resume.
- CLI, MCP, and Council Hall interfaces behind an explicit feature flag.
- Reproducible assembly, checksums, SBOM, provenance, signatures, and release
  verification on Linux, macOS, Windows, Nix, and OCI targets.

## Secure defaults

Occult is disabled until explicitly initialized. The starter deck is
local-only, free-only, and capped at zero dollars. Provider credentials remain
inside Hermes; Council receives only a scoped service token and sanitized
results.

## Install

Install Hermes and Ollama, then:

```text
ollama pull qwen2.5:3b
hermes occult init --model qwen2.5:3b
hermes gateway restart
hermes occult status
```

See `docs/occult/production-operations.md` for verification, backup, update,
rollback, and incident procedures.
