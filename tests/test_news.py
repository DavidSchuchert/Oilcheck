from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from heizoel import db
from heizoel.config import settings
from heizoel.llm.base import ModelResult
from heizoel.news import digest as digest_mod
from heizoel.news.digest import (
    EMPTY_SITUATION,
    FALLBACK_SITUATION,
    build_item_prompt,
    build_prompt,
    load_digest,
    run_digest,
)
from heizoel.news.feeds import parse_feed, prefilter
from heizoel.news.prompts import ITEM_SCHEMA, SITUATION_SCHEMA

FIX = Path(__file__).parent / "fixtures"
DAY = date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _model_summary_mode(monkeypatch):
    """Die bestehenden Tests prüfen die Modell-Zusammenfassung (Stufe 2)."""
    monkeypatch.setattr(settings, "news_summary_mode", "model")


def test_compose_situation_from_notes():
    from heizoel.news.digest import compose_situation

    rel = [
        {"category": "geopolitik", "note": "Iran-Krieg treibt Ölkonzern-Gewinne"},
        {"category": "co2_gesetz", "note": "Tankrabatt kommt nur teilweise an."},
        {"category": "geopolitik", "note": ""},
    ]
    s = compose_situation(rel)
    assert s.startswith("3 relevante Meldungen in den letzten 24 Stunden, Themen: Geopolitik, Steuern und CO2-Preis.")
    assert "Iran-Krieg treibt Ölkonzern-Gewinne." in s and "Tankrabatt kommt nur teilweise an." in s
    assert compose_situation([{"category": "rohoel", "note": "x"}]).startswith("1 relevante Meldung ")


def test_notes_mode_skips_second_call(tmp_db, feeds, monkeypatch):
    monkeypatch.setattr(settings, "news_summary_mode", "notes")
    model = FakeModel(VERDICTS)
    res = run_digest(date(2026, 10, 16), model=model)
    assert res["n_relevant"] >= 1
    assert model.situation_calls == 0
    row = db.query_one("SELECT summary FROM news_digests")
    assert row["summary"].split(" ")[1:4] == ["relevante", "Meldungen", "in"] or row["summary"].startswith("1 relevante")


def test_parse_rss():
    items = parse_feed((FIX / "rss_sample.xml").read_text(encoding="utf-8"), "Test")
    assert [i["url"] for i in items] == ["https://example.org/a", "https://example.org/b"]
    first = items[0]
    assert first["feed"] == "Test"
    assert first["published"] == "2026-10-16T06:30:00Z"
    assert "<" not in first["summary"] and "Ölpreis" in first["summary"]
    assert "& steigt" in first["summary"]


def test_parse_atom():
    items = parse_feed((FIX / "rss_atom.xml").read_text(encoding="utf-8"), "Atom")
    assert len(items) == 1
    assert items[0]["url"] == "https://example.org/c"
    assert items[0]["published"] == "2026-10-15T12:00:00Z"
    assert items[0]["summary"] == "BEHG: Neue Stufe."


def test_summary_truncated():
    xml = (
        "<rss><channel><item><title>t</title><link>u</link><description>"
        + "x" * 2000
        + "</description></item></channel></rss>"
    )
    assert len(parse_feed(xml, "f")[0]["summary"]) == 600


def _item(url, title="x", summary="", published=None):
    return {"url": url, "feed": "F", "title": title, "published": published, "summary": summary}


def test_prefilter_keyword_dedupe_limit(monkeypatch):
    items = [
        _item("u1", "Brent steigt", published="2026-10-15T00:00:00Z"),
        _item("u1", "Brent steigt", published="2026-10-15T00:00:00Z"),
        _item("u2", "Langweilig"),
        _item("u3", "Etwas", summary="Neue BEHG-Stufe", published="2026-10-16T00:00:00Z"),
        _item("u4", "OPEC tagt", published="2026-10-14T00:00:00Z"),
    ]
    out = prefilter(items)
    assert [i["url"] for i in out] == ["u3", "u1", "u4"]
    monkeypatch.setattr(settings, "news_max_items", 2)
    assert [i["url"] for i in prefilter(items)] == ["u3", "u1"]


