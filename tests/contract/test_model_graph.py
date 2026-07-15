from __future__ import annotations

import ast
from pathlib import Path
import unittest


class ModelGraphBoundaryTests(unittest.TestCase):
    def test_graph_core_has_no_gateway_or_vendor_imports(self) -> None:
        root = Path(__file__).resolve().parents[2]
        paths = (
            root / "src/powerfactory_agent/domain/topology.py",
            root / "src/powerfactory_agent/operations/model_graph.py",
            root / "src/powerfactory_agent/persistence/model_graph_store.py",
        )
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            self.assertFalse(any("gateway" in module or module == "powerfactory" for module in modules), path)

