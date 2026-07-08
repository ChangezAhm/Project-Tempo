"""Offline proof of the AI-source-understanding plumbing: the deterministic parts
(address parsing, catalogue-from-understanding reading cached values, sheet
selection, digest/parse) — everything except the LLM call itself. No API calls.
"""

from datetime import date

from app.population import source_cache
from app.population.catalogue import a1_to_rowcol, catalogue_from_understanding
from app.population.source_understanding import (
    _CACHE_VERSION,
    _MAX_IMAGE_BYTES,
    _build_content,
    _digest,
    _parse,
    cached_sheets,
    estimate_source_understanding_usd,
    select_sheets,
)


def test_a1_to_rowcol():
    assert a1_to_rowcol("A1") == (1, 1)
    assert a1_to_rowcol("E5") == (5, 5)
    assert a1_to_rowcol("AD20") == (20, 30)
    assert a1_to_rowcol("AA1") == (1, 27)
    assert a1_to_rowcol("") is None
    assert a1_to_rowcol("Sheet!A1") is None   # caller strips sheet prefixes first


def _formula(addr, row, col, cached, nf="#,##0"):
    return {"address": addr, "row": row, "col": col, "value": "=DRIVER()",
            "cached_value": cached, "style": {"number_format": nf}}


def _snapshot():
    return {"sheets": [{"name": "PL", "cells": [
        {"address": "B20", "row": 20, "col": 2, "value": "Revenue"},
        _formula("AD20", 20, 30, 12_000_000),
        _formula("AE20", 20, 31, 13_000_000),
        {"address": "B21", "row": 21, "col": 2, "value": "Gross margin"},
        _formula("AD21", 21, 30, 0.81, nf="0.0%"),
        _formula("AE21", 21, 31, 0.82, nf="0.0%"),
    ]}]}


def _understanding():
    return [{
        "sheet": "PL",
        "periods": [
            {"header_cell": "AD11", "date": "2023-01-31", "grain": "month"},
            {"header_cell": "AE11", "date": "2023-02-28", "grain": "month"},
        ],
        "series": [
            {"label_cell": "B20", "label": "Revenue", "canonical_metric": "revenue",
             "unit": "EUR'm", "currency": "EUR", "sign_flip": False},
            {"label_cell": "B21", "label": "Gross margin", "canonical_metric": "gross_margin",
             "unit": "%", "currency": None, "sign_flip": False},
        ],
    }]


def test_catalogue_from_understanding_reads_cached_values():
    cat = catalogue_from_understanding(_snapshot(), _understanding())
    assert set(cat) == {"PL!r20", "PL!r21"}

    rev = cat["PL!r20"]
    assert rev.label == "Revenue" and rev.unit.kind == "money" and rev.unit.currency == "EUR"
    # samples come from the formula cells' CACHED values, not the formula text
    assert rev.sample == [12_000_000, 13_000_000]
    # period columns resolved from the AI's header cells + dates
    assert rev.period_cols == [(30, date(2023, 1, 31), "month"), (31, date(2023, 2, 28), "month")]

    # the percent row is typed percent (from its number format), not money
    assert cat["PL!r21"].unit.kind == "percent"


def test_catalogue_from_understanding_skips_sheet_without_periods():
    su = [{"sheet": "PL", "periods": [], "series": [{"label_cell": "B20", "label": "Revenue"}]}]
    assert catalogue_from_understanding(_snapshot(), su) == {}


def test_select_sheets_ranks_by_numeric_density():
    snap = {"sheets": [
        {"name": "Notes", "cells": [{"row": 1, "col": 1, "value": "hello"}]},
        {"name": "Data", "cells": [_formula(f"A{i}", i, 1, i * 1.0) for i in range(1, 40)]},
    ]}
    picked = select_sheets(snap, min_numeric=12)
    assert [s["name"] for s in picked] == ["Data"]   # Notes has no numeric cells


def test_digest_exposes_addresses_and_cached_values():
    d = _digest(_snapshot()["sheets"][0])
    assert "B20" in d and "Revenue" in d
    assert "12000000" in d            # cached value shown, not the formula text
    assert "=DRIVER()" not in d


def test_parse_handles_fences_and_junk():
    out = _parse('```json\n{"periods":[{"header_cell":"AD11","date":"2023-01-31","grain":"month"}],'
                 '"series":[{"label_cell":"B20","label":"Revenue"}]}\n```')
    assert out.periods[0].header_cell == "AD11" and out.series[0].label == "Revenue"
    assert _parse("no json").periods == []


# --- vision (source sheet images) -------------------------------------------

def test_build_content_text_only_when_no_tiles():
    content, n = _build_content("DIGEST", [])
    assert content == "DIGEST" and n == 0


def test_build_content_images_before_digest_oversize_dropped():
    small = b"\x89PNG-small"
    big = b"\x89PNG" + b"0" * (_MAX_IMAGE_BYTES + 1)
    content, n = _build_content("DIGEST", [("slice 1 of 2", small), ("slice 2 of 2", big)])
    assert n == 1                                             # oversized tile dropped
    types = [b["type"] for b in content]
    assert types.count("image") == 1
    assert content[-1] == {"type": "text", "text": "DIGEST"}  # digest is the last block
    captions = [b["text"] for b in content if b["type"] == "text"]
    assert "slice 1 of 2" in captions                         # surviving tile keeps its caption


def test_cached_sheets_versioning(tmp_path, monkeypatch):
    monkeypatch.setattr(source_cache, "_DIR", tmp_path)
    assert cached_sheets(None) is None
    assert cached_sheets("k") is None                          # miss
    source_cache.put("k", {"sheets": [{"sheet": "PL"}]})       # pre-vision entry (no version)
    assert cached_sheets("k") is None                          # stale → treated as a miss
    source_cache.put("k", {"version": _CACHE_VERSION, "sheets": [{"sheet": "PL"}]})
    assert cached_sheets("k") == [{"sheet": "PL"}]


def test_understand_source_never_caches_partial_results(tmp_path, monkeypatch):
    from app.population import source_understanding as su
    monkeypatch.setattr(source_cache, "_DIR", tmp_path)

    def _sheet(name, n):
        return {"name": name,
                "cells": [{"address": f"C{i}", "row": i, "col": 3, "value": float(i)}
                          for i in range(1, n)]}

    snap = {"sheets": [_sheet("A", 20), _sheet("B", 25)]}

    def fake_understand(sheet, model, tiles=()):
        if sheet["name"] == "A":
            raise ValueError("boom")
        return {"sheet": sheet["name"], "periods": [], "series": []}

    monkeypatch.setattr(su, "_understand_sheet", fake_understand)
    out = su.understand_source(snap, "hash123")
    assert [s["sheet"] for s in out] == ["B"]        # the good sheet still returns
    assert cached_sheets("hash123") is None           # but the partial is NOT cached


def test_estimate_prices_images_when_vision_on(monkeypatch):
    snap = {"sheets": [{"name": "PL", "cells": [
        {"address": f"C{i}", "row": i, "col": 3, "value": float(i)} for i in range(1, 20)
    ]}]}
    monkeypatch.setenv("TEMPO_SOURCE_VISION", "0")
    text_only = estimate_source_understanding_usd(snap)
    monkeypatch.setenv("TEMPO_SOURCE_VISION", "1")
    with_images = estimate_source_understanding_usd(snap)
    assert with_images > text_only > 0
