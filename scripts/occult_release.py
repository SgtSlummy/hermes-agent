#!/usr/bin/env python3
"""Assemble, verify, or promote an Occult release without rebuilding."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent.occult.release import (
    OccultReleaseError,
    assemble_release,
    promote_release,
    verify_release,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--artifacts", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--version", required=True)
    assemble.add_argument("--commit", required=True)
    assemble.add_argument(
        "--channel",
        choices=("nightly", "preview", "stable"),
        required=True,
    )
    assemble.add_argument(
        "--source-date-epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")),
    )
    assemble.add_argument("--source-root", type=Path, default=Path.cwd())

    verify = commands.add_parser("verify")
    verify.add_argument("release", type=Path)
    signature = verify.add_mutually_exclusive_group()
    signature.add_argument(
        "--require-signature",
        action="store_true",
        dest="require_signature",
        default=None,
    )
    signature.add_argument(
        "--allow-unsigned",
        action="store_false",
        dest="require_signature",
        help="staging verification only; stable promotion still requires Sigstore",
    )

    promote = commands.add_parser("promote")
    promote.add_argument("staged", type=Path)
    promote.add_argument("destination", type=Path)
    promote.add_argument(
        "--allow-unsigned-preview",
        action="store_true",
        help="never use for stable promotion",
    )

    args = parser.parse_args()
    try:
        if args.command == "assemble":
            result = assemble_release(
                args.artifacts,
                args.output,
                version=args.version,
                commit_sha=args.commit,
                channel=args.channel,
                source_date_epoch=args.source_date_epoch,
                source_root=args.source_root,
            )
        elif args.command == "verify":
            result = verify_release(
                args.release,
                require_signature=args.require_signature,
            )
        else:
            result = promote_release(
                args.staged,
                args.destination,
                require_signature=not args.allow_unsigned_preview,
            )
    except OccultReleaseError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