class FakeModel:
    """Antwortet je nach Schema: Stufe 1 (Einzelmeldung) oder Stufe 2 (Lagebild)."""

    name = "fake-model"

    def __init__(self, verdicts=None, situation="Ruhige Lage. Zweiter Satz.", fail_on=()):
        # verdicts: Titel -> Dict, der die Standardantwort überschreibt
        self.verdicts = verdicts or {}
        self.situation = situation
        self.fail_on = fail_on
        self.item_calls = 0
        self.situation_calls = 0
        self.situation_prompt = None

    @property
    def calls(self):
        return self.item_calls + self.situation_calls

    def complete(self, system, prompt, json_schema):
        from heizoel.llm.base import ModelError

        if "situation" in json_schema["properties"]:
            self.situation_calls += 1
            self.situation_prompt = prompt
            data = {"situation": self.situation}
        else:
            self.item_calls += 1
            title = prompt.splitlines()[0].removeprefix("Titel: ")
            if title in self.fail_on:
                raise ModelError("kaputt")
            data = {"note": "Einordnung.", "relevant": False, "category": "sonstiges",
                    "rule_change": False, "quote": ""}  # fmt: skip
            data.update(self.verdicts.get(title, {}))
        return ModelResult(data, 100, 20, 5, self.name)


ITEMS = [
    _item("https://x.org/1", "OPEC Treffen", "Fördermenge", "2026-10-16T05:00:00Z"),
    _item("https://x.org/2", "CO2-Preis Debatte", "BEHG", "2026-10-16T04:00:00Z"),
]

VERDICTS = {
    "OPEC Treffen": {"relevant": True, "category": "rohoel", "note": "OPEC tagt."},
    "CO2-Preis Debatte": {
        "relevant": True, "category": "co2_gesetz", "rule_change": True, "quote": "BEHG",
    },
}  # fmt: skip


@pytest.fixture()
def feeds(monkeypatch):
    monkeypatch.setattr(digest_mod, "fetch_all", lambda client=None: list(ITEMS))


def test_schemas_accept_valid():
    from heizoel.llm.base import ModelError, validate_against_schema

    ok = {"note": "n", "relevant": True, "category": "rohoel", "rule_change": False, "quote": ""}
    validate_against_schema(ok, ITEM_SCHEMA)
    validate_against_schema({"situation": "s"}, SITUATION_SCHEMA)
    with pytest.raises(ModelError):
        validate_against_schema({**ok, "category": "unsinn"}, ITEM_SCHEMA)
    with pytest.raises(ModelError):
        validate_against_schema({"relevant": True}, ITEM_SCHEMA)


def test_build_prompts():
    p = build_item_prompt(ITEMS[0])
    assert p.startswith("Titel: OPEC Treffen") and "Fördermenge" in p
    q = build_prompt([{"title": "T", "source": "Q", "note": "N"}])
    assert "1. Titel: T" in q and "Quelle: Q" in q and "N" in q


def test_run_digest_saves_and_skips(tmp_db, feeds):
    model = FakeModel(VERDICTS)
    res = run_digest(DAY, model=model)
    assert res["n_items"] == 2 and res["n_relevant"] == 2 and res["n_hints"] == 1
    assert res["n_calls"] == 3 == model.calls and model.situation_calls == 1
    assert res["model"] == "fake-model" and res["tokens_in"] == 300
    assert res["tokens_out"] == 60 and res["duration_ms"] == 15
    assert "OPEC tagt." in model.situation_prompt
    loaded = load_digest(DAY)
    assert loaded["situation"].startswith("Ruhige Lage")
    assert loaded["items"][0]["category"] == "rohoel"
    assert loaded["items"][0]["source"] == "F" and loaded["items"][0]["url"] == "https://x.org/1"
    hints = db.query("SELECT * FROM rule_change_hints")
    assert len(hints) == 1 and hints[0]["status"] == "open" and hints[0]["quote"] == "BEHG"
    assert len(db.query("SELECT * FROM news_items")) == 2

    assert run_digest(DAY, model=model) == {"skipped": "exists"}
    assert model.calls == 3

    model2 = FakeModel(VERDICTS, situation="Neues Lagebild.")
    run_digest(DAY, force=True, model=model2)
    assert load_digest(DAY)["situation"] == "Neues Lagebild."
    assert len(db.query("SELECT * FROM rule_change_hints")) == 1  # INSERT OR IGNORE


