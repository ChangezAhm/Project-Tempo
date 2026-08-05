<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Parser: rule freeze + Fill-Plan architecture (2026-08-05)

The populate pipeline is migrating to the Fill-Plan architecture — read
`docs/Fill-Plan-Architecture.md` BEFORE changing anything under `parser/app/`.

**Rule freeze:** do NOT add new global semantic heuristics (label lexicons,
magnitude thresholds, format regexes that decide MEANING) to fix a failing file.
When a fill fails, the fix is one of, in order of preference:
1. a Template-Contract decision (an answered question, scoped to the template);
2. richer planner input or prompt guidance (the LLM owns intent judgments);
3. a new typed verifier error code (deterministic code owns constraints, and a
   constraint failure must surface as a structured, question-able error — never a
   silent blank);
4. a genuinely new FACT reader (dates, formats, formulas — objective data only).
A new global rule requires evidence from multiple templates, and a row in the
rule ledger. Deleting rules is part of every phase's definition of done.

**LLM calls:** every model call goes through `app/llm.py::guarded_stream` (spend
guard + LangSmith tracing + tier/thinking/temperature policy). Never import the
Anthropic SDK anywhere else, never hand-roll the guard.
