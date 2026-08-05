-- 0011: data points gain CATEGORY_SOURCE — who decided the category.
--
-- The authority model (Fill-Plan Phase 3) makes label-lexicon categories
-- overridable PRIORS rather than silent facts: null = factual (formula/
-- connector/role/blank); 'lexicon:<kind>' = a label-lexicon prior (overridable
-- by LLM enrichment and by user corrections); 'llm' / 'user' after an override.

alter table public.template_data_points
  add column if not exists category_source text;
