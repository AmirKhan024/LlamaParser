"""Load the reviewable seed policy/policy_v1.json into the database
(policy_versions + policy_clauses). The DB is the runtime source of truth;
the JSON is what a human reviewed.

A version label is immutable: seeding a label that already exists is refused
(a changed policy is a new version, e.g. --file policy/policy_v2.json).

Usage: python scripts/seed_policy.py [--file policy/policy_v1.json] [--no-activate]
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import policy as pol
import repository
from db import get_sessionmaker


def seed(session, path: Path, *, activate: bool = True):
    doc = pol.load_policy_json(path)
    meta = doc.get("build_meta", {})
    if not meta.get("human_reviewed"):
        print("WARNING: this policy JSON was not marked human_reviewed; read its review table before relying on it.")
    return repository.create_policy_version(
        session, version=doc["version"], source_sha256=doc["source_sha256"], reference_data=doc["reference_data"],
        build_meta=meta, clauses=doc["clauses"], activate=activate,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=BASE_DIR / "policy" / "policy_v1.json")
    ap.add_argument("--no-activate", action="store_true")
    args = ap.parse_args()
    session = get_sessionmaker()()
    try:
        pv = seed(session, args.file, activate=not args.no_activate)
        print(f"seeded policy {pv.version}: {len(pv.clauses)} clauses, active={pv.is_active}")
        return 0
    except ValueError as exc:
        print(f"not seeded: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
