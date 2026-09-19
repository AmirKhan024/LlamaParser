"""Re-run the policy check over historical claims under a (new) policy and/or
prompt version. Decisions are append-only: every run writes NEW rows with a
higher evaluation_seq, and the older runs (and any reviewer labels on them)
are untouched -- so two versions can be compared row for row.

Idempotent per (claim, policy_version, prompt_version): a claim already
evaluated under exactly that pair is skipped unless --force.

Usage:
  python scripts/reevaluate_claims.py --policy-version v2                   # every claim
  python scripts/reevaluate_claims.py --prompt-version v2 --claim <uuid>    # one claim
  python scripts/reevaluate_claims.py --policy-version v2 --no-cache        # ask the model again
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)   # same as the eval scripts: .env beats a stale machine-level key

import policy_eval
import repository
from db import get_sessionmaker
from models import Claim
from sqlalchemy import select
from sqlalchemy.orm import selectinload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy-version")
    ap.add_argument("--prompt-version")
    ap.add_argument("--claim", help="only this claim id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached raw model responses")
    args = ap.parse_args()

    session = get_sessionmaker()()
    try:
        query = select(Claim).options(selectinload(Claim.documents), selectinload(Claim.employee))
        if args.claim:
            query = query.where(Claim.id == args.claim)
        done = skipped = failed = 0
        for claim in session.scalars(query).all():
            session.refresh(claim)
            claim = repository.get_claim(session, claim.id)
            try:
                result = policy_eval.evaluate_claim(
                    session, claim, claim.employee, actor_id=claim.employee_id, policy_version=args.policy_version,
                    prompt_version=args.prompt_version, force=args.force, use_cache=not args.no_cache,
                )
            except policy_eval.PolicyModelDown as exc:
                print(f"STOP: the model is unavailable ({exc}); {done} claim(s) done so far.", file=sys.stderr)
                return 2
            except Exception as exc:  # noqa: BLE001 -- one bad claim must not abort the batch
                failed += 1
                print(f"{claim.id}: FAILED {exc}", file=sys.stderr)
                continue
            if result.reused:
                skipped += 1
            else:
                done += 1
                print(f"{claim.id}: seq {result.evaluation_seq}, {len(result.decisions)} decision(s)")
        print(f"evaluated {done}, already up to date {skipped}, failed {failed}")
        return 0 if not failed else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
