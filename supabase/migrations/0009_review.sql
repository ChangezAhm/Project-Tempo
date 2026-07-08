-- Project Tempo — 0009: review items — durable, answerable review questions.
--
-- The Layer-3 understanding produces "needs human review" flags (prose) plus
-- impact_chains / data_flow claims the deterministic verifier could NOT
-- confirm (graph_supported = false). Those are great questions nobody can
-- answer today: they live inside a jsonb blob and are wiped by every re-run.
-- This table turns each one into a durable row a human (or the graph
-- verifier, for machine-checkable ones) can resolve. Built by
-- app/review/items.py; machine checks run in app/review/verify.py.
--
-- item_key is a content hash of (source, question): the same question maps to
-- the same row across re-runs, so inserts are add-only and answers survive
-- re-understanding. Rows are NEVER deleted or overwritten by the pipeline.
-- Run AFTER 0008.
--
-- Service-role only: RLS enabled with NO policies (the 0007 convention) — the
-- parser and Next.js server routes use the service-role key, which bypasses
-- RLS; anon/authenticated get nothing.

create table if not exists public.template_review_items (
  id uuid primary key default gen_random_uuid(),
  template_version_id uuid not null references public.template_versions(id) on delete cascade,
  item_key text not null,          -- content hash: same question upserts, answers survive re-runs
  source text not null,            -- understanding | triage | derive | populate
  kind text not null,              -- machine_checkable | judgment | human_assisted | triage_decision
  question text not null,
  why text,
  affected jsonb,                  -- {"sheets":[...], "cells":[...], "metrics":[...]}
  suggested_answer text,
  check_spec jsonb,                -- machine_checkable: what to verify (see app/review/verify.py)
  status text not null default 'open',   -- open | verified | refuted | answered | dismissed
  resolution jsonb,                -- {evidence|answer|reason, resolved_at, resolved_by}
  created_at timestamptz not null default now(),
  unique (template_version_id, item_key)
);

alter table public.template_review_items enable row level security;  -- no policies: service-role only (see 0007)

create index if not exists tri_version_idx on public.template_review_items (template_version_id);
