"""Deterministic metric: how many receipt fields the agent extracted correctly.

Grades the agent's text reply (its ``RECEIPT: {...}`` line) against the ground
truth carried in the eval case's ``reference``. Deterministic and local — no
judge model — because the fields have exact answers. Selected via
``tests/eval/receipts_eval_config.yaml``.

Score is the fraction of the four fields (merchant, amount, currency, date) that
match, in [0.0, 1.0].
"""

from __future__ import annotations

import json
import re
from datetime import datetime

_FIELDS = ("merchant", "amount", "currency", "date")
_RECEIPT_LINE_RE = re.compile(r"RECEIPT:\s*(\{.*?\})", re.IGNORECASE | re.DOTALL)
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d %b %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d/%m/%Y",
)


def _content_text(value) -> str:
    """Pull text out of a str, a Content dict, or a ResponseCandidate dict."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "response" in value:  # ResponseCandidate wrapper
            return _content_text(value["response"])
        parts = value.get("parts") or []
        return " ".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")
        )
    return str(value)


def parse_receipt(text: str) -> dict:
    """Extract merchant/amount/currency/date from an agent reply."""
    empty = dict.fromkeys(_FIELDS)
    if not text:
        return empty
    payload = None
    match = _RECEIPT_LINE_RE.search(text)
    if match:
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        for candidate in re.finditer(r"\{[^{}]+\}", text):
            try:
                obj = json.loads(candidate.group(0))
            except json.JSONDecodeError:
                continue
            if any(k in obj for k in _FIELDS):
                payload = obj
                break
    if not isinstance(payload, dict):
        return empty
    return {
        k: (None if payload.get(k) in (None, "") else str(payload[k])) for k in _FIELDS
    }


def _norm_merchant(v: str | None) -> str:
    if not v:
        return ""
    return re.sub(r"[^a-z0-9]", "", v.lower())


def _norm_currency(v: str | None) -> str:
    if not v:
        return ""
    v = v.strip()
    if v in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[v]
    return v.upper()[:3]


def _norm_amount(v: str | None) -> float | None:
    if not v:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(v))
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _norm_date(v: str | None) -> str:
    if not v:
        return ""
    v = v.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v.lower()


def _field_matches(field: str, expected: str | None, got: str | None) -> bool:
    if field == "amount":
        e, g = _norm_amount(expected), _norm_amount(got)
        return e is not None and g is not None and abs(e - g) < 0.005
    if field == "currency":
        return bool(_norm_currency(expected)) and _norm_currency(
            expected
        ) == _norm_currency(got)
    if field == "date":
        return bool(_norm_date(expected)) and _norm_date(expected) == _norm_date(got)
    e, g = _norm_merchant(expected), _norm_merchant(got)
    return bool(e) and bool(g) and (e == g or e in g or g in e)


def score_fields(expected: dict, got: dict) -> tuple[float, dict]:
    per_field = {f: _field_matches(f, expected.get(f), got.get(f)) for f in _FIELDS}
    score = sum(per_field.values()) / len(_FIELDS)
    return score, per_field


def evaluate(instance) -> dict:
    expected = parse_receipt(_content_text(instance.get("reference")))
    got = parse_receipt(_content_text(instance.get("response")))
    score, per_field = score_fields(expected, got)
    misses = [f for f, ok in per_field.items() if not ok]
    if not any(v is not None for v in got.values()):
        explanation = "No RECEIPT line found in the agent reply."
    elif misses:
        explanation = "Mismatched fields: " + ", ".join(
            f"{f} (expected {expected.get(f)!r}, got {got.get(f)!r})" for f in misses
        )
    else:
        explanation = "All receipt fields matched."
    return {"score": score, "explanation": explanation}
