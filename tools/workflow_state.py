"""Read-only diagnosis by default; explicit migration, binding and JSON export."""

from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from core.config import DATA_DIR, LINKS_DIR
    from core.tasks.identity import account_id
    from core.tasks.repository import TaskRepository
    from core.tasks.migration import dry_run, migrate, bind_unbound
    from core.runtime import application_lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "dry-run",
            "migrate",
            "status",
            "export",
            "bind-current",
            "repair-check",
            "repair",
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "dry-run":
        result = dry_run(LINKS_DIR)
    else:
        with application_lock(DATA_DIR / "application.lock"):
            repo = TaskRepository(DATA_DIR / "workflows" / "state.sqlite")
            if args.action == "migrate":
                result = migrate(LINKS_DIR, repo)
            elif args.action in {"repair", "repair-check"}:
                from core.tasks.repair import repair_state

                result = repair_state(
                    repo, LINKS_DIR, apply=args.action == "repair", recheck=True
                )
            elif args.action == "bind-current":
                repo.backup(repo.path.parent / "before-binding.sqlite")
                result = {"bound_tasks": bind_unbound(repo, account_id())}
            else:
                result = repo.export()
                if args.action == "status":
                    from collections import Counter

                    result = {
                        "tasks": len(result["tasks"]),
                        "statuses": dict(Counter(t["status"] for t in result["tasks"])),
                        "unbound": sum(
                            t["account"] == "unbound" for t in result["tasks"]
                        ),
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
