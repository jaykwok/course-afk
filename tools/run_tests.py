"""Credential-free, shell-independent CI entry point and bounded artifacts."""

from pathlib import Path
import importlib.metadata
import json
import os
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "ci"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    metadata = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version,
        "sha": os.environ.get("GITHUB_SHA"),
        "versions": {p: importlib.metadata.version(p) for p in ("dbos", "playwright")},
    }
    (OUTPUT / "environment.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    # Tests own temporary data roots. No runtime data directories are artifacts.
    with (OUTPUT / "tests.txt").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "tests" / "run_suite.py")],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
    print((OUTPUT / "tests.txt").read_text(encoding="utf-8")[-14000:])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
