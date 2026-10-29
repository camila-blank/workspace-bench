# 0023: Claude Opus 5.5 is the default judge

Camila, 2026-10-29: "the default should be opus 5.5" — flip the default and re-measure the
floors. This lifts her 2026-10-15 "let's not change the judge in the public version for now"
(commit `0f5c626`, which reverted the 2026-10-11 switch `ddd88ac` + the Opus floor refreeze
`37e307a`). Every recent run of record (V4 port, summary mode, ATI v2 panel judging) already
used `judge_model=claude-opus-5-5` as an override; this makes that the instrument of record.

## Scope

1. **Default.** `judge_config.DEFAULT_JUDGE = "claude-opus-5-5"`. Retained pins, unchanged:
   agentic_misalignment and jailbreak_recognition stay on `claude-sonnet-5` (Camila
   2026-10-29: keep the Sonnet pins; Opus is blocked at agentic Stage B, see plan 0012);
   introspection stays pinned to `claude-opus-5-5` with `{"anthropic_effort": "default"}`
   (now equal to the default model, still an explicit pin); the regex families
   (`*_mt`, multihop_mt_v2, multilingual_multihop/typo, directed_modulation_obscure) stay
   regex-scored with `REGEX_JUDGE`, and `opts=judge=mc` on them now resolves to Opus.
   `hallucination` and `jlens_concept_pr` name `model=DEFAULT_JUDGE` and follow it.
2. **Reasoning is keyed by route, not by "same route as the family model".** Today
   `resolve` applies a family's `reasoning` dict only while the judge stays on the family
   model's route. After the flip, the family model's route becomes Anthropic for every
   default family, so OpenRouter-style dicts (`hallucination` / `jlens_concept_pr`
   `{"effort": "minimal"}`, `knowledge_cutoff` `{"effort": "low"}`) would start riding the
   Anthropic route — ignored by `anthropic_effort` but present in the cache fingerprint, so
   every cached Opus-override call (reasoning `None`) would miss. New rule in `resolve`:
   keep only the keys of `config.reasoning` that belong to the resolved route
   (`anthropic_effort` → Anthropic; every other key → OpenRouter); on OpenRouter an empty
   result becomes `DEFAULT_REASONING` (`{"effort": "minimal"}`); on Anthropic an empty
   result is `None`. Invariants this must keep, each pinned by a test:
   - an Opus run of a default family resolves `reasoning=None` — byte-identical
     fingerprints to the cached `judge_model=claude-opus-5-5` override runs (cache reuse);
   - introspection on Anthropic keeps `{"anthropic_effort": "default"}`;
   - a Gemini/OpenRouter override gets `{"effort": "minimal"}`, and knowledge_cutoff's
     OpenRouter override keeps `{"effort": "low"}` — exactly today's OpenRouter behaviour;
   - agentic / jailbreak (reasoning None) are unchanged on both routes.
   `pinned` stays `model == config.model`, so an Opus run of a default family is now pinned
   and a Gemini run is an override.
3. **Summarizer / aux models** keep falling back to the resolved judge, so token-bag
   summaries move to Opus with the judge (documented; no code change).
4. **Floors** (`evals/baselines/`). `floors()` already drops entries whose `judge_model` is not
   the family's judge — the gate is retained as is.
   - *Restore, no new calls:* the 2026-10-11 Opus entries from `37e307a` whose `instrument`
     still equals the family's current `prompt_version`: prompt-only for association,
     basic_readout, directed_modulation, multihop, multilingual, poetry, typo, buggy_code,
     conjunctive_association, moral_rationale, relational_multihop, user_modeling; lucky
     guessing for conjunctive_association, directed_modulation, moral_rationale,
     multi_concept_directed_modulation, relational_multihop, role_bound_association,
     user_modeling, brew_intermediates. The six multi-token lucky entries keep main's
     `mc-2026-09-16` stamps (they are MC diagnostics never drawn beside regex scores — the
     restamp `0f5c626` undid stays undone). Regex prompt-only entries are unchanged.
   - *Re-measure under Opus:* prompt-only for arithmetic_intermediates, brew_intermediates,
     chain_intermediates, role_bound_association — only if prompt-only summaries at the
     families' CURRENT read sites exist (re-judge them with Opus, freeze). If they do not,
     the Gemini entries stay in the file, are not drawn (judge mismatch), and the README
     says so; generating them is a GPU follow-up, not this PR.
   - Families that never had floors (wrong_bridge, wrong_bridge_true, multihop_mt_v2,
     directed_modulation_obscure, introspection, knowledge_cutoff, hallucination,
     jlens_concept_pr, agentic, jailbreak) are out of scope.
5. **ATI subset** needs no change: staleness is `instrument` + `bank_sha`
   (`text_inversion.py`), and the v2 entries are already stamped `claude-opus-5-5`.
6. **Docs.** README (Quickstart keys: `ANTHROPIC_API_KEY` or `ANTHROPIC_PROFILE` for the
   default; `OPENROUTER_API_KEY` only for OpenRouter overrides and the `capable` / ATI panel
   answerers; §Judges table + bullets; §Baselines), CLAUDE.md judge-layer paragraph,
   AGENTS.md, `evals/baselines/README.md` (lucky variants' judge, the Opus re-measure
   paragraph, which entries are drawn), `docs/summarizer.md`, and every family README's
   judge line. History (plans 0000–0022, plan0012_results) is not rewritten.
7. **Tests.** Literal-Gemini assertions that test *the default* move to `DEFAULT_JUDGE`;
   tests that exercise the OpenRouter route keep an explicit `google/gemini-3.8-flash`.
   New `resolve` tests for every invariant in (2). Pre-existing test edits are limited to
   those two categories.

## Not in scope
Unpinning the Sonnet families; re-judging cached arms; generating prompt-only summaries on
GPU; PR #109.

## Checks
`ruff check .`, `ruff format --check .`, `pytest -q`; `wsbench list` shows Opus for every
default family; a `wsbench report` over a cached Opus run draws the restored floors; a
cache-only re-run (`scripts/cache_only_run.py` or a dry run) of one cached Opus-override
family hits the cache 100% under the new default.
