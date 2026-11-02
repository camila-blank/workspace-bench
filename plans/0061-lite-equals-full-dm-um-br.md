# 0061: `reads=lite` reads the full positions for the directed-modulation families, user_modeling and basic_readout

## Decision

Camila (2026-11-02): "for ALL directed modulation eval families lite should be the same positions
as normal. user modeling should also be the same, and basic readout."

The families, and the lite rule each has today:

| family | full positions (unchanged) | lite today | lite after |
|---|---|---|---|
| `directed_modulation` | every token of the teacher-forced carrier | `final_token` (the carrier's closing ".") | `same` |
| `model_specific_directed_modulation` (aliases `dm_category_default`, `directed_modulation_obscure`; its `nothink` / `alice` control rows, built from `readplan.plan(NAME)`) | every carrier token | `final_token` | `same` |
| `multi_concept_directed_modulation` | `last_n` = every token of the prefilled target sentence | `offset_from_end k=6` (the `' to'`) | `same` |
| `user_modeling` | `all` = every token of the chat_context render | `final_token` | `same` |
| `basic_readout` (every bank: the default `items_v2.json` rows `question_through_end`, and any bank whose rows take the `final_prompt_token` / `chat` paths) | unchanged per row | the bank row's `readout.lite`, else `final_token` | `same` |

`basic_readout_mt` is NOT in scope (it is a separate family; not named in the decision).

`same` = `reads=lite` selects exactly the `reads=full` cells, and `lite_layers = None` (every full
layer), as association (r2) and conjunctive_association (r3, #222) already do. A lite run then
differs from full only in its `+reads-lite` stamp.

## Changes

1. `src/wsbench/readplan.py`
   - New module constant `LITE_SAME_FAMILIES = ("basic_readout", "directed_modulation",
     "model_specific_directed_modulation", "multi_concept_directed_modulation", "user_modeling")`
     with a comment citing this plan and the decision. `SAME = {"kind": "same"}`.
   - `directed_modulation`, `model_specific_directed_modulation`,
     `multi_concept_directed_modulation`, `user_modeling` rows: `lite_positions=dict(SAME)`;
     `lite_layers` stays `None`. Their `positions`, `layers`, render, text/prefill/assistant/
     messages and `note` content describing the full read are unchanged; only the `note`'s lite
     clause is reworded ("lite = the same positions (plan 0061)").
   - `_final_token_families_items`: for `family in LITE_SAME_FAMILIES` every row's
     `lite_positions` is `dict(SAME)` on **all three** paths (`question_through_end`,
     `chat` prefill, plain), overriding the bank row's `readout.lite`. The other families on this
     path (`typo`, `wrong_bridge`) keep their current lite (`FINAL`, or the bank's `lite` for
     `wrong_bridge`'s `all_positions` rows) — **retained invariant**.
   - The bank files are NOT edited (their `readout.lite` keys stay as the historical record;
     the plan row is what the judge reads, never the bank).
   - **Retained invariant:** `plan_sha` is unchanged for every family (`reads.py` never hashes the
     lite site), so every existing full readouts file, its sidecar and the frozen FINAL64 inputs
     stay valid; no new reads are needed (lite was always a subset of full).
2. `src/wsbench/evals/multi_concept_directed_modulation/judge.py` `_windows`: `reads=lite` now
   takes the **full** path (write-cell region labels from the sentence window tokens, AND the
   write-compliance gate), exactly as `reads=full`. The `' to'`-cell check and its exit-2 are
   deleted, and `LITE_TOKEN` with them if nothing else uses it. The `prompt_only:` reader branch
   (one cell per item, every cell in-sentence, no compliance gate) is **retained unchanged**.
   Docstring updated.
3. No other judge branches on `reads.mode == "lite"` for these families (checked: DM, MSDM, UM,
   basic_readout judges); digest / summary-mode code is untouched. The `+reads-lite` stamp
   (`reads.stamp`) is **retained**: lite results stay distinguishable from full.
4. `evals/baselines/lucky_guessing.json`: no change (none of these families has lite-specific
   floors; only conjunctive_association does).
5. Tests — the ONLY sanctioned edits to pre-existing tests (each asserts the old lite rule this
   plan replaces; the edit must assert the new rule, not delete the check):
   - `tests/test_directed_modulation.py::test_dm_lite_judges_final_carrier_token` → rename to
     `test_dm_lite_judges_full_carrier`; assert `spec.lite_positions == {"kind": "same"}`,
     `lite_layers is None`, the lite run judges the same cells as full, stamp ends `+reads-lite`.
   - `tests/test_msdm_plan0034.py::test_dmo_lite_final_token_and_full_span` → assert `same`
     (rename to `test_dmo_lite_same_as_full_span`); its full-span assertions are kept.
   - `tests/test_mcdm_v2.py::test_readplan_mcdm_lite_row` → `same`;
     `test_mcdm_lite_skips_compliance_and_reads_to` → becomes
     `test_mcdm_lite_matches_full` (a lite run's regions, compliance and value equal the full
     run's on the same file; stamp `+reads-lite`). The declared-lite-file-with-non-`' to'`-cell
     exit-2 assertion is removed with the check it tested.
   - `tests/test_user_modeling.py::test_lite_keeps_only_final_position` → rename
     `test_lite_keeps_all_positions` (lite judges every cell full does; stamp `+reads-lite`);
     line ~465 assertion → `{"kind": "same"}` and `lite_layers is None`.
   - `tests/test_basic_readout.py::test_basic_readout_lite_is_final_token` → rename
     `test_basic_readout_lite_is_same`; `rep.lite_rule` records `{"kind": "same"}`. The bank-content
     assertion at line ~192 (`it["readout"] == {... "lite": {"kind": "final_token"}}`) is kept:
     the bank is not edited.
   - `tests/test_readplan.py`: docstrings / assertions that state these families' lite site
     (around lines 311 and 365) updated to `same`; any assertion about `typo` / `wrong_bridge` /
     other families is untouched.
   - New test in `tests/test_readplan.py`: for each of the five families, every plan row has
     `lite_positions == {"kind": "same"}` and `lite_layers is None`, and
     `readplan.plan(f, reads="lite")` selects the same (id, pos, layer) cells as
     `reads="full"`; `typo` / `wrong_bridge` lite rows are unchanged; `plan_sha(f)` equals the
     value computed on `origin/main` before this change (hard-code the pre-change sha per family).
   Any other test that fails is a regression to fix in code, not by editing the test.
6. Docs: the lite clauses for these five families in `README.md`, `CLAUDE.md`,
   `docs/read_sites.md`, `evals/{directed_modulation,model_specific_directed_modulation,
   multi_concept_directed_modulation,user_modeling,basic_readout}/README.md` (and any
   `mcdm.md` / family doc that defines the `' to'` lite site) say "lite = the same positions as
   full (plan 0061, Camila 2026-11-02); lite results judged before this are stale". The MSDM
   README's "lite carries no signal … never a headline" paragraph is replaced by the same note.

## Not changed

Full positions, layers, banks, judges' full-mode behaviour, prompt versions of full runs, any
other family's lite rule, the macro rule (`results.py` still refuses mixed reads modes).

## Verification

`uv run pytest -q` green (baseline main green first); `ruff check` + `ruff format --check` on
touched files; the plan_sha test above.
