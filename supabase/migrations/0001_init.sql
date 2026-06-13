-- Project Tempo — Build Step 1: durable template upload
-- (see docs/Migration-Plan.md §5 "Build Step 1" and §9 schema)
--
-- Hierarchy: templates -> template_versions -> template_files
--   templates        = the user-named sponsor template
--   template_versions = each parse/upload of that template (re-parse safe)
--   template_files    = the raw workbook stored in Storage for a version
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL) for the project in
-- .env.local, or via `supabase db push` if you wire up the CLI.
--
-- NOTE: the MVP has no auth yet. RLS is ENABLED with permissive policies so the
-- app works with the publishable (anon) key. TIGHTEN these once Supabase auth +
-- org scoping land (see docs/Project-Tempo.md and Migration-Plan.md §7/§9).

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table if not exists public.templates (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  sponsor_name text,
  note         text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create table if not exists public.template_versions (
  id             uuid primary key default gen_random_uuid(),
  template_id    uuid not null references public.templates(id) on delete cascade,
  version_number int not null default 1,
  created_at     timestamptz not null default now(),
  unique (template_id, version_number)
);

create table if not exists public.template_files (
  id                  uuid primary key default gen_random_uuid(),
  template_version_id uuid not null references public.template_versions(id) on delete cascade,
  storage_path        text not null,
  original_filename   text not null,
  content_type        text,
  size_bytes          bigint not null,
  sha256              text,
  created_at          timestamptz not null default now()
);

create index if not exists template_versions_template_id_idx
  on public.template_versions (template_id);
create index if not exists template_files_version_id_idx
  on public.template_files (template_version_id);

-- ---------------------------------------------------------------------------
-- Row level security (permissive MVP policies — no auth yet)
-- ---------------------------------------------------------------------------

alter table public.templates         enable row level security;
alter table public.template_versions enable row level security;
alter table public.template_files    enable row level security;

drop policy if exists tempo_mvp_all_templates on public.templates;
drop policy if exists tempo_mvp_all_versions  on public.template_versions;
drop policy if exists tempo_mvp_all_files     on public.template_files;

create policy tempo_mvp_all_templates on public.templates
  for all using (true) with check (true);
create policy tempo_mvp_all_versions on public.template_versions
  for all using (true) with check (true);
create policy tempo_mvp_all_files on public.template_files
  for all using (true) with check (true);

-- keep updated_at fresh on templates
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists templates_set_updated_at on public.templates;
create trigger templates_set_updated_at
  before update on public.templates
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- Storage: private bucket for raw workbooks
-- ---------------------------------------------------------------------------

insert into storage.buckets (id, name, public)
values ('template-files', 'template-files', false)
on conflict (id) do nothing;

drop policy if exists tempo_mvp_storage_all on storage.objects;
create policy tempo_mvp_storage_all on storage.objects
  for all using (bucket_id = 'template-files')
  with check (bucket_id = 'template-files');
