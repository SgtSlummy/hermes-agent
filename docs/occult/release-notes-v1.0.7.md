# Tarot Router v1.0.7

This launch patch restores Windows PowerShell 5.1 compatibility for the
verified installer path-state probe. PowerShell 5.1 removes embedded double
quotes from a native Python `-c` argument; the probe now uses literals that
survive that argument boundary while retaining the existing reparse-point,
hardlink, directory, and independent-file protections.

A Windows regression test executes the exact embedded probe through Windows
PowerShell against both absent and present managed-command paths. The full
public canary still requires per-user installation, explicit Ollama
initialization, idempotent reruns, tamper repair, Council pause/restart/resume,
backup and restore, rollback, and redacted audit output.

Runtime API paths, `OCCULT_*` compatibility identifiers, runtime contract
`1.0.0`, Council state schema `3`, Hermes package version `0.14.0`, and the
pinned Agents Council `v0.5.5` release are unchanged.

Hermes v1.0.4, v1.0.5, and v1.0.6 remain public but unpromoted because their
public canaries found launch blockers. Promote v1.0.7 only after its downloaded
public assets pass the complete launch and rerun canaries.

The protected final-promotion workflow independently checks the redacted public
canary, GitHub asset digests, Sigstore identities, signed checksum manifests,
and the release bundle before marking Council and Hermes as `latest`.
