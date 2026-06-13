-- Project Tempo — Layer 4b: the Template Contract + corrections.
--
-- The reviewed data model IS the contract. Corrections are TEMPLATE-LEVEL (they
-- span versions): a correction is a content-based match + patch that re-applies
-- to every re-derived data model, so a fix made once sticks across re-uploads.
-- The match is on derived content (metric / sheet / scenario), never the cell,
-- so it survives cells moving between versions. Run AFTER 0005.
--
-- NOTE: the as-of date is deliberately NOT here — it is a population-time input
-- (a future "population run" entity), not a property of the template.

-- One contract per template (spans versions).
create table if not exists public.template_contract (
  id                   uuid primary key default gen_random_uuid(),
  template_id          uuid not null unique references public.templates(id) on delete cascade,
  status               text not null default 'draft',     -- draft | approved
  approved_version_id  uuid references public.template_versions(id) on delete set null,
  notes                text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

-- Template-level overrides: match (content) + patch (fields to override).
create table if not exists public.template_corrections (
  id           uuid primary key default gen_random_uuid(),
  template_id  uuid not null references public.templates(id) on delete cascade,
  target       text not null default 'fact',          -- advisory label: fact | metric | sheet | dimension
  match        jsonb not null default '{}'::jsonb,     -- {canonical_metric, sheet_name, scenario, fact_key, ...}
  patch        jsonb not null default '{}'::jsonb,     -- {basis, scenario, category, canonical_metric, unit, ...}
  note         text,
  created_by   text,
  superseded   boolean not null default false,
  created_at   timestamptz not null default now()
);

-- Data points gain a category so a correction can mark a slot as a config/control
-- input (or exclude it) rather than a reporting data point.
alter table public.template_data_points
  add column if not exists category text not null default 'data';   -- data | config | exclude

create index if not exists tc_template_idx   on public.template_contract (template_id);
create index if not exists tcorr_template_idx on public.template_corrections (template_id) where not superseded;

alter table public.template_contract     enable row level security;
alter table public.template_corrections  enable row level security;

drop policy if exists tempo_mvp_all_contract    on public.template_contract;
drop policy if exists tempo_mvp_all_corrections  on public.template_corrections;

create policy tempo_mvp_all_contract    on public.template_contract    for all using (true) with check (true);
create policy tempo_mvp_all_corrections on public.template_corrections for all using (true) with check (true);
