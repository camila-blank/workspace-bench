# 0071: a frozen logit-lens L60 floor

Issue #352. Decision: Camila, 2026-11-05: "can you also include a logit lens L60 baseline as
well ... and ship this to the repo as an official baseline".

## What it is

The plainest token readout of Qwen3.6-27B's residual stream: the bench's own
`wsbench.produce.methods.LogitLens` (`W_U · final_norm(h)`, the model's own final norm module and
unembedding, no artifact, top-10 tokens) read at **layer 60 only**, at every plan cell of every
family, then judged by each family's own **summary** instrument exactly like a lens's single-layer
row (`layers=60`). A lens's per-family score can be read against it the way it is read against the
prompt-only floor: what does reading the activation with no learned reader already give?

It is a floor beside the existing two (lucky guessing, prompt-only), not a gate: nothing is
excluded or rescaled by it, and it never enters the macro.

## Frozen file: `evals/baselines/logit_lens.json`

- `_source`: `protocol` = `logit-lens-L60-v1`, `model` = `Qwen/Qwen3.6-27B`, `method` =
  `logit_lens`, `layer` = 60, `k` = 10, `reads` = `full`, `sampling` = `tokens10`, the bench SHA
  each family's readouts were planned at (the 27 v1.0.0 families: 0b7cba39; nyt_connections:
  7285f7d8; hinted_cutoff: 675867259 — every `plan_sha` equals current main's), the producer and the
  measurement date.
- one entry per family (29: the 27 core families, nyt_connections, hinted_cutoff;
  jlens_concept_pr excluded as for prompt-only), in the **same schema as a summary prompt-only
  entry** (`rate`, `ci95`, `metric`, `higher_is_better`, `n_items`, `n_items_decided`, `counts`,
  `judge_model`, `instrument`, `layers_judged` = `[60]`, `complete`, `aggregate` = `summary`,
  `summarizer`, `decided`, `digest_unserved`, `written`), plus `ati` = `{rate, n_items}` for the
  ATI families (the bench's own `anti_text_inversion=True` scoring of the same judged cells; a
  cache replay, zero calls).
- No macro is stored: the floor never enters the macro. The README records the page-rule averages
  (full set 0.130, ATI 0.101) for reference.

An entry is frozen only when its judged `prompt_version` is the family's CURRENT summary
instrument (the prompt-only freeze check, reused): the families whose instrument moved after
v1.0.0 (#286: multihop_mt_v2 `xrole-v1`, wrong_intermediate `wbcues-v2`) are re-judged at main
from the same readouts (digests reused from the summary store) before freezing.

## Code

- `src/wsbench/baselines/logit_lens.py` (new): `PROTOCOL`, `LAYER`, `K`, `FROZEN`;
  `freeze(src, ati_src, dst, source)` folds `src/<family>/results.json` (refused unless every run is
  summary mode, `layers_judged == [60]`, sampling `tokens10`, a full run — not `items` / `limit` —
  and on the family's current instrument) and `ati_src/<family>/results.json` (scope
  `anti_text_inversion`, same instrument) into the file; `floors()` returns the entries;
  `floor_applies(entry, config)` = `prompt_only.summary_floor_matches` (same instrument, digest
  settings and judge model), so the floor is drawn only beside a summary row that ran its
  instrument, exactly like the summary prompt-only floor. The entry builder is factored out of
  `prompt_only.freeze` (`entry_of(r, summary)`), behavior unchanged for prompt-only.
- `wsbench freeze kind=logit_lens src=<judged dir> ati_src=<ati dir> source=<run_config.json>`
  (registered banks only; under `$WSB_BANK_ROOT` it is refused with a clear message — adapted
  subjects get their own floor later).
- `wsbench report`: a third floor column `logit lens L60` whenever the frozen file has entries
  (summary rows: the frozen rate where the row ran the entry's instrument — the existing
  `results.summary_floor_applies` path, which the entry schema satisfies — else `—`; native rows:
  `—`, the floor is summary-mode only); `json=True` adds `floors.logit_lens`. The ATI branch keeps
  skipping every floor column, as today. `summary_floor_columns` reads the first two native
  columns by position (`list(native)[:2]`), so the lucky / prompt-only cells are unchanged.

## Reproduction (documented in `evals/baselines/README.md`)

```
uv run wsbench produce family=<f> method=logit_lens layers=60 out=<dir>/<f>.jsonl   # per family
uv run wsbench run all=True readouts_root=<dir> layers=60 out=<judged>
uv run wsbench run all=True readouts_root=<dir> layers=60 anti_text_inversion=True out=<ati>
uv run wsbench freeze kind=logit_lens src=<judged> ati_src=<ati> source=<run_config.json>
```

The committed numbers were produced by the wsb-bp Modal producer (the same `LogitLens.read` math,
batched; cells verified identical to the J-lens arm's L60 cells) and judged with the frozen
v1.0.0 instruments (current main's for the two moved families).

## Docs and version

README "Baselines" paragraph and an `evals/baselines/README.md` section (per-family table). No
version bump: no score, macro or instrument changes (the package stays 1.2.0).

## Tests

New `tests/test_logit_lens_floor.py`: freeze builds the entry schema from synthetic summary
results; refuses a native run, a run judged at another layer set, a subset run, a stale
instrument; `floor_applies` mirrors `summary_floor_matches`; the report draws the column beside a
matching summary row and `—` beside a native row; the committed file loads, has 29 families,
`layers_judged == [60]` everywhere and the `_source` protocol. No pre-existing test assertion
changes (the report-table tests that pin the column count get the new column only if they build
the table from the frozen files; any such update is listed in the PR).

## Checks

`ruff check`, `ruff format --check`, `pytest -q`.
