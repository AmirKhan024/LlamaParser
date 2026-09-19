"""Run the Stage 4 fraud assessment over every demo claim (direct service call).
Env: FRAUD_MODEL (narrative model). Usage: python demo/assess_all.py"""

import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dotenv import load_dotenv

load_dotenv()

import fraud_eval  # noqa: E402
import repository  # noqa: E402
from db import get_sessionmaker  # noqa: E402


def main() -> None:
    st = json.loads((HERE / "state.json").read_text())
    run_id = uuid.uuid4()
    with get_sessionmaker()() as s:
        for key, cid in st["claims"].items():
            claim = repository.get_claim(s, uuid.UUID(cid))
            if claim is None or claim.status != "submitted":
                print(key, "skipped (not submitted)")
                continue
            row = fraud_eval.assess_claim(s, claim, run_id=run_id)
            print(f"{key:18} band={row.risk_band:12} score={row.risk_score:3} "
                  f"assessable={row.assessable_signal_count}/{row.total_signal_count} "
                  f"fired={[r['rule_id'] for r in row.rules_fired]} narrative={row.narrative_status}")


if __name__ == "__main__":
    main()
