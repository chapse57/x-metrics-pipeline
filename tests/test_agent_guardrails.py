"""The guardrail is tested with deliberately wrong model outputs. These are the
failure modes seen when running LLM classifiers on real account data; each one
must be caught deterministically, without the model's cooperation."""
import json

import pytest

from xmetrics.agent import NICHES, AgentInput, RuleClassifier, classify_with_guardrails, guard, rule_spam

INP = AgentInput("realFatCat1", "ES Emini Price Action & Order flow Trader 9 years",
                 ["Choppy ES session today, cross-product order flow broke down.", "New video on the channel."])


def _ok(**over):
    base = {"niche": "Futures / order flow", "is_spam": False, "confidence": 0.9,
            "evidence": ["Order flow Trader", "cross-product order flow"], "fit_note": "Order-flow ES educator."}
    base.update(over)
    return json.dumps(base)


def test_accepts_well_formed_grounded_answer():
    v = guard(_ok(), INP, rule_says_spam=False)
    assert v.status == "accepted" and v.failed == []


def test_accepts_json_inside_code_fence():
    v = guard("```json\n" + _ok() + "\n```", INP, rule_says_spam=False)
    assert v.status == "accepted"


def test_rejects_invented_category():
    v = guard(_ok(niche="Quant futures educator"), INP, rule_says_spam=False)
    assert v.status == "rejected" and "label_set" in v.failed


def test_rejects_hallucinated_evidence():
    # a plausible-sounding quote that is NOT in the bio or posts
    v = guard(_ok(evidence=["15 years trading NQ futures"]), INP, rule_says_spam=False)
    assert v.status == "rejected" and "evidence" in v.failed


def test_rejects_empty_evidence():
    v = guard(_ok(evidence=[]), INP, rule_says_spam=False)
    assert v.status == "rejected" and "evidence" in v.failed


def test_evidence_matching_is_whitespace_and_case_insensitive_but_verbatim():
    v = guard(_ok(evidence=["order   flow trader"]), INP, rule_says_spam=False)
    assert v.status == "accepted"
    v = guard(_ok(evidence=["order-flow trader"]), INP, rule_says_spam=False)  # paraphrase with a hyphen
    assert v.status == "rejected"


def test_low_confidence_goes_to_review_not_to_client():
    v = guard(_ok(confidence=0.55), INP, rule_says_spam=False)
    assert v.status == "review" and v.failed == ["confidence"]


def test_rule_conflict_on_spam_goes_to_review():
    v = guard(_ok(is_spam=False), INP, rule_says_spam=True)
    assert v.status == "review" and "rule_conflict" in v.failed


@pytest.mark.parametrize("raw", ["", "Sure! Here is my analysis: the account is about futures.",
                                 '{"niche": "Futures / order flow"}', '{"niche": 1, "is_spam": "no", "confidence": "high", "evidence": "x", "fit_note": 1}',
                                 _ok(confidence=1.7)])
def test_rejects_schema_violations(raw):
    v = guard(raw, INP, rule_says_spam=False)
    assert v.status == "rejected" and "schema" in v.failed


def test_rule_spam_patterns():
    assert rule_spam("Join my telegram for VIP signals")
    assert rule_spam("100% guaranteed profits, DM for access")
    assert not rule_spam(INP.corpus())


def test_rule_classifier_output_passes_its_own_guardrail():
    """The offline fallback must satisfy the same contract as the LLM."""
    v = classify_with_guardrails(RuleClassifier(), INP)
    assert v.status in ("accepted", "review")
    assert v.parsed["niche"] in NICHES
    for q in v.parsed["evidence"]:
        assert q.lower() in INP.corpus().lower()


class _FlakyModel:
    """Simulates a model that answers correctly for one account and hallucinates for another."""
    name = "fake"

    def classify(self, inp):
        if inp.handle == "realFatCat1":
            return _ok()
        return _ok(niche="Momentum guru", evidence=["trades SPX every day"])


def test_end_to_end_catches_the_bad_answer_and_keeps_the_good_one():
    good = classify_with_guardrails(_FlakyModel(), INP)
    bad = classify_with_guardrails(_FlakyModel(), AgentInput("someone", "Swing trader. Breakouts.", []))
    assert good.status == "accepted"
    assert bad.status == "rejected" and set(bad.failed) == {"label_set", "evidence"}
