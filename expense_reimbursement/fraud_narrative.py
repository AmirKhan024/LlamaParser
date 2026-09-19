"""Stage 4 narrative layer: ONE model call per flagged claim that turns the
fired rules and their evidence into a reviewer-facing paragraph. The model
explains; it does not detect, score or add findings.

The paragraph is accepted only if it is GROUNDED in the rule output:
  * it references only rules that fired (declared ids and any textual mention
    of an unfired rule's id or distinctive wording);
  * every number in it appears in the fired rules' evidence (or is a count of a
    list in that evidence) -- it cannot introduce a figure of its own.
Otherwise the narrative is dropped (status `rejected`, reason kept) and the UI
shows the raw rules. The rules and the score never depend on this module.
"""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import policy_llm
from fraud_rules import RuleResult

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Distinctive wording of each rule: if the narrative uses it while that rule did NOT fire, it is
# talking about something the rules did not find.
RULE_PHRASES: dict[str, list[str]] = {
    "duplicate_same_employee": ["same employee"],
    "duplicate_cross_employee": ["another employee", "different employee", "two employees", "other employee"],
    "threshold_gaming": ["threshold", "just below", "just under"],
    "velocity": ["velocity", "in one week", "per week", "weekly"],
    "round_number": ["round number", "round-number", "round amount"],
    "weekend_business": ["weekend", "saturday", "sunday"],
    "policy_repeat_violation": ["repeat violation", "repeatedly violated", "same clause"],
    "correction_upward": ["edited upward", "upward edit", "increased the amount"],
    "correction_guardrail": ["guardrail"],
}


def load_prompt(version: str) -> tuple[str, str]:
    path = PROMPTS_DIR / f"fraud_narrative_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt file for narrative prompt_version {version!r}: {path}")
    text = path.read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def call_model(messages: list[dict], *, model: str, scope: str, cache_dir=None, use_cache: bool = True) -> policy_llm.LLMCall:
    """Single seam to the model (tests replace it). Temperature 0, JSON mode."""
    return policy_llm.call_json(messages, model=model, scope=scope, cache_dir=cache_dir, use_cache=use_cache)


def build_payload(claim_title: str, employee: str, band: str, fired: list[RuleResult]) -> dict[str, Any]:
    return {
        "claim": {"title": claim_title, "employee": employee, "risk_band": band},
        "fired_rules": [
            {"rule_id": r.rule_id, "description": r.description, "reason": r.reason, "evidence": r.evidence} for r in fired
        ],
    }


def build_messages(prompt_text: str, payload: dict) -> list[dict]:
    return [{"role": "system", "content": prompt_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in _NUM.finditer(text):
        try:
            out.add(Decimal(m.group(0).replace(",", "").rstrip(".")))
        except InvalidOperation:
            pass
    return out


def _evidence_numbers(node: Any) -> set[Decimal]:
    """Every number in the evidence (from strings, dates included), plus the length of every list
    (so "2 receipts" is grounded when the evidence lists two documents)."""
    out: set[Decimal] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out |= _numbers(str(k)) | _evidence_numbers(v)
    elif isinstance(node, list):
        out.add(Decimal(len(node)))
        for v in node:
            out |= _evidence_numbers(v)
    elif node is not None:
        out |= _numbers(str(node))
    return out


def ground(raw_text: str, results: list[RuleResult]) -> tuple[Optional[str], Optional[str]]:
    """(narrative, rejection_reason). Exactly one is None."""
    fired = {r.rule_id: r for r in results if r.status == "fired"}
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None, "response is not valid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return None, "response has no summary text"
    summary = data["summary"].strip()
    referenced = data.get("rules_referenced")
    if not isinstance(referenced, list) or not referenced:
        return None, "response does not declare rules_referenced"
    bad = [r for r in referenced if r not in fired]
    if bad:
        return None, f"references rule(s) that did not fire: {bad}"
    lowered = summary.lower()
    for rule_id, phrases in RULE_PHRASES.items():
        if rule_id in fired:
            continue
        if rule_id in lowered or any(p in lowered for p in phrases):
            return None, f"mentions the unfired rule {rule_id}"
    allowed = _evidence_numbers([{"reason": r.reason, "evidence": r.evidence} for r in fired.values()])
    invented = sorted(n for n in _numbers(summary) if n not in allowed)
    if invented:
        return None, f"contains number(s) not in the rule evidence: {[str(n) for n in invented]}"
    return summary, None
