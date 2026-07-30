#!/usr/bin/env python3
"""Build the reviewed, signed Major Arcana starter archives.

Maintainers provide a base64-encoded raw Ed25519 private key through
``OCCULT_STARTER_SIGNING_KEY``.
The ``--ephemeral`` mode exists only to bootstrap a new trust root; it never
writes or prints the generated private key.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.occult.tarot_packages import signature_payload

SIGNER_ID = "occult-starter-v1"
AGENTS = (
    {
        "slug": "magician",
        "id": "occult.major.magician",
        "name": "The Magician",
        "number": 1,
        "description": "Turns clear intent into careful implementation.",
        "prompt": "Build practical solutions while preserving user intent and system boundaries.",
        "upright": "Create the smallest complete implementation and verify it.",
        "reversed": "Test feasibility, expose missing assumptions, and avoid premature implementation.",
        "temperament": {"creativity": 0.60, "precision": 0.85},
    },
    {
        "slug": "justice",
        "id": "occult.major.justice",
        "name": "Justice",
        "number": 11,
        "description": "Audits correctness, evidence, and policy.",
        "prompt": "Evaluate work against explicit evidence, contracts, and safety policy.",
        "upright": "Audit the result and state each actionable defect precisely.",
        "reversed": "Search for blind spots, hidden assumptions, and overconfident conclusions.",
        "temperament": {"creativity": 0.25, "precision": 0.95},
    },
    {
        "slug": "temperance",
        "id": "occult.major.temperance",
        "name": "Temperance",
        "number": 14,
        "description": "Combines compatible findings into one coherent result.",
        "prompt": "Synthesize multiple inputs without erasing disagreements or uncertainty.",
        "upright": "Reconcile the inputs into a balanced, actionable synthesis.",
        "reversed": "Identify combinations that should remain separate and explain why.",
        "temperament": {"creativity": 0.55, "precision": 0.80},
    },
    {
        "slug": "judgement",
        "id": "occult.major.judgement",
        "name": "Judgement",
        "number": 20,
        "description": "Scores completion and determines whether another pass is needed.",
        "prompt": "Apply explicit acceptance criteria and return a defensible completion decision.",
        "upright": "Judge the result against the criteria and prescribe the next pass when needed.",
        "reversed": "Challenge the criteria themselves for gaps, bias, or impossible requirements.",
        "temperament": {"creativity": 0.30, "precision": 0.95},
    },
    {
        "slug": "world",
        "id": "occult.major.world",
        "name": "The World",
        "number": 21,
        "description": "Coordinates the complete workflow and closes the reading.",
        "prompt": "Coordinate bounded work, preserve dependencies, and return a complete final state.",
        "upright": "Orchestrate the work from intake through verified completion.",
        "reversed": "Find unfinished dependencies and prevent a false declaration of completion.",
        "temperament": {"creativity": 0.50, "precision": 0.90},
    },
)


def package_files(agent: dict[str, object]) -> dict[str, bytes]:
    temperament = {
        axis: {
            "default": value,
            "minimum": max(0.0, float(value) - 0.30),
            "maximum": min(1.0, float(value) + 0.15),
        }
        for axis, value in dict(agent["temperament"]).items()
    }
    manifest = {
        "format_version": "1.0",
        "agent": {
            "id": agent["id"],
            "name": agent["name"],
            "arcana_number": agent["number"],
            "version": "1.0.0",
            "description": agent["description"],
        },
        "orientation": {"upright": True, "reversed": True},
        "capabilities": ["text"],
        "temperament": temperament,
        "permissions": {"maximum_risk_level": 0},
        "entrypoints": {
            "system_prompt": "system_prompt.md",
            "behavior": "behavior.yaml",
            "routing": "routing.yaml",
            "memory": "memory.yaml",
            "tools": "tools.yaml",
        },
    }
    return {
        "manifest.yaml": yaml.safe_dump(manifest, sort_keys=False).encode(),
        "system_prompt.md": (str(agent["prompt"]).strip() + "\n").encode(),
        "behavior.yaml": yaml.safe_dump(
            {"upright": agent["upright"], "reversed": agent["reversed"]},
            sort_keys=False,
        ).encode(),
        "routing.yaml": yaml.safe_dump(
            {
                "required_capabilities": ["text"],
                "allow_paid": False,
                "allow_external": False,
            },
            sort_keys=False,
        ).encode(),
        "memory.yaml": yaml.safe_dump(
            {
                "namespaces": ["project", "agent", "reading"],
                "maximum_sensitivity": "internal",
                "external_maximum_sensitivity": "public",
            },
            sort_keys=False,
        ).encode(),
        "tools.yaml": yaml.safe_dump(
            {"allowed": [], "approval_required": []},
            sort_keys=False,
        ).encode(),
    }


def write_archive(
    path: Path,
    private_key: Ed25519PrivateKey,
    files: dict[str, bytes],
) -> None:
    hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())
    }
    signature = {
        "algorithm": "ed25519",
        "format_version": "1.0",
        "signer_id": SIGNER_ID,
        "files": hashes,
        "signature": base64.b64encode(
            private_key.sign(signature_payload(SIGNER_ID, hashes))
        ).decode(),
    }
    content = {
        **files,
        "signature.json": json.dumps(
            signature, sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(content.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ephemeral", action="store_true")
    args = parser.parse_args()
    encoded_key = os.getenv("OCCULT_STARTER_SIGNING_KEY", "").strip()
    if bool(encoded_key) == bool(args.ephemeral):
        parser.error("choose exactly one of OCCULT_STARTER_SIGNING_KEY or --ephemeral")
    if args.ephemeral:
        private_key = Ed25519PrivateKey.generate()
    else:
        try:
            raw = base64.b64decode(encoded_key, validate=True)
            private_key = Ed25519PrivateKey.from_private_bytes(raw)
        except (TypeError, ValueError):
            parser.error(
                "OCCULT_STARTER_SIGNING_KEY must be a Base64-encoded "
                "32-byte Ed25519 private key"
            )

    args.output.mkdir(parents=True, exist_ok=True)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signer_payload = {
        "format_version": "1.0",
        "signers": {SIGNER_ID: base64.b64encode(public_key).decode()},
    }
    (args.output / "starter_signers.json").write_text(
        json.dumps(signer_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for agent in AGENTS:
        write_archive(
            args.output / f"{agent['slug']}.tarot",
            private_key,
            package_files(agent),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
