-- Project Tempo — Layer 2: deterministic structure (metric rows, periods,
-- input fields, section signals). See docs/Migration-Plan.md Steps 12-15.
--
-- Written by the parser using the SERVICE-ROLE key (bypasses RLS); read by the
-- app + LLM with the publishable key. sheet_name is denormalised on every
-- table so the schema reads naturally for LLM querying. Run AFTER 0002.
--
-- Canonical metric naming / type / value-role are deliberately ABSENT here —
-- those are LLM-assigned in Layer 3. Layer 2 stores structural facts only.

-- ---------------------------------------------------------------------------
-- Metric rows (hierarchical: parent_metric_row_id links child rows by indent)
-- ---------------------------------------------------------------------------
create table if not exists public.template_metric_rows (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null references public.template_versions(id) on delete cascade,
  template_sheet_id    uuid references public.template_sheets(id) on delete cascade,
  sheet_name           text not null,
  row                  int not null,
  label_text           text not null,
  label_cell           text not null,           -- e.g. "B14"
  label_col            int not null,
  indent_level         int not null default 0,
  parent_metric_row_id uuid references public.template_metric_rows(id) on delete set null,
  data_cols            jsonb not null default '[]'::jsonb,
  data_range           text,                    -- e.g. "C14:N14"
  is_formula           boolean not null default false,
  is_bold              boolean not null default false,
  is_strikethrough     boolean not null default false,
  unit                 text,                    -- from number format / label, e.g. "£m", "%", "x"
  number_format        text,
  sample_value         text,
  named_range          text,
  created_at           timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Period columns
-- ---------------------------------------------------------------------------
create table if not exists public.template_periods (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null references public.template_versions(id) on delete cascade,
  template_sheet_id    uuid references public.template_sheets(id) on delete cascade,
  sheet_name           text not null,
  col                  int not null,
  row                  int,
  label                text not null,
  parsed_date          text,                    -- "2026-03", "2026-Q1", "2026"
  period_type          text,                    -- month | quarter | year | ytd | ltm | budget
  status               text                     -- historical | current | future | budget | ytd | ltm | unknown
);

-- ---------------------------------------------------------------------------
-- Input fields (what a user fills in)
-- ---------------------------------------------------------------------------
create table if not exists public.template_fields (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null references public.template_versions(id) on delete cascade,
  metric_row_id        uuid references public.template_metric_rows(id) on delete set null,
  template_sheet_id    uuid references public.template_sheets(id) on delete cascade,
  sheet_name           text not null,
  row                  int not null,
  label_text           text,
  label_cell           text,
  input_columns        jsonb not null default '[]'::jsonb,
  formula_columns      jsonb not null default '[]'::jsonb,
  current_period_col   int,
  current_period_label text,
  is_unlocked          boolean not null default false,
  needs_collection     boolean not null default false,
  has_historical_data  boolean not null default false,
  unit                 text,
  number_format        text,
  sample_value         text,
  named_range          text,
  indent_level         int not null default 0,
  dependent_formulas   jsonb not null default '[]'::jsonb,
  downstream_cells     jsonb not null default '[]'::jsonb,
  input_evidence       jsonb not null default '[]'::jsonb
);

-- ---------------------------------------------------------------------------
-- Section SIGNALS (evidence for the Layer-3 section LLM — NOT final sections)
-- ---------------------------------------------------------------------------
create table if not exists public.template_regions (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null references public.template_versions(id) on delete cascade,
  template_sheet_id    uuid references public.template_sheets(id) on delete cascade,
  sheet_name           text not null,
  cell_range           text not null,
  min_row              int, min_col int, max_row int, max_col int,
  region_type          text,                    -- header_block | data_table | data_block
  cell_count           int default 0,
  formula_count        int default 0,
  input_count          int default 0,
  label_count          int default 0
);

create table if not exists public.template_section_signals (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null references public.template_versions(id) on delete cascade,
  template_sheet_id    uuid references public.template_sheets(id) on delete cascade,
  sheet_name           text not null,
  row                  int not null,
  text                 text not null,
  signal_type          text not null default 'title_candidate'
);

-- Indexes
create index if not exists tmr_version_idx   on public.template_metric_rows (template_version_id);
create index if not exists tmr_sheet_idx     on public.template_metric_rows (template_sheet_id);
create index if not exists tmr_parent_idx    on public.template_metric_rows (parent_metric_row_id);
create index if not exists tper_version_idx  on public.template_periods (template_version_id);
create index if not exists tfld_version_idx  on public.template_fields (template_version_id);
create index if not exists tfld_metric_idx   on public.template_fields (metric_row_id);
create index if not exists treg_version_idx  on public.template_regions (template_version_id);
create index if not exists tsig_version_idx  on public.template_section_signals (template_version_id);

-- RLS (permissive MVP — same caveat as 0001/0002)
alter table public.template_metric_rows     enable row level security;
alter table public.template_periods         enable row level security;
alter table public.template_fields          enable row level security;
alter table public.template_regions         enable row level security;
alter table public.template_section_signals enable row level security;

drop policy if exists tempo_mvp_all_metric_rows on public.template_metric_rows;
drop policy if exists tempo_mvp_all_periods     on public.template_periods;
drop policy if exists tempo_mvp_all_fields      on public.template_fields;
drop policy if exists tempo_mvp_all_regions     on public.template_regions;
drop policy if exists tempo_mvp_all_signals     on public.template_section_signals;

create policy tempo_mvp_all_metric_rows on public.template_metric_rows for all using (true) with check (true);
create policy tempo_mvp_all_periods     on public.template_periods     for all using (true) with check (true);
create policy tempo_mvp_all_fields      on public.template_fields      for all using (true) with check (true);
create policy tempo_mvp_all_regions     on public.template_regions     for all using (true) with check (true);
create policy tempo_mvp_all_signals     on public.template_section_signals for all using (true) with check (true);
