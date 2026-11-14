"""Intent parser: numbers, budgets, deadlines, product types, and the LLM cross-check."""
import json
from datetime import date

import pytest

from app.intent import _number, parse_budget, parse_deadline, parse_intent, parse_rules, product_type_of
from app.llm import LLMError, extract_json
from app.models import Unit

TODAY = date(2026, 11, 13)                      # a Friday
MONDAY = date(2026, 11, 16)
MIN_CONFIDENCE = 0.8                            # the demo mandates' threshold


class FakeLLM:
    name = "fake"

    def __init__(self, reply: str):
        self.reply = reply

    def complete(self, prompt: str) -> str:
        return self.reply


def llm_reply(**over) -> str:
    base = {"product_type": "milk", "quantity": 1000, "unit": "L", "deadline": "2026-11-16",
            "required_claims": ["FREE_FROM_LACTOSE"], "confidence": 1.0, "ambiguities": []}
    return json.dumps({**base, **over})


# ---------------------------------------------------------------- numbers

@pytest.mark.parametrize("raw,value", [
    ("1000", 1000), ("1 000", 1000), ("1 000", 1000), ("1,000", 1000), ("12,500", 12500),
    ("1,000.5", 1000.5), ("1.000,5", 1000.5), ("1,5", 1.5), ("2.5", 2.5), ("0,25", 0.25),
])
def test_numbers_unambiguous(raw, value):
    assert _number(raw) == (value, None)


def test_dot_thousands_is_read_as_thousands_but_flagged():
    value, note = _number("1.000")
    assert value == 1000 and note and "1,000" in note


@pytest.mark.parametrize("raw", ["1,00,0", "1.2.3", "1,000.000,5"])
def test_malformed_numbers_are_refused(raw):
    with pytest.raises(ValueError):
        _number(raw)


def test_dot_thousands_quantity_goes_to_a_human():
    i = parse_rules("I need 1.000 litres of lactose-free milk by Monday", TODAY)
    assert i.quantity == 1000 and i.confidence < MIN_CONFIDENCE


def test_headline_request_stays_confident():
    i = parse_rules("I need 1,000 litres of lactose-free milk by Monday.", TODAY)
    assert (i.quantity, i.unit, i.deadline, i.confidence) == (1000, Unit.L, MONDAY, 1.0)


# ---------------------------------------------------------------- budget

@pytest.mark.parametrize("text", [
    "Up to 500 litres of milk by Friday",
    "max 500 l of milk by Friday",
    "no more than 2,000 kg butter by Friday",
    "under 300 litres of cream by Friday",
])
def test_quantity_phrases_are_not_budgets(text):
    assert parse_budget(text.lower()) == (None, None)
    assert parse_rules(text, TODAY).max_budget_eur is None


@pytest.mark.parametrize("text,budget", [
    ("budget of 3,000 eur", 3000), ("budget 1500", 1500), ("max €800", 800), ("up to 2 500 euros", 2500),
    ("no more than 900 eur", 900), ("under €1,200.50", 1200.5),
])
def test_budgets_need_the_word_budget_or_a_currency(text, budget):
    assert parse_budget(text.lower()) == (budget, None)


def test_quantity_and_budget_in_one_request():
    i = parse_rules("Up to 500 litres of milk by Friday, at most 700 EUR", TODAY)
    assert i.quantity == 500 and i.max_budget_eur == 700


# ---------------------------------------------------------------- deadline and product

def test_first_weekday_mentioned_wins():
    assert parse_deadline("by Friday, or Monday at the latest", TODAY) == date(2026, 11, 20)
    assert parse_deadline("by Monday, Friday is too early", TODAY) == MONDAY


def test_today_and_tomorrow_are_whole_words():
    assert parse_deadline("deliver tomorrow", TODAY) == date(2026, 11, 14)
    assert parse_deadline("todayish", TODAY) is None


@pytest.mark.parametrize("text,ptype", [
    ("200 litres of oat milk", "oat drink"),
    ("200 litres of lactose-free milk", "milk"),
    ("50 kg of milk-free butter", "butter"),
    ("200 litres of kaurajuoma", "oat drink"),
])
def test_most_specific_product_type_wins(text, ptype):
    assert product_type_of(text) == (ptype, [])


def test_two_products_in_one_request_go_to_a_human():
    i = parse_rules("200 litres of milk and 20 litres of cream by Monday", TODAY)
    assert i.product_terms == ["milk"] and i.confidence < MIN_CONFIDENCE
    assert any("more than one product" in a for a in i.ambiguities)


# ---------------------------------------------------------------- LLM

REQUEST = "I need 1,000 litres of lactose-free milk by Monday."


def test_llm_alone_cannot_auto_approve_what_rules_could_not_read():
    """The rule parser finds no quantity; whatever number the LLM produced is unconfirmed."""
    i = parse_intent("Can you get us some milk for next week?", TODAY, FakeLLM(llm_reply(quantity=500)))
    assert i.confidence < MIN_CONFIDENCE
    assert any("only the language model" in a for a in i.ambiguities)
    assert i.parser == "llm:fake"


def test_llm_and_rules_disagreeing_on_product_goes_to_a_human():
    i = parse_intent(REQUEST, TODAY, FakeLLM(llm_reply(product_type="cream")))
    assert i.confidence < MIN_CONFIDENCE
    assert any("disagree on product_terms" in a for a in i.ambiguities)


def test_cross_check_keeps_the_lower_budget_and_rule_ambiguities():
    text = "I need 1.000 litres of lactose-free milk by Monday, budget 900 eur"
    i = parse_intent(text, TODAY, FakeLLM(llm_reply(max_budget_eur=5000)))
    assert i.max_budget_eur == 900
    assert i.confidence < MIN_CONFIDENCE and any("thousands separator" in a for a in i.ambiguities)


@pytest.mark.parametrize("over", [
    {"quantity": float("nan")}, {"quantity": -5}, {"quantity": None}, {"deadline": "2020-01-01"},
    {"max_budget_eur": -1}, {"unit": "barrels"}, {"product_type": None, "product_terms": []},
])
def test_invalid_llm_output_falls_back_to_rules(over):
    i = parse_intent(REQUEST, TODAY, FakeLLM(llm_reply(**over)))   # json round-trips NaN as a bare NaN
    assert i.parser == "rules" and i.quantity == 1000


def test_nan_confidence_from_llm_is_not_trusted():
    i = parse_intent(REQUEST, TODAY, FakeLLM(llm_reply(confidence=float("nan"))))
    assert i.confidence == 0.0


def test_extract_json_tolerates_chatter_with_braces():
    assert extract_json('Sure! {"a": 1} hope that helps {not json}') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        extract_json("no object {here")


def test_llm_provider_selection(monkeypatch):
    """auto takes the first API key, then a CLI on PATH, else none; codex prefers its CLI; unknown means none."""
    import settings
    from app import llm
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(settings, k, "")
    on_path: set[str] = set()
    monkeypatch.setattr(llm.shutil, "which", lambda exe: f"/bin/{exe}" if exe in on_path else None)
    assert llm.pick_provider("auto") == "none" and llm.get_llm("auto") is None
    assert llm.pick_provider("codex") == "openai"
    on_path.add("codex")
    assert llm.pick_provider("auto") == "codex_cli" and llm.pick_provider("codex") == "codex_cli"
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "g")
    assert llm.pick_provider("auto") == "gemini"
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "a")
    assert llm.get_llm("auto").name == "claude:" + settings.ANTHROPIC_MODEL
    assert llm.pick_provider("Gemini ") == "gemini" and llm.pick_provider("bogus") == "none"
