"""Generate or verify the checked-in domain JSON Schema."""

from __future__ import annotations

import argparse
from pathlib import Path

from powerfactory_agent.domain.schema import SCHEMA_PATH, check_schema, write_schema


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the checked-in schema differs")
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[2]
    if args.check:
        if not check_schema(repository_root):
            parser.error(f"{SCHEMA_PATH} is missing or stale; regenerate it")
        return 0
    destination = write_schema(repository_root)
    print(destination.relative_to(repository_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
