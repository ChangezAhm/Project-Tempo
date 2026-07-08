-- Project Tempo — 0007: lock down RLS (remove the permissive MVP policies)
--
-- ⚠️ APPLY ONLY once the app reads Supabase exclusively via the Next.js server
-- routes (/api/v1/templates, /api/v1/template/[id], ...) with
-- SUPABASE_SERVICE_ROLE_KEY set in the app's environment. From that point the
-- browser's publishable (anon) key loses ALL direct table + storage access —
-- any client still using it (the pre-fix lib/templates.ts) will break.
--
-- Migrations 0001–0006 created `for all using (true) with check (true)`
-- policies so the anon key could read/write everything. Both the Next.js
-- server routes and the parser service now use the service-role key, which
-- BYPASSES RLS entirely — so no policy is needed for them at all. We therefore
-- simply DROP the permissive policies rather than recreating them
-- `to service_role`: a service_role-scoped policy would never be evaluated
-- (dead code), while zero policies + RLS enabled means anon/authenticated get
-- no access — the cleaner and equivalent form.
--
-- RLS remains ENABLED on every table (from the earlier migrations), so with
-- no policies all non-service-role access is denied. When real auth lands,
-- add scoped policies for `authenticated` here.

-- 0001_init.sql
drop policy if exists tempo_mvp_all_templates on public.templates;
drop policy if exists tempo_mvp_all_versions  on public.template_versions;
drop policy if exists tempo_mvp_all_files     on public.template_files;

-- 0002_structure.sql
drop policy if exists tempo_mvp_all_sheets on public.template_sheets;
drop policy if exists tempo_mvp_all_jobs   on public.analysis_jobs;

-- 0003_structure_l2.sql
drop policy if exists tempo_mvp_all_metric_rows on public.template_metric_rows;
drop policy if exists tempo_mvp_all_periods     on public.template_periods;
drop policy if exists tempo_mvp_all_fields      on public.template_fields;
drop policy if exists tempo_mvp_all_regions     on public.template_regions;
drop policy if exists tempo_mvp_all_signals     on public.template_section_signals;

-- 0004_understanding.sql
drop policy if exists tempo_mvp_all_understanding       on public.template_understanding;
drop policy if exists tempo_mvp_all_sheet_understanding on public.template_sheet_understanding;
drop policy if exists tempo_mvp_all_critical_inputs     on public.template_critical_inputs;

-- 0005_datamodel.sql
drop policy if exists tempo_mvp_all_data_points on public.template_data_points;
drop policy if exists tempo_mvp_all_data_model  on public.template_data_model;

-- 0006_contract.sql
drop policy if exists tempo_mvp_all_contract    on public.template_contract;
drop policy if exists tempo_mvp_all_corrections on public.template_corrections;

-- Storage: the 0001 policy let anyone touch the private template-files bucket.
-- The server routes and parser use the service-role key (bypasses RLS), so
-- the bucket needs no anon policy either.
drop policy if exists tempo_mvp_storage_all on storage.objects;
