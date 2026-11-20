"""Run meaningful offline tests and save a concise factual test report."""
import json
import platform
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portable_memory import __version__


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"version": __version__, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "platform": platform.system(), "python": platform.python_version(),
              "tests_run": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "passed": result.wasSuccessful(), "live_saas_tested": False,
              "uses_synthetic_data": True}
    (ROOT / "docs" / "test-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
