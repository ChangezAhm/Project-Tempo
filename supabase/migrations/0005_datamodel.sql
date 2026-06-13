-- Project Tempo — Layer 4: the dimensional data model.
-- Every template input cell becomes a FACT at coordinates (metric, period,
-- scenario, basis, as-of-date, entity, unit), derived from L2 + L3. This is what
-- population targets and the Template Contract (0006) locks.
--
-- Service-role writes, permissive RLS, cascade FKs, denormalized sheet_name —
-- same conventions as 0003/0004. Run AFTER 0004.
--
-- Uniqueness is (version, sheet, cell): a cell is one fact. fact_key is a
-- content-addressed grouping key (metric|period|scenario|basis|entity) used to
-- re-bind user corrections across re-uploads — it is NOT unique (all cells of a
-- metric/period share it, which is the correct correction granularity).

create table if not exists public.template_data_points (
  id                     uuid primary key default gen_random_uuid(),
  template_version_id    uuid not null references public.template_versions(id) on delete cascade,
  fact_key               text not null,
  sheet_name             text not null,
  cell                   text not null,
  "row"                  int not null,
  col                    int not null,
  metric_row_id          text,                       -- L2 template_metric_rows.id hint (not FK-enforced)
  metric_label           text not null,
  canonical_metric       text,
  period_index           int,                        -- relative ordinal on the timeline (absolute date set at population)
  period_label           text,
  parsed_date            text,
  period_type            text,
  scenario               text not null,              -- actual | budget | forecast | unknown
  basis                  text not null,              -- point_in_time | flow | ytd | trailing | unknown
  entity                 text,
  unit                   text,
  currency               text,
  value_role             text,
  sign_convention        text,
  qualification_criteria text,
  definition             text,
  expected_source        text,
  needs_value            boolean not null default true,
  scenario_source        text,
  basis_source           text,
  confidence             real not null default 0.5,
  applied_correction_ids jsonb not null default '[]'::jsonb,
  created_at             timestamptz not null default now(),
  unique (template_version_id, sheet_name, cell)
);

create table if not exists public.template_data_model (
  id                     uuid primary key default gen_random_uuid(),
  template_version_id    uuid not null unique references public.template_versions(id) on delete cascade,
  archetype              text,
  timeline_relative      boolean not null default false,
  base_currency          text,
  fact_count             int not null default 0,
  scenarios              jsonb not null default '[]'::jsonb,
  period_grains          jsonb not null default '[]'::jsonb,
  entities               jsonb not null default '[]'::jsonb,
  review_flags           jsonb not null default '[]'::jsonb,
  dimensions             jsonb not null,             -- full DetectedDimensions
  created_at             timestamptz not null default now()
);

create index if not exists tdp_version_idx  on public.template_data_points (template_version_id);
create index if not exists tdp_factkey_idx  on public.template_data_points (template_version_id, fact_key);
create index if not exists tdp_sheet_idx    on public.template_data_points (template_version_id, sheet_name);
create index if not exists tdm_version_idx  on public.template_data_model (template_version_id);

alter table public.template_data_points enable row level security;
alter table public.template_data_model  enable row level security;

drop policy if exists tempo_mvp_all_data_points on public.template_data_points;
drop policy if exists tempo_mvp_all_data_model  on public.template_data_model;

create policy tempo_mvp_all_data_points on public.template_data_points for all using (true) with check (true);
create policy tempo_mvp_all_data_model  on public.template_data_model  for all using (true) with check (true);
