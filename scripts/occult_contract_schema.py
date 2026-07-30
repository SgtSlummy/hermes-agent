"""Export or verify the language-neutral Occult v1 JSON Schema bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.occult.contracts import contract_json_schema

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "agent" / "occult" / "spec" / "v1" / "contract.schema.json"
)


def _render_schema() -> str:
    return json.dumps(
        contract_json_schema(),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in schema differs from the runtime models",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="schema destination",
    )
    args = parser.parse_args()

    rendered = _render_schema()
    output = args.output.resolve()

    if args.check:
        if not output.is_file():
            print(f"missing Occult contract schema: {output}")
            return 1
        if output.read_text(encoding="utf-8") != rendered:
            print(
                "Occult contract schema is stale; run "
                "python scripts/occult_contract_schema.py"
            )
            return 1
        print(f"Occult contract schema is current: {output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote Occult contract schema: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