def test_none_relevant_no_second_call(tmp_db, feeds):
    model = FakeModel()
    res = run_digest(DAY, model=model)
    assert res["n_relevant"] == 0 and res["n_calls"] == 2 and model.situation_calls == 0
    d = load_digest(DAY)
    assert d["situation"] == EMPTY_SITUATION and d["items"] == []


def test_inconsistent_empty_text_rebuilt(tmp_db, feeds):
    model = FakeModel(VERDICTS, situation=EMPTY_SITUATION)
    run_digest(DAY, model=model)
    assert load_digest(DAY)["situation"] == (
        "Relevante Meldungen heute: OPEC Treffen; CO2-Preis Debatte"
    )


def test_rule_change_requires_quote_and_verbatim(tmp_db, feeds):
    v = {
        "OPEC Treffen": {"relevant": True, "rule_change": True, "quote": ""},
        "CO2-Preis Debatte": {"relevant": True, "rule_change": True, "quote": "erfunden"},
    }
    res = run_digest(DAY, model=FakeModel(v))
    assert res["n_hints"] == 1
    assert db.query("SELECT quote FROM rule_change_hints")[0]["quote"] == "CO2-Preis Debatte"


def test_rule_change_ignored_if_not_relevant(tmp_db, feeds):
    v = {"OPEC Treffen": {"relevant": False, "rule_change": True, "quote": "Fördermenge"}}
    assert run_digest(DAY, model=FakeModel(v))["n_hints"] == 0


def test_bad_item_answer_skipped(tmp_db, feeds):
    model = FakeModel(VERDICTS, fail_on=("OPEC Treffen",))
    res = run_digest(DAY, model=model)
    assert res["n_relevant"] == 1 and res["n_hints"] == 1 and res["n_calls"] == 3
    assert [i["title"] for i in load_digest(DAY)["items"]] == ["CO2-Preis Debatte"]


def test_schema_violation_skipped(tmp_db, feeds):
    model = FakeModel({"OPEC Treffen": {"category": "unsinn", "relevant": True}})
    res = run_digest(DAY, model=model)
    assert res["n_relevant"] == 0


def test_note_truncated(tmp_db, feeds):
    run_digest(DAY, model=FakeModel({"OPEC Treffen": {"relevant": True, "note": "x" * 400}}))
    assert len(load_digest(DAY)["items"][0]["note"]) == 160


def test_run_digest_driver_none(tmp_db, feeds, monkeypatch):
    monkeypatch.setattr(settings, "llm_driver", "none")
    assert run_digest(DAY) == {"skipped": "llm_driver=none"}
    assert load_digest(DAY) is None


def test_run_digest_no_items(tmp_db, monkeypatch):
    monkeypatch.setattr(digest_mod, "fetch_all", lambda client=None: [_item("u", "Fussball")])
    model = FakeModel(VERDICTS)
    res = run_digest(DAY, model=model)
    assert model.calls == 0 and res["n_items"] == 0 and res["n_calls"] == 0
    d = load_digest(DAY)
    assert d["situation"] == EMPTY_SITUATION and d["items"] == [] and d["model"] is None


@pytest.mark.parametrize("bad", ["Die Prognose ist klar.", "Der Preis wird steigen.",
                                 "Der Preis WIRD FALLEN.", "Kaufen Sie jetzt."])  # fmt: skip
def test_forbidden_words_replaced(tmp_db, feeds, bad):
    run_digest(DAY, model=FakeModel(VERDICTS, situation=bad))
    assert load_digest(DAY)["situation"] == FALLBACK_SITUATION


def test_situation_error_falls_back_to_titles(tmp_db, feeds):
    from heizoel.llm.base import ModelError

    class SituationBroken(FakeModel):
        def complete(self, system, prompt, json_schema):
            if "situation" in json_schema["properties"]:
                raise ModelError("kaputt")
            return super().complete(system, prompt, json_schema)

    run_digest(DAY, model=SituationBroken(VERDICTS))
    assert load_digest(DAY)["situation"].startswith("Relevante Meldungen heute: OPEC Treffen")
