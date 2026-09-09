"""Foundation fixes for the poisoned-partial-understanding incident: understanding
reads every data-bearing sheet up to a high cap, NEVER caches a partial (so a
truncated/failed read can't be reused forever), and loudly flags any data-bearing
sheet that didn't reach the catalogue. Plus the tile-render size guard."""

import struct
from types import SimpleNamespace

from app.understanding.sheet_image import _png_size
from app.population.pipeline.understand import (
    _claims_complete, _data_bearing_sheets, _flag_uncatalogued_sheets)
from app.population import source_understanding as SU


def _sheet(name, nnum=12):
    cells = [{"row": 1, "col": 1, "value": name, "address": "A1"}]
    for i in range(nnum):
        cells.append({"row": 2, "col": i + 1, "value": float(i + 1), "address": f"C{i}"})
    return {"name": name, "cells": cells}


def _snap(names):
    return {"sheets": [_sheet(n) for n in names]}


def test_png_size_guards_short_buffer():
    assert _png_size(b"") is None
    assert _png_size(b"tooshort") is None                       # < 24 bytes
    hdr = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 640, 480)
    assert _png_size(hdr) == (640, 480)


def test_data_bearing_and_claims_complete():
    snap = _snap(["A", "B", "C"])
    assert _data_bearing_sheets(snap) == {"A", "B", "C"}
    assert _claims_complete(snap, [{"sheet": "A"}, {"sheet": "B"}, {"sheet": "C"}])
    assert not _claims_complete(snap, [{"sheet": "A"}])          # partial → not complete


def test_understand_source_never_caches_a_truncated_partial(monkeypatch):
    snap = _snap(["A", "B", "C"])                                # 3 data-bearing
    monkeypatch.setattr(SU, "_understand_sheet",
                        lambda s, m, t: {"sheet": s.get("name"), "periods": [], "series": []})
    monkeypatch.setattr(SU, "_render_tiles", lambda p, n: [])
    monkeypatch.setattr(SU, "cached_sheets", lambda ch: None)
    puts = {}
    monkeypatch.setattr(SU.source_cache, "put", lambda k, v: puts.__setitem__(k, v))
    res = SU.understand_source(snap, content_hash="h1", max_sheets=2, source_path=None)
    assert len(res) == 2                                         # only the cap's worth read
    assert "h1" not in puts                                      # partial NOT cached
    assert SU.understanding_gaps("h1")["truncated"] == ["C"]     # gap recorded, surfaceable


def test_understand_source_caches_only_a_complete_read(monkeypatch):
    snap = _snap(["A", "B"])
    monkeypatch.setattr(SU, "_understand_sheet",
                        lambda s, m, t: {"sheet": s.get("name"), "periods": [], "series": []})
    monkeypatch.setattr(SU, "_render_tiles", lambda p, n: [])
    monkeypatch.setattr(SU, "cached_sheets", lambda ch: None)
    puts = {}
    monkeypatch.setattr(SU.source_cache, "put", lambda k, v: puts.__setitem__(k, v))
    res = SU.understand_source(snap, content_hash="h2", max_sheets=5, source_path=None)
    assert len(res) == 2 and "h2" in puts                        # complete → cached
    assert SU.understanding_gaps("h2") == {}                     # no gap


def test_flag_uncatalogued_sheets_is_loud():
    snap = _snap(["A", "B", "C"])
    catalogue = {"x": SimpleNamespace(sheet="A"), "y": SimpleNamespace(sheet="B")}  # C missing
    st = SimpleNamespace(source_snapshot=snap, catalogue=catalogue, routing={})
    _flag_uncatalogued_sheets(st)
    assert st.routing["source_sheets_uncatalogued"] == ["C"]
    assert any("INCOMPLETE SOURCE READ" in w for w in st.routing["stage_warnings"])
