"""Explicit engine shutdown precedes Python's executor-thread shutdown hooks."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tests  # noqa: F401 -- isolate data before discovery of core.ui


def main():
    loader = unittest.defaultTestLoader
    suite = (
        loader.loadTestsFromNames(sys.argv[1:])
        if len(sys.argv) > 1
        else loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    )
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        from core.workflow.host import shutdown_host

        shutdown_host()


if __name__ == "__main__":
    raise SystemExit(main())
