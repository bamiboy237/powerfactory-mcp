#!/usr/bin/env python3
"""Run the vendor-neutral lifecycle probe with a configured Windows adapter."""

from __future__ import annotations

import argparse
import importlib
from typing import Callable, cast

from powerfactory_agent.probes import (
    LifecycleAdapter,
    LifecycleProbeRunner,
    write_evidence_json,
)


_DEFAULT_ADAPTER = (
    "powerfactory_agent.probes.powerfactory2026:create_powerfactory2026_adapter"
)


def _load_adapter(specification: str) -> LifecycleAdapter:
    module_name, separator, factory_name = specification.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("adapter must use the form 'module:factory'")

    module = importlib.import_module(module_name)
    factory = cast(Callable[[], object], getattr(module, factory_name))
    adapter = factory()
    if not isinstance(adapter, LifecycleAdapter):
        raise TypeError("adapter factory must return a LifecycleAdapter")
    return adapter


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--adapter",
        default=_DEFAULT_ADAPTER,
        help=(
            "adapter factory as module:factory; defaults to the first-party "
            "PowerFactory 2026 adapter configured by POWERFACTORY_PROBE_CONFIG"
        ),
    )
    args = parser.parse_args()
    return args


def main() -> int:
    args = _parse_args()
    evidence = LifecycleProbeRunner(_load_adapter(args.adapter)).run(args.repeat)
    write_evidence_json(evidence, args.output)
    return 0 if evidence.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
