"""Phase D: explicit, side-effect-free runtime construction and idempotent startup.

Construction must capture validated configuration only and create no database,
worker thread, or native session. Side effects move behind an explicit,
idempotent ``_start()`` invoked on confirmed context admission. Portable
startup-success evidence requires real PowerFactory and is therefore Windows
blocked; the partial-failure path is deterministic without PowerFactory because
the native start handler cannot load ``powerfactory.pyd`` on this host.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from powerfactory_agent.mcp.configuration import configure_probe, create_installation, load_installation
from powerfactory_agent.mcp.runtime import PowerFactoryEngineeringRuntime, RuntimeOperationFailure


_OWNER_THREAD = "powerfactory-serialized-owner"
_WATCHDOG_THREAD = "powerfactory-operation-watchdog"


def _make_installation(state_dir: Path) -> object:
    create_installation(state_dir)
    pyd_path = state_dir.parent / "powerfactory.pyd"
    pyd_path.touch()
    configure_probe(
        state_dir / "powerfactory-agent.json",
        {
            "pyd_path": str(pyd_path),
            "python_version": "3.12",
            "sample_limit": 1,
            "cardinality_ceiling": 10,
            "include_out_of_service": False,
            "session_ownership": "attached",
        },
    )
    return load_installation(state_dir / "powerfactory-agent.json")


def _owner_threads() -> set[str]:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.name in {_OWNER_THREAD, _WATCHDOG_THREAD}
    }


class RuntimeConstructionTests(unittest.TestCase):
    def test_construction_creates_no_database_thread_or_native_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "agent"
            installation = _make_installation(state_dir)
            before = _owner_threads()

            runtime = PowerFactoryEngineeringRuntime(installation)

            self.assertFalse(runtime._started)
            self.assertIsNone(runtime._owner)
            self.assertIsNone(runtime._database)
            self.assertIsNone(runtime._session)
            self.assertFalse((state_dir / "powerfactory-agent.sqlite3").exists())
            self.assertEqual(before, _owner_threads())

    def test_close_before_startup_is_a_harmless_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "agent"
            installation = _make_installation(state_dir)

            runtime = PowerFactoryEngineeringRuntime(installation)
            runtime.close()  # must not raise

            self.assertFalse(runtime._started)
            self.assertIsNone(runtime._owner)


class RuntimeStartupFailureTests(unittest.TestCase):
    def test_partial_startup_failure_is_deterministic_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "agent"
            installation = _make_installation(state_dir)

            runtime = PowerFactoryEngineeringRuntime(installation)
            with self.assertRaises(RuntimeOperationFailure):
                runtime._start()

            self.assertFalse(runtime._started)
            self.assertIsNone(runtime._owner)
            self.assertIsNone(runtime._session)
            self.assertEqual(set(), _owner_threads())

            # close before/after a failed startup remains harmless.
            runtime.close()


if __name__ == "__main__":
    unittest.main()