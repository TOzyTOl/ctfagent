"""Export / import the case library between CTFAGENT installs.

The case library is the platform's memory: distilled experience per solved or
instructive run. Cards never carry flag literals (scrubbed at distill time),
so they are safe to share without sharing the run database — which contains
verified flags and operator chat and should never leave the machine.

    export:  python scripts/export_cases.py --db ctfagent.db --out cases.json
    import:  python scripts/export_cases.py --import cases.json --db other.db

Import mirrors ``bench._import_cases``: every card lands with a synthetic
run_id and challenge_id of -1, because those foreign keys mean nothing outside
the database that produced them. Re-importing the same file appends, by
design — the operator decides, the script does not deduplicate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ctfagent.caselib import strip_flags  # noqa: E402
from ctfagent.db import Store  # noqa: E402
from ctfagent.models import Case  # noqa: E402


def export_cases(db_path: Path, out_path: Path) -> int:
    store = Store(db_path)
    cases = store.list_cases(limit=1000)
    payload = []
    for case in cases:
        item = case.__dict__.copy()
        # Defence in depth: cards are scrubbed at distill time, but the export
        # re-scrubs every text field, so an older card written before the
        # scrubbing rule existed cannot leak through the export.
        for field in ("route", "dead_ends", "open_unknowns"):
            item[field] = [strip_flags(str(x)) for x in (item.get(field) or [])]
        for field in ("derivation", "name"):
            item[field] = strip_flags(str(item.get(field) or ""))
        item.pop("id", None)
        payload.append(item)
    store.close()
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"exported {len(payload)} cards -> {out_path}")
    return len(payload)


def import_cases(in_path: Path, db_path: Path) -> int:
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    store = Store(db_path)
    count = 0
    for index, item in enumerate(payload, start=1):
        case = Case(
            id=0,
            run_id=-index,          # synthetic: the producing run is not here
            challenge_id=-1,        # same, mirrors bench._import_cases
            name=item.get("name", ""),
            category=item.get("category", "unknown"),
            outcome=item.get("outcome", "failed"),
            route=item.get("route", []),
            derivation=item.get("derivation", ""),
            dead_ends=item.get("dead_ends", []),
            open_unknowns=item.get("open_unknowns", []),
            techniques=item.get("techniques", []),
            cost_usd=item.get("cost_usd", 0.0),
            elapsed_s=item.get("elapsed_s", 0),
            help_count=item.get("help_count", 0),
            harm_count=item.get("harm_count", 0),
        )
        store.upsert_case(case)
        count += 1
    store.close()
    print(f"imported {count} cards -> {db_path}")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("ctfagent.db"))
    parser.add_argument("--out", type=Path, help="export target (JSON)")
    parser.add_argument("--import", dest="import_path", type=Path, help="import source (JSON)")
    args = parser.parse_args()
    if args.import_path:
        import_cases(args.import_path, args.db)
    else:
        export_cases(args.db, args.out or Path("cases.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
