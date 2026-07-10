-- 0010: extensible regions gain SLOTS — per-row write policy.
--
-- The old model could only represent "a range of blank rows to append into",
-- which made pre-labelled editable rows (EBITDA adjustment lines, custom-metric
-- placeholders, like-for-like labels, chart-of-accounts rows) structurally
-- undetectable. A slot is one row with an explicit mode:
--   blank          — append target (auto-applied at populate)
--   placeholder    — throwaway label ("Custom KPI 1", "[Specify]") that may be
--                    overwritten, but only via an approved review item and only
--                    while the live cell still equals current_label
--   editable_label — a real-looking label the author marked editable (unlocked/
--                    validated); overwrite only via approval
--
-- kind gains values: adjustment_rows | editable_labels | chart_of_accounts
-- (text column — no DDL needed for the enum).

alter table public.template_extensible_regions
  add column if not exists slots jsonb not null default '[]',
  -- [{"row":31,"mode":"blank|placeholder|editable_label","current_label":null,"evidence":["B31"]}]
  add column if not exists detection_source text not null default 'standalone',
  -- 'standalone' (authoring/regions.py re-detect) | 'understanding' (in-L3 seed)
  add column if not exists section_ref text;
  -- the L3 section hosting the region (e.g. "EBITDA adjustments"), when known
