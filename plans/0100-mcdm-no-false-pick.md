# 0100: multi_concept_directed_modulation, an item passes only with no false pick

Camila, 2026-11-20: "we should require no false pick."

## Why

The judge sees six candidate concepts (one to three are the dictated ones) and may tick
several. Today an item passes when every dictated concept is credited and the binding partner
is not named; a wrongly ticked candidate costs nothing. A lens whose readout sprays many
concepts therefore passes without being precise: about 8% of non-gold candidate slots are
ticked for the trained lenses, and passing items carry picks such as "tightrope" or "walrus"
beside the right concepts. Measured on the cached fast-v2 runs (102 items): s3d-rl600 .422 ->
.314, rl-affw-x4 .441 -> .324, mlnla11-rl600 .333 -> .304, jlens-public .088 -> .088.

## The rule

Summary mode (the default, one judge call per item on its digest): **an item passes iff every
dictated concept is credited AND the judge credited no candidate that is not dictated.** A
false pick is exactly what the row builder already records as `false` / `false_picks`: a
verified pick of a non-gold candidate, whatever its basis (a pick whose quote does not verify
is dropped before that and never counts, as today). A false pick is a decided fail, like the
partner veto today: no unjudged cell can rescue the item. **The partner veto is retained**; it
is now implied by the new rule for crediting bases (a partner pick always lands in `false`),
and the code keeps the explicit partner clause so `extras` that report it do not change.

Native mode (`opts=aggregate=native`, a diagnostic: one call per in-sentence cell, concepts
credited across the union of cells): **unchanged pass rule.** The union of false picks over up
to 132 cells would fail nearly every item for a reason unrelated to the summary rule. Native
rows keep recording `false_picks`, and native results gain `extras.no_false_pick_rate` (the
pass rate the new rule would give) so the two modes can be compared. This is my ruling;
Camila may extend the rule to native.

`extras.content_only_pass_rate` (the plan-0025 content-bound variant) is unchanged.

## Code (`src/wsbench/evals/multi_concept_directed_modulation/`)

- `judge.py`, the per-item epilogue (around the `covered = ...` line): compute
  `false_any = {c for v in mine for c in v["false"]}` (check the real field name on the
  per-cell verdict dict and use it), and in summary mode require `not false_any` in `covered`
  and make `passed` a decided `False` when `partner_content or false_any`. Find how the
  function knows it is in summary mode (the synthetic `digest.SUMMARY_LAYER` cell or an
  explicit flag) and branch on that, not on the number of cells. In native mode the pass is
  today's expression exactly.
- Row schema: no field removed or renamed. Add `false_pick_fail: bool` to an item's row when
  the false pick alone turned a would-be pass into a fail (diagnostic), and
  `extras.n_false_pick_fails`, `extras.no_false_pick_rate` (both modes).
- `prompts.py`: `PROMPT_VERSION` gains the stamp-only suffix `+nofalse-a2` (comment in the
  style of the existing `+nocap-a1` line). **`JUDGE_PROMPT_VERSION` does not change and no
  prompt text changes, so every cached judge call stays valid** (the cache key is
  `JUDGE_PROMPT_VERSION`; verify at the `Call` construction sites and say so in the report).
- `CHANCE_LABEL`: the per-call uniform expression it quotes is the old rule's; restate it for
  the new rule (uniform picks of exactly the gold set) or drop the formula and keep "the
  measured floors are the lucky-guessing baseline".
- `src/wsbench/fast.py` is untouched: `multi_concept_directed_modulation` takes the generic
  path over row `pass`, which follows the family's verdict. Verify with the fast rescore guard
  test (the family value recomputed from units must equal the family value).
- `baselines/lucky_guessing.py`: the family's guesser score follows the judge's rule. Today
  `score_draw` returns `correct` (golds ⊆ picks, no partner) and `exact` (picks == golds), both
  already cached per draw. The headline floor fields for this family switch to the `exact`
  rule; find where `correct` is selected for this family and switch it there, keeping
  `score_draw` returning both.

## Frozen artefacts (re-derive from caches; no judge call should be needed)

The implementer does the code and tests; the entries below are re-frozen by the **main
session** from real cached runs after the code is in. The implementer must NOT hand-edit
`evals/baselines/*.json`, and must report every test that fails only because a committed entry
carries the old stamp or the old rule's value:

- `evals/baselines/logit_lens.json` entry (rows at
  `/workspace/root/wsb-bp/qmain_v2/judged/logitlens-L63/L63/multi_concept_directed_modulation/`,
  value expected unchanged at .149): re-judge from the L63 readouts into a copy of that cache
  (all calls replay), then `wsbench freeze kind=logit_lens partial=True`.
- `evals/baselines/prompt_only_summary.json` entry (.965 under the old rule): re-judge the
  prompt-only readouts from their cache, `wsbench freeze` with `source=`.
- `evals/baselines/lucky_guessing.json` (three guesser entries): recomputed from the cached
  draws under the `exact` rule (blind .056, described .144, uniform .007 expected).
- The MC leakage standard: under the exact rule the analytic uniform chance is far below the
  old .272, and the described guesser (.144) exceeds chance + 0.05. The family therefore joins
  the families that fail the described standard; `evals/baselines/README.md`'s table and the
  family README say so. `tests/test_dm_blind_judge.py::test_mcdm_floors_live_after_the_restamp`
  asserts the old standard: its update is sanctioned (new stamp; the standard assertion becomes
  the recorded "fails described" state, with the measured values).
- Not affected: ATI (the family is not gated), `evals/fast/*.json` (ids only), HB, the 16
  adapted sets' floors (already on an older stamp).

## Docs

`evals/multi_concept_directed_modulation/README.md` (headline rule, the "others are false
picks" paragraph, floors section, a dated note: numbers before 2026-11-20 are on the old rule;
old -> new for the four lenses above), top-level README "Judged by" line and Judges row stamp,
`CLAUDE.md` if it restates the rule.

## Tests

- New: a summary-mode item with all golds credited and one verified non-gold pick fails
  (`pass is False`, `false_pick_fail` true); the same item without the false pick passes; an
  unverified-quote wrong pick does not fail the item; a relation item's partner veto behaves
  as before; native mode's verdict on the same cells is unchanged and
  `extras.no_false_pick_rate` reports the stricter rate; the cache key of a judge call is
  unchanged by this plan (fingerprint equality before / after for a fixed call).
- **Sanctioned edits to pre-existing tests, and nothing else:** assertions that pin the
  literal `PROMPT_VERSION` stamp; summary-mode cases in `tests/test_mcdm_v2.py`,
  `tests/test_multi_concept_directed_modulation.py`, `tests/test_summary_mode_dm.py` whose
  fixture has a false pick and asserted a pass under the old rule (each such case is updated to
  the new expected verdict and listed in the report with the false pick named); the leakage
  test named above. A test that fails for any other reason is reported, not edited.

## File tree

```
src/wsbench/evals/multi_concept_directed_modulation/{judge,prompts}.py
src/wsbench/baselines/lucky_guessing.py           exact rule for this family's floors
evals/multi_concept_directed_modulation/README.md
evals/baselines/{logit_lens,prompt_only_summary,lucky_guessing}.json, README.md  (main session)
README.md, CLAUDE.md
tests/test_mcdm_no_false_pick.py                  new
plans/0100-mcdm-no-false-pick.md
```

Gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`.
