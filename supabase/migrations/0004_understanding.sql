-- Project Tempo — Layer 3: workbook + per-sheet understanding (the LLM layer)
-- and the distilled "critical input areas" that drive the review UI.
--
-- Written by the parser using the SERVICE-ROLE key (bypasses RLS); read by the
-- app with the publishable key. Rich/nested understanding is stored as JSONB
-- (it's interpretive narrative, not relationally queried — Layer 2 covers that);
-- the headline fields are lifted to columns for easy listing. Run AFTER 0003.

-- ---------------------------------------------------------------------------
-- Workbook-level understanding (one row per version)
-- ---------------------------------------------------------------------------
create table if not exists public.template_understanding (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null unique references public.template_versions(id) on delete cascade,
  archetype            text,
  purpose              text,
  audience             text,
  summary              text,
  input_surface_sheets jsonb not null default '[]'::jsonb,
  review_flags         jsonb not null default '[]'::jsonb,
  understanding        jsonb not null,           -- full WorkbookUnderstanding
  verify               jsonb,                    -- graph-support summary
  usage                jsonb,                    -- token usage
  model                text,
  created_at           timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Per-sheet understanding (one row per analysed sheet) + its snippet image
-- ---------------------------------------------------------------------------
create table if not exists public.template_sheet_understanding (
  id                   uuid primary key default gen_random_uuid(),
  template_version_id  uuid not null references public.template_versions(id) on delete cascade,
  sheet_name           text not null,
  role                 text,
  summary              text,
  understanding        jsonb not null,           -- full SheetUnderstanding
  snippet_path         text,                     -- Storage path in template-snippets
  created_at           timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Critical input areas — distilled, ranked list for the review UI. Each row is
-- something the portfolio company must fill in, with the business-logic
-- interpretation captured for population mapping.
-- ---------------------------------------------------------------------------
create table if not exists public.template_critical_inputs (
  id                    uuid primary key default gen_random_uuid(),
  template_version_id   uuid not null references public.template_versions(id) on delete cascade,
  sheet_name            text not null,
  label                 text not null,
  cells                 jsonb not null default '[]'::jsonb,
  definition            text,
  qualification_criteria text,
  expected_source       text,
  interpretation_source text,                    -- template_stated | model_knowledge | inferred
  unit                  text,
  needs_value           boolean not null default false,
  rank                  int not null default 0,
  snippet_path          text,                    -- the sheet's snippet in template-snippets
  created_at            timestamptz not null default now()
);

-- Indexes
create index if not exists tund_version_idx  on public.template_understanding (template_version_id);
create index if not exists tshu_version_idx  on public.template_sheet_understanding (template_version_id);
create index if not exists tci_version_idx   on public.template_critical_inputs (template_version_id);
create index if not exists tci_rank_idx      on public.template_critical_inputs (template_version_id, rank);

-- RLS (permissive MVP — same caveat as 0001/0002/0003)
alter table public.template_understanding        enable row level security;
alter table public.template_sheet_understanding  enable row level security;
alter table public.template_critical_inputs      enable row level security;

drop policy if exists tempo_mvp_all_understanding       on public.template_understanding;
drop policy if exists tempo_mvp_all_sheet_understanding on public.template_sheet_understanding;
drop policy if exists tempo_mvp_all_critical_inputs     on public.template_critical_inputs;

create policy tempo_mvp_all_understanding       on public.template_understanding       for all using (true) with check (true);
create policy tempo_mvp_all_sheet_understanding on public.template_sheet_understanding for all using (true) with check (true);
create policy tempo_mvp_all_critical_inputs     on public.template_critical_inputs     for all using (true) with check (true);
