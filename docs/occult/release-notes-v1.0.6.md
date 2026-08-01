# Tarot Router v1.0.6

This launch patch makes verified installer reruns idempotent. After the current
Hermes and Agents Council release assets pass Sigstore and SHA-256 verification,
the Windows and POSIX installers now reuse an exact matching installation only
when its receipt, environment identifiers, active commands, staged command
copies, and versions all validate. An identical rerun does not replace the
active commands or receipt.

Explicit `--initialize-local` / `-InitializeLocal` remains opt-in and may update
only the preserved local profile state and the corresponding receipt flags.
Invalid, incomplete, mismatched, or damaged installations are repaired through
the existing staged activation path.

Runtime API paths, `OCCULT_*` compatibility identifiers, runtime contract
`1.0.0`, Council state schema `3`, Hermes package version `0.14.0`, and the
pinned Agents Council `v0.5.5` release are unchanged.

Hermes v1.0.4 and v1.0.5 remain public but unpromoted because their public
canaries found pre-initialization status and installer-idempotency defects,
respectively. Promote v1.0.6 only after its downloaded public assets pass the
complete launch and rerun canaries.
