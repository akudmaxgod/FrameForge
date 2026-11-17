"""Entry point for the focused Story Analyzer V3 test system.

Usage:
    .\\.venv\\Scripts\\python.exe tests/run_v3_test_system.py hard
    .\\.venv\\Scripts\\python.exe tests/run_v3_test_system.py critical
    .\\.venv\\Scripts\\python.exe tests/run_v3_test_system.py regression
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TESTS_DIR))

SUITES = {
    # Mandatory product gate.  These modules exercise fail-closed contracts,
    # approval lifecycle, stale-source protection and every animation path.
    # Broad historical regressions stay available through ``full`` below.
    "hard": [
        "test_forgotten_cities_contracts",
        "test_forgotten_cities_stage10_acceptance",
        "test_veo_animation",
        "test_v3_critical_contracts",
        "test_channel_profiles",
    ],
    "critical": ["test_v3_critical_contracts"],
    "regression": [
        "test_v3_critical_contracts",
        "test_document_ir_v3",
        "test_entities_facts_v3",
        "test_coreference_v3",
        "test_v3_groq_arbitration",
        "test_v3_semantic_first",
        "test_v3_two_layer_audit",
        "test_v3_event_planner",
        "test_v3_persistence_ui",
        "test_v3_acceptance_e2e",
        "test_v3_grounding",
        "test_v3_real_project_flow",
    ],
    "full": "discover",
}


def main() -> int:
    suite_name = sys.argv[1] if len(sys.argv) > 1 else "critical"
    if suite_name not in SUITES:
        print(f"Unknown suite {suite_name!r}. Choose: {', '.join(SUITES)}")
        return 2

    loader = unittest.defaultTestLoader
    if SUITES[suite_name] == "discover":
        suite = loader.discover(str(TESTS_DIR), pattern="test_*.py", top_level_dir=str(PROJECT_ROOT))
    else:
        suite = unittest.TestSuite()
        for module_name in SUITES[suite_name]:
            suite.addTests(loader.loadTestsFromName(module_name))

    result = unittest.TextTestRunner(verbosity=1 if suite_name in ("hard", "full") else 2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
