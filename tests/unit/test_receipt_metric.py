import importlib.util
import pathlib

_METRIC = (
    pathlib.Path(__file__).resolve().parents[1] / "eval" / "receipt_field_accuracy.py"
)
_spec = importlib.util.spec_from_file_location("receipt_field_accuracy", _METRIC)
metric = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(metric)


def _instance(response_text, expected):
    import json

    return {
        "response": {"role": "model", "parts": [{"text": response_text}]},
        "reference": {
            "response": {"role": "model", "parts": [{"text": json.dumps(expected)}]}
        },
    }


_GT = {
    "merchant": "Joe's Grill",
    "amount": "58.40",
    "currency": "USD",
    "date": "2026-08-20",
}


def test_perfect_match_scores_one():
    reply = 'Saved. RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}'
    res = metric.evaluate(_instance(reply, _GT))
    assert res["score"] == 1.0


def test_one_wrong_field_scores_three_quarters():
    reply = 'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.99","currency":"USD","date":"2026-08-20"}'
    res = metric.evaluate(_instance(reply, _GT))
    assert res["score"] == 0.75
    assert "amount" in res["explanation"]


def test_missing_receipt_line_scores_zero():
    res = metric.evaluate(_instance("I stored the receipt for you.", _GT))
    assert res["score"] == 0.0
    assert "No RECEIPT line" in res["explanation"]


def test_currency_symbol_and_date_format_variance_still_match():
    reply = 'RECEIPT: {"merchant":"JOE\'S GRILL","amount":"$58.40","currency":"$","date":"08/20/2026"}'
    res = metric.evaluate(_instance(reply, _GT))
    assert res["score"] == 1.0


def test_euro_symbol_maps_to_eur():
    gt = {
        "merchant": "Cafe de Flore",
        "amount": "23.80",
        "currency": "EUR",
        "date": "2026-05-02",
    }
    reply = 'RECEIPT: {"merchant":"Cafe de Flore","amount":"23.80","currency":"€","date":"2026-05-02"}'
    res = metric.evaluate(_instance(reply, gt))
    assert res["score"] == 1.0


def test_norm_merchant_folds_accents_before_alnum_strip():
    """Accented Café must match unaccented Cafe after NFKD fold."""
    assert metric._norm_merchant("Café") == metric._norm_merchant("Cafe")
    assert metric._norm_merchant("Café") == "cafe"


def test_plain_string_shapes_are_accepted():
    import json

    inst = {
        "response": 'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}',
        "reference": json.dumps(_GT),
    }
    assert metric.evaluate(inst)["score"] == 1.0
