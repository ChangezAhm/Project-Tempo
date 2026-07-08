-- Project Tempo — 0008: authoring — extensible regions (Population-Authoring Plan, Part 2A).
--
-- An extensible region is a place the template INVITES the filler to ADD line
-- items: blank repeating rows under a section with the same column shape as the
-- filled rows above, "(specify)"/"Other…" labels, dropdown validations on label
-- cells, a subtotal whose SUM range already spans the blank rows. Detected per
-- version by app/authoring/regions.py (one guarded Sonnet call per data sheet,
-- deterministically verified against the snapshot) and stored here — the
-- authoring half of the Template Contract. `total_row` is a hard guard: the
-- subtotal already sums the range and must NEVER be written. Row range is
-- blank-rows-only capacity; population never inserts rows. Run AFTER 0007.
--
-- Service-role only: RLS enabled with NO policies (the 0007 convention) — the
-- parser and Next.js server routes use the service-role key, which bypasses
-- RLS; anon/authenticated get nothing.

create table if not exists public.template_extensible_regions (
  id uuid primary key default gen_random_uuid(),
  template_version_id uuid not null references public.template_versions(id) on delete cascade,
  sheet_name text not null,
  kind text,                     -- kpi_list | other_adjustments | custom_rows | other
  label_col int not null,        -- 1-based column of the label cells
  value_cols jsonb not null default '[]',   -- [{"col": int, "parsed_date": "YYYY-MM-DD"|null}]
  row_start int not null,        -- first row available for additions
  row_end int not null,          -- last row available (capacity = contiguous empty rows)
  total_row int,                 -- the subtotal row that must NEVER be written
  rules text,                    -- author guidance ("enter one KPI per row", units)
  confidence real,
  evidence jsonb,                -- cell refs the model cited
  created_at timestamptz not null default now()
);

alter table public.template_extensible_regions enable row level security;  -- no policies: service-role only (see 0007)

create index if not exists ter_version_idx on public.template_extensible_regions (template_version_id);
