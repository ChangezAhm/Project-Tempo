# Tempo Parser Service

Stateless Excel structural parser for Project Tempo. The Next.js app owns
upload/storage/contract DB; this service is called with a `template_id`,
pulls the stored workbook from Supabase Storage, runs Aspose.Cells
extraction, and writes the structure (`template_sheets`) back to Supabase
using the service-role key.

## Layout

```
app/
  main.py              FastAPI app — POST /parse/{template_id}, GET /health
  config.py            settings + Aspose.Cells license bootstrap
  supabase_client.py   service-role reads/writes (Storage + tables)
  pipeline.py          download → parse_workbook → persist template_sheets
  labels.py            derive row labels / column headers (Build Step 3)
  raw_extraction/      VENDORED verbatim from template-compiler-prototype:
    workbook_parser.py   Aspose single-pass parse (cells, formulas, validations,
                         text boxes, named ranges, frozen panes, precedents…)
    cell_analyzer.py     per-cell type/style/role
    formula_mapper.py    precedent → dependency graph
    region_detector.py   contiguous region detection
    column_utils.py      A1 ⇄ index helpers
    schema.py            extraction Pydantic models
```

`raw_extraction/` is copied with import paths rewritten only — see
`docs/Migration-Plan.md` for why it's vendored rather than rewritten.

## Setup

```bash
pip install -e .                  # or: pip install fastapi uvicorn aspose-cells-python pydantic-settings supabase
cp .env.example .env              # fill in SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
uvicorn app.main:app --reload --port 8000
```

## Use

```bash
curl -X POST http://localhost:8000/parse/<template_id>
```

Returns a summary (sheet/cell/formula counts + per-sheet list) and persists
one `template_sheets` row per sheet plus an `analysis_jobs` row.

> No `Aspose.Cells.lic` → eval mode. Reading is unaffected (we never save).
