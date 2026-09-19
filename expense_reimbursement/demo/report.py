"""Summarise what the demo run did (writes demo/RUN_REPORT.md). NOT an accuracy
report: there are no expected labels for verdicts, clauses or categories. The only
comparison made is extraction sanity (the amount Stage 1 read vs the total printed on
the receipt we rendered) -- a smoke check for crashes and odd behaviour.

Usage: python demo/report.py
"""

import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select  # noqa: E402

import repository  # noqa: E402
from db import get_sessionmaker  # noqa: E402
from models import Claim, CheckResultRow, Document, Extraction, FraudAssessment, PolicyDecision  # noqa: E402
from scenarios import S  # noqa: E402


def intended_total(s: dict):
    if s["kind"] != "hotel":
        return Decimal(str(s["total"]))
    if s.get("rate") is None:
        return Decimal(str(s["total"]))
    sub = Decimal(str(s["rate"])) * s["nights"] + sum(Decimal(str(a)) for _, a in s["extras"])
    return (sub * (Decimal("1.12") if s.get("currency", "INR") == "INR" else 1)).quantize(Decimal("0.01"))


def table(counter: Counter, top=None) -> str:
    return "\n".join(f"| {k} | {v} |" for k, v in counter.most_common(top)) or "| (none) | 0 |"


def main() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text())
    st = json.loads((HERE / "state.json").read_text())
    out = ["# Demo run report", "",
           "Smoke test / demo script, not an eval: no expected verdicts or clauses exist, and no accuracy number is reported.", ""]
    with get_sessionmaker()() as s:
        files = st["files"]
        docs = {f: s.get(Document, __import__("uuid").UUID(r["document_id"])) for f, r in files.items() if r.get("document_id")}
        out += ["## Stage 1 (extraction)", "",
                f"- receipts rendered: {len(manifest['items'])}; uploaded: {len(docs)}; "
                f"extracted OK: {sum(1 for d in docs.values() if d.status in ('ready','needs_review','confirmed'))}; "
                f"confirmed: {sum(1 for r in files.values() if r.get('confirmed'))}; "
                f"claims submitted: {len(st.get('submitted', {}))} of {len(st['claims'])}", ""]
        status = Counter(d.status for d in docs.values())
        out += ["| document status | n |", "|---|---|", table(status), ""]
        types, cats, methods = Counter(), Counter(), Counter()
        amount_rows, failing_checks = [], Counter()
        by_file = {it["file"]: it for it in manifest["items"]}
        for i, (f, d) in enumerate(docs.items()):
            ext = repository.latest_extraction(s, d.id)
            ai = next((e for e in d.extractions if e.source == "ai"), None)
            if ai is None:
                continue
            types[ai.document_type] += 1
            cats[ai.category or "(none)"] += 1
            methods[ai.category_method or "-"] += 1
            for c in ai.check_results:
                if not c.passed:
                    failing_checks[c.check_name] += 1
            idx = manifest["items"].index(by_file[f])
            sc = S[idx]
            want = intended_total(sc)
            got = ai.amount
            if got is None or abs(got - want) > Decimal("0.01"):
                amount_rows.append((f, sc["vendor"], want, got, ai.document_type))
        out += ["Extracted document types:", "", "| type | n |", "|---|---|", table(types), "",
                "Stage 2 categories assigned:", "", "| category | n |", "|---|---|", table(cats), "",
                "Failing Stage 1 arithmetic checks (AI version):", "", "| check | n |", "|---|---|", table(failing_checks), ""]
        out += ["Extraction sanity -- amount read differs from the total printed on the receipt:", ""]
        out += ([f"- `{f}` {v}: printed {w}, extracted {g} ({t})" for f, v, w, g, t in amount_rows] or ["- none"]) + [""]

        out += ["Oddities / crashes logged by the runner:", ""]
        out += ([f"- {o.get('file') or o.get('claim')}: {o['what']}" for o in st["oddities"]] or ["- none"]) + [""]

        decisions = []
        for cid in st["claims"].values():
            decisions += repository.latest_decision_run(s, __import__("uuid").UUID(cid))
        out += ["## Stage 3 (policy decisions, latest run per claim)", "", f"- units: {len(decisions)} across "
                f"{len({d.claim_id for d in decisions})} claims; model: {sorted({d.model_name for d in decisions})}", ""]
        out += ["Verdict distribution:", "", "| verdict | n |", "|---|---|", table(Counter(d.verdict for d in decisions)), "",
                "Verdict by category:", "", "| category / verdict | n |", "|---|---|",
                table(Counter(f"{d.category_used} / {d.verdict}" for d in decisions)), "",
                "Clauses selected:", "", "| clause | n |", "|---|---|", table(Counter(d.clause_id or "(none)" for d in decisions)), ""]
        miss = Counter(m for d in decisions if d.verdict == "insufficient_information" for m in d.missing_fields)
        out += ["Top blocking missing_fields (insufficient_information units):", "", "| field | n |", "|---|---|", table(miss, 12), "",
                f"Hard failures (system:*): {sum(1 for d in decisions if d.hard_failure_reason)}", ""]
        out += [f"  - {d.clause_id}: {d.hard_failure_reason[:160]}" for d in decisions if d.hard_failure_reason][:10] + [""]

        assessments = {}
        for a in s.scalars(select(FraudAssessment).order_by(FraudAssessment.created_at)):
            assessments[a.claim_id] = a
        if assessments:
            rows = list(assessments.values())
            fired, na, clear = Counter(), Counter(), Counter()
            for a in rows:
                for r in a.rules_fired: fired[r["rule_id"]] += 1
                for r in a.rules_not_applicable: na[r["rule_id"]] += 1
                for r in a.rules_clear: clear[r["rule_id"]] += 1
            import fraud_config
            out += ["## Stage 4 (fraud rules, latest assessment per claim)", "", f"- claims assessed: {len(rows)}", "",
                    "Risk bands:", "", "| band | n |", "|---|---|", table(Counter(a.risk_band for a in rows)), "",
                    "| rule | fired | clear | not_applicable |", "|---|---|---|---|"]
            out += [f"| {rid} | {fired[rid]} | {clear[rid]} | {na[rid]} |" for rid in fraud_config.WEIGHTS]
            out += ["", f"Mean assessable signals: {sum(a.assessable_signal_count for a in rows) / len(rows):.1f} of {rows[0].total_signal_count}",
                    f"Narratives: {dict(Counter(a.narrative_status for a in rows))}", ""]
    (HERE / "RUN_REPORT.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
