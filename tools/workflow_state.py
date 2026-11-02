"""Inspect current SQLite task state or export it as JSON."""

from pathlib import Path
import argparse
from collections import Counter
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "export"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    from core.config import DATA_DIR
    from core.tasks.repository import TaskRepository
    from core.runtime import application_lock

    with application_lock(DATA_DIR / "application.lock"):
        result = TaskRepository(DATA_DIR / "workflows" / "state.sqlite").export()
        if args.action == "status":
            result = {
                "tasks": len(result["tasks"]),
                "statuses": dict(Counter(t["status"] for t in result["tasks"])),
                "unresolved_writes": sum(
                    i["state"] == "unverified" for i in result["action_intents"]
                ),
            }
    if args.output:
        from core.storage import write_json_atomic

        write_json_atomic(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
