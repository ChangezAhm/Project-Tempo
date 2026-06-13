-- Project Tempo — Build Step 2-3: structural extraction (Layer 2 start)
-- (see docs/Migration-Plan.md §9 schema)
--
-- template_sheets : one row per worksheet, with raw-extraction metadata +
--                   derived row labels / column headers (JSON, not every cell).
-- analysis_jobs   : status of a parse run (the parser service writes these).
--
-- Run AFTER 0001_init.sql. Same permissive-MVP RLS caveat applies — these
-- are written by the parser using the SERVICE-ROLE key (which bypasses RLS),
-- and read by the app with the publishable key.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table if not exists public.template_sheets (
  id                  uuid primary key default gen_random_uuid(),
  template_version_id uuid not null references public.template_versions(id) on delete cascade,
  name                text not null,
  index               int not null,
  is_hidden           boolean not null default false,
  is_protected        boolean not null default false,
  tab_color           text,
  used_max_row        int not null default 0,
  used_max_col        int not null default 0,
  frozen_rows         int not null default 0,
  frozen_cols         int not null default 0,
  print_area          text,
  was_truncated       boolean not null default false,
  cell_count          int not null default 0,
  formula_count       int not null default 0,
  row_labels          jsonb not null default '{}'::jsonb,
  column_headers      jsonb not null default '{}'::jsonb,
  created_at          timestamptz not null default now(),
  unique (template_version_id, index)
);

create table if not exists public.analysis_jobs (
  id                  uuid primary key default gen_random_uuid(),
  template_version_id uuid not null references public.template_versions(id) on delete cascade,
  job_type            text not null,
  status              text not null default 'running',  -- running | completed | failed
  started_at          timestamptz,
  completed_at        timestamptz,
  error               text,
  summary             jsonb,
  created_at          timestamptz not null default now()
);

create index if not exists template_sheets_version_id_idx
  on public.template_sheets (template_version_id);
create index if not exists analysis_jobs_version_id_idx
  on public.analysis_jobs (template_version_id);

-- ---------------------------------------------------------------------------
-- Row level security (permissive MVP policies — no auth yet)
-- ---------------------------------------------------------------------------

alter table public.template_sheets enable row level security;
alter table public.analysis_jobs   enable row level security;

drop policy if exists tempo_mvp_all_sheets on public.template_sheets;
drop policy if exists tempo_mvp_all_jobs   on public.analysis_jobs;

create policy tempo_mvp_all_sheets on public.template_sheets
  for all using (true) with check (true);
create policy tempo_mvp_all_jobs on public.analysis_jobs
  for all using (true) with check (true);
