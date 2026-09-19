"""The one Groq call Stage 3 makes, used by scripts/build_policy.py (structure
the policy) and policy_select.py (select a clause per expense).

Reuses Stage 2's Groq client and 429 backoff (categorize._get_client /
_call_groq_with_retry: temperature 0, JSON mode) instead of adding a third
copy. Adds two things those don't have: one retry on Groq's own
"Failed to validate JSON" rejection (same as extract.py), and an on-disk cache
of raw responses so an unchanged request is never paid for twice.

The cache key is a hash of the exact request (model + messages), so a changed
prompt, policy or extraction can never be served a stale answer; the file NAME
also carries a human-readable scope (claim / policy version / prompt version)
so the cache can be inspected and pruned by hand.
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from groq import BadRequestError

import categorize

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = Path(os.environ.get("POLICY_CACHE_DIR") or BASE_DIR / "storage" / "policy_cache")


class PolicyModelUnavailable(RuntimeError):
    """No API key / the provider is down or rate-limited for the day --
    the caller decides whether that is fatal (a build) or per-unit (an
    evaluation, which records it as insufficient_information)."""


class PolicyModelError(RuntimeError):
    """The model answered, but not usably -- Groq's JSON-mode validator
    rejected the generation twice in a row. Per-call, not a reason to stop."""


@dataclass
class LLMCall:
    raw_text: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    latency_ms: int
    cache_hit: bool
    model: str


def request_hash(model: str, messages: list[dict]) -> str:
    blob = json.dumps({"model": model, "messages": messages}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60]


def cache_path(cache_dir: Path, scope: str, model: str, messages: list[dict]) -> Path:
    return Path(cache_dir) / f"{_slug(scope)}__{request_hash(model, messages)[:16]}.json"


def call_json(
    messages: list[dict],
    *,
    model: str,
    scope: str,
    cache_dir: Optional[Path] = None,
    use_cache: bool = True,
) -> LLMCall:
    """One JSON-mode chat completion at temperature 0. `scope` names what
    the call is for (e.g. "<claim>__v1__pv1__<doc>") and only affects the
    cache file name. Raises PolicyModelUnavailable if the model can't be
    reached at all; any other provider error propagates."""
    cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    path = cache_path(cache_dir, scope, model, messages)
    if use_cache and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        return LLMCall(
            raw_text=cached["raw_text"],
            prompt_tokens=cached.get("prompt_tokens"),
            completion_tokens=cached.get("completion_tokens"),
            latency_ms=cached.get("latency_ms", 0),
            cache_hit=True,
            model=model,
        )

    try:
        client = categorize._get_client()
    except categorize.CategorizerUnavailable as exc:
        raise PolicyModelUnavailable(str(exc)) from exc

    started = time.monotonic()
    try:
        response, _seconds = categorize._call_groq_with_retry(client, model, messages)
    except BadRequestError as exc:
        # Groq's own JSON validator occasionally rejects an ordinary
        # generation (see extract._call_groq); one retry of the same input.
        if categorize._groq_error_code(exc) not in categorize._GROQ_JSON_FAILURE_CODES:
            raise
        try:
            response, _seconds = categorize._call_groq_with_retry(client, model, messages)
        except BadRequestError as exc2:
            if categorize._groq_error_code(exc2) not in categorize._GROQ_JSON_FAILURE_CODES:
                raise
            raise PolicyModelError(f"Groq rejected the generation twice ({categorize._groq_error_code(exc2)})") from exc2
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = response.usage
    call = LLMCall(
        raw_text=response.choices[0].message.content or "",
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        latency_ms=latency_ms,
        cache_hit=False,
        model=model,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "raw_text": call.raw_text,
                "prompt_tokens": call.prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "latency_ms": call.latency_ms,
                "model": model,
                "scope": scope,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return call
