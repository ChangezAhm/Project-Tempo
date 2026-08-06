-- 0012: data points gain WRITE_MODE — how population may write the cell.
--
-- The write-semantics authority fix (Fill-Plan authority model): a formula cell
-- the understanding claims as an input is a TYPE-OVER DEFAULT — population
-- overwrites the formula with the sourced value (the template master is never
-- modified; fills happen on a copy). null = plain write; 'type_over' = the cell
-- holds a default formula the fill replaces. Writability is decided by the
-- LLM's claim plus the structural constraint (multi-input formulas are never
-- converted); only USER corrections may re-decide it.

alter table public.template_data_points
  add column if not exists write_mode text;
