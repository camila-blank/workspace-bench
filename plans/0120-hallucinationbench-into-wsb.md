# Plan 0120: HallucinationBench moves into this repository (stage A: the code and banks, unchanged; stages B–E outlined)

Camila, 2026-11-27: HallucinationBench (HB, `camila-blank/hallucination-bench`, `main` @
`ecfae41`, index `hindex-v8`) becomes part of this repository so that it can use the run
infrastructure built here (stored activations, `wsbench read`, the Modal launcher, the
cross-process judge pacers, one judge process per family). A one-time capture of HB's items
into the stored-activation dataset is approved. Changes to `benign_roleplay` wait for the
jailbreak changes she is making here. A fast subset of HB is undecided (§7).

Base: `origin/main` @ 401e7bda. Worktree `/root/tmp/wsb-0120`, branch
`camila/0120-hb-into-wsb`. HB source checkout: `/root/tmp/hb-v8-final` (`ecfae41`). Frozen v8
judge outputs: `/dev/shm/hb-v8-full/v8-full` (volatile; archive `/workspace/hb-v8-full`).

This plan supersedes two decisions of plan 0040: "the hallucination benchmark lives in another
repository" and "the index is ingested as a vendored file". It **retains** these invariants of
plans 0040 and 0080, restated here because the code that encodes them is touched:

- **I1. The HB index never enters any WSB macro, tier or fast subset.** No HB eval is in
  `tiers.CORE`, in `evals/fast/`, in the ATI subset, in the floors, or in `results.macro`. The
  report block stays a separate section with `in_macro: false`.
- **I2. One number per (method, layer)**, `full` where the method has every family, else
  `matched (no sibling)`; joint rows are kept apart.
- **I3. An arm is mapped to an HB method only when its checkpoint is verified identical**
  (`methods.json`), never by alias.
- **I4. Adapted sets carry no hallucination family.** `REMOVED_FAMILIES["hallucination"]`
  stays; the key `hallucination` is not reused.
- **I5 (HB's own rule). A change to any HB prompt, bank, scoring rule or weight is a new index
  version.** Stages A and B change none; the frozen `hindex-v8` numbers must come out the same.

## 0. Why a sub-package and not six ordinary families

A WSB family is one pass rate over items, in summary mode, with floors, ATI, lucky-guessing and
capable tables. An HB family is not: its units are claims (character-weighted leaf shares),
situation cells, refusal lines, probe-validated units and prompts; every method has per-layer
rows and a joint 11-layer row; the index is a weighted macro (Σ 20, `matched` Σ 19) with its
own cluster bootstraps, a hashed manifest and persistent-failure exclusions. Registering the
evals through `EvalSpec` alone would need about a dozen table and count-pin edits each (the
list in plan 0108 §"sites") and a `digest.NATIVE_ONLY` entry each (that set is pinned to
exactly `{jlens_concept_pr}`), for instruments that share none of that machinery.

So: HB's code is a sub-package `wsbench.hb` with its own command `wsbench hb …` (the precedent
is `wsbench viewer …`, dispatched in `cli.main` before `COMMANDS`). What it takes from WSB is
the **run infrastructure**, in stage B: the judge transport and pacers, and the read plan /
capture / store / `read` path. It registers no `EvalSpec` in stage A.

## 1. Stage A (this PR): move the code, banks and tests; no behaviour change

### 1.1 What moves where

| HB (`ecfae41`) | here |
|---|---|
| `src/hallucination_bench/**` (81 files, 30,009 lines) | `src/wsbench/hb/**`, same relative layout |
| `data/<eval>/**` (38 MB) | `evals/hallucinationbench/<eval>/**` (`wsbench_chat`, `wsbench_chat_x3`, `story`, `agentic_misalignment`, `sibling_probe`, `overrefusal`, `benign_roleplay`, `calibration`) |
| `data/index/{v8_manifest.json,v8_persistent_failures.json,methods.json}` | `evals/hallucinationbench/index/` (HB's `methods.json`: index method → arm per family; it is a different file from the existing `evals/hallucinationbench/methods.json`, readouts-root basename → index method, which stays) |
| `tests/**` (64 files, about 649 tests, fixtures 652 KB) | `tests/hb/**` |
| `scripts/**` that the v8 index path needs (`overrefusal/sj_run.py`, `overrefusal/offtopic_export.py`, `sibling_probe/um_ladder.py`, `sibling_probe/um_probes_modal.py`, `main_x3/*`) | `scripts/hb/**` |
| `docs/results/0029_index_v8.md`, `docs/readout_contract.md` if present, HB `README.md`, HB `CLAUDE.md` | `docs/hb/` (`README.md` → `docs/hb/README.md`; HB's `CLAUDE.md` → `src/wsbench/hb/CLAUDE.md`, paths rewritten) |
| `plans/00NN-*.md` (20 files) | `plans/hb/` unchanged (history; HB's numbers collide with WSB's, hence the sub-directory) |
| `hbench` console script | `wsbench hb <args>` = `wsbench.hb.cli.main(args)`; the argparse CLI is unchanged, `prog="wsbench hb"` |

Not moved: `plots/` (output), `outputs/`, `uv.lock`, the other scripts (one-off jobs with
dev-box paths; they stay in the archived HB repository, stage E).

Kept as they are in this repository: `evals/hallucinationbench/{index.json,methods.json,README.md}`,
`src/wsbench/hbindex.py`, `scripts/vendor_hb_index.py`, the viewer's `ingest/hb.py` (its
`hb_repo=` now also accepts this checkout: `BANKS` paths get a second root, `evals/hallucinationbench/`).
The vendored `index.json` remains the index of record for `hindex-v8` (§1.4).

### 1.2 Edits allowed in moved code (everything else is byte-identical after the import rewrite)

1. Imports and module strings: `hallucination_bench` → `wsbench.hb` (also in `python -m`,
   `subprocess` and test strings).
2. Data root: one function `wsbench.hb.paths.data(*parts)` = `<repo>/evals/hallucinationbench/…`
   replaces every `data/…` literal and every `Path(__file__).parents[n] / "data"`.
3. `paths.py`: each `_env_path("HBENCH_X", "/workspace/…")` default becomes
   `wsbench.paths.external("HBENCH_X", "<rel>")` with the same environment variable and the
   same default location under `$WSB_DATA_ROOT` (`tests/test_paths.py::test_no_box_path_literal_in_src`).
   The two `/root/tmp/hb28-readouts*` defaults have no `/workspace` location: they become
   required-when-used (`HBENCH_X3_READOUTS`, `HBENCH_X3_NEW_READOUTS`; exit 2 naming the
   variable), which no test or `final` run reads.
4. `from __future__ import annotations` removed from the 3 files that have it only if the file
   still imports and its tests pass without it; otherwise left and listed in the PR.
5. Nothing else: no reformatting of prompt text, no renames, no lint-driven refactors, no
   docstring trims. **The claim-map `VERSIONS` objects, `taxonomy_v2`, `units.py`, the
   `cverify-v1` builder, the AM detector's canonical lines, `cache.py`'s row format
   (`fingerprint`, not `fp`) and the fingerprint tuple `(prompt version, model, effort, system,
   user)` are untouched**, so every frozen cache row still hits and the hash pins
   (`test_registry_pins.py`, `test_v7_rebuild_identity.py`, `test_continuation_verifier.py`) pass
   unmodified.

`benign_roleplay/wsb.py` keeps running WSB's `jailbreak_recognition` **as a subprocess in a
separate checkout pinned to `0b18789`** (`$HBENCH_WSBENCH_PUB`), exactly as now; it does not
call this checkout's `jb-v4` code. That looks odd inside WSB and is deliberate: v8's
benign-roleplay numbers are `jb-v3`, the family is on hold, and stage C replaces the mechanism.
`test_no_wsb_logic_in_hb` moves with it and keeps its meaning (no import of `wsbench.evals…`
from `wsbench.hb.evals.benign_roleplay`).

### 1.3 Repository rules for the imported tree

- **Ruff.** `ruff format` must already be clean (it is, 81 files). For `ruff check`,
  `pyproject.toml` gets per-file ignores for `src/wsbench/hb/**`, `tests/hb/**` and
  `scripts/hb/**` covering exactly the rule codes that fire there under WSB's selection
  (measured on `src`: N806, N803, N812, N815, RUF001, RUF002, SIM118, SIM102, SIM300, SIM108,
  C416; the implementer measures tests and scripts and adds only codes that fire). No code is
  changed to satisfy a lint rule in this stage.
- **Dependencies.** New extra `hb = ["matplotlib>=3.11.2", "transformers>=5.11", "jinja2>=3.1"]`
  (plots in `final.py`; the sibling tokenizer). `dev` gains nothing; an HB test that needs an
  `hb` dependency, the tokenizer, `$HBENCH_WSBENCH_PUB` or scikit-learn skips without it, as it
  does in HB today. CI installs `.[dev,hb]`.
- **Tests.** `tests/hb/conftest.py` carries HB's `pythonpath = ["tests"]` helper imports
  (rewritten to `tests/hb`). WSB's own tests are not edited, with these exceptions, each
  because the test enumerates the tree: `test_family_paths.py` / `test_readme*.py` /
  `test_prompts_in_readme.py` must not start treating `evals/hallucinationbench/<eval>` or
  `wsbench.hb.evals.*` as WSB families (they are not registered; if a test scans directories
  rather than the registry, it gets `hallucinationbench` in its existing non-family list, as
  `_UNMOVED` has it). **No assertion of a pre-existing WSB test changes value**: no family
  count, tier list, category list, manifest ledger entry or golden file moves (I1, I4).
- **Docs.** `docs/hallucinationbench.md` is rewritten: HB lives in `src/wsbench/hb`, how to run
  `wsbench hb …`, the index stays out of the macro (I1), the index of record for `hindex-v8`
  (§1.4). README: the one existing HallucinationBench sentence is updated (no dates, plan
  numbers or version labels in README prose, `test_readme_style.py`). `CLAUDE.md` and
  `AGENTS.md`: a short "HallucinationBench (`wsbench.hb`)" section pointing at
  `src/wsbench/hb/CLAUDE.md`.
- **Bank protection (plan 0097, PR #510, open).** If #510 has merged before this PR, the eight
  `evals/hallucinationbench/<eval>` directories are pack units and go through `wsbench pack`;
  if not, they are plaintext like every other bank and #510 picks them up. HB's banks are
  already public in the HB repository, so this PR does not change their exposure.

### 1.4 Acceptance gates for stage A

- **G1 (index identity).** With `outputs/v8-full` pointing at the frozen outputs and the
  manifest's input paths restored, `wsbench hb final --manifest
  evals/hallucinationbench/index/v8_manifest.json --methods evals/hallucinationbench/index/methods.json
  --out X --plots Y` writes an `index.json` equal to HB `main`'s output **byte for byte except
  `git_sha` and the manifest path** (HB's existing regression gate; about 9 min, no API). The
  manifest's relative `data/…` input paths are resolved through `paths.data` (§1.2 item 2) and
  their sha256 values are unchanged because the files are moved, not rewritten.
- **G2 (values equal the vendored index).** Every (method, layer, family) value, `full`,
  `matched`, joint row and CI in G1's output equals `evals/hallucinationbench/index.json` (built
  at HB `2563704`) for the methods vendored there. The vendored file is not regenerated;
  `HB_SHA` in `scripts/vendor_hb_index.py` is not changed.
- **G3.** `pytest tests/hb -q`: the same tests pass and the same tests skip as in the HB
  checkout with the same environment (counts recorded in the PR; a test that passed there and
  fails or newly skips here is a blocker).
- **G4.** WSB's suite: `pytest -q` green with no pre-existing assertion changed (§1.3);
  `ruff check .`, `ruff format --check .` green.
- **G5 (cache replay, sample).** `wsbench hb run --bank x3 --map-prompt claimmap-v2.9` on the
  frozen `s3d-rl600` L44 root with the judge client forced offline (a client factory that
  raises) completes from cache and rewrites a `cells.jsonl` with the manifest's sha256. Same
  for one AM per-layer root and the story root. This proves fingerprints survived the move.

### 1.5 Files (new or modified)

```
src/wsbench/hb/                      (new: HB's package, 81 files; + CLAUDE.md)
src/wsbench/cli.py                   (modified: `hb` dispatch before COMMANDS, usage line)
evals/hallucinationbench/<eval>/     (new: 8 bank directories)
evals/hallucinationbench/index/      (new: v8 manifest, persistent failures, HB methods.json)
evals/hallucinationbench/README.md   (modified)
scripts/hb/                          (new)
tests/hb/                            (new: 64 test files, fixtures, conftest.py)
docs/hallucinationbench.md           (modified)   docs/hb/   (new)
plans/hb/                            (new: HB's 20 plans)   plans/0120-…md (this file)
pyproject.toml                       (modified: `hb` extra, per-file ruff ignores)
.github/workflows/ci.yml             (modified: install `.[dev,hb]`)
README.md, CLAUDE.md, AGENTS.md      (modified: pointers)
src/wsbench/viewer/ingest/hb.py      (modified: second bank root)
```

No long-running job: G1 and G5 run in tmux with a timestamped log under `logs/` of the
worktree (`tail -f logs/<file>`); nothing is launched on GPUs in stage A.

## 2. Stage B1 (next PR): judge throughput

Measured on the frozen v8 caches (2026-11-27): one full judge pass over three methods is about
1.34M calls and 2.44B output tokens; main and agentic misalignment are 97% of it; NLA is 65%.
One `hbench` process at its defaults (64 calls in flight, 1,500 requests/min) would need about
14 h (J-lens), 40 h (O-lens), 84 h (NLA). The 2026-11-05 run took 5.5 h with 76 hand-launched
processes and ran into the organisation's output-token rate limit (2–3 retries per call).

- `wsbench.hb.llm.LLM` keeps its interface, its usage accounting, **its cache fingerprint tuple
  and row format (unchanged, I5)**, and its caller-side fallbacks with `meta.answered_by`; its
  transport acquires `wsbench.llm`'s cross-process slot (`WSB_SLOTS`) and the file-backed
  request and input-token pacers (`WSB_PACER`) around each API attempt. Throughput settings are
  in no fingerprint, as in WSB.
- `wsbench hb run-all method=<m> readouts=<root> out=<dir>`: one child process per (eval,
  stage group, layer shard), as `runner.py` does per family, in a run-private temp directory
  with `WSB_SLOTS` 1500; defaults concurrency 256. It replaces `jobs/job.sh`, `amstage.sh`,
  `or_*.sh`. Stages keep their order within an eval; evals and layers run side by side.
- The NLA joint claim map on Sonnet made 76k calls for 36k cells because long answers were cut
  at the output cap and re-asked (31% of all output tokens). Raising that stage's output cap or
  re-asking with a larger cap is **not** a prompt change but can change which answer is cached
  for a cell that previously failed; it is done only for cells with no successful row, and the
  count is reported.
- Gate: replay of the three methods' frozen roots makes zero API calls and reproduces G1; a
  fresh judge pass of one layer of one method (O-lens L44, all evals) is timed and its index row
  compared with the frozen row (judge noise expected; reported, not gated).

Expected after B1 (estimate from the organisation rate limits, not measured): the output-token
limit is the floor, roughly 0.5 h J-lens, 1–1.5 h O-lens, 3 h NLA when nothing else is judging.

## 3. Stage B2 (PR after B1): reads from stored activations

HB's repository has no read side; its readouts came from one-off scripts. HB's banks already
hold what the `captured` render needs (`capture_rows.json`: `input_ids` + `eval_positions`;
`sites[].pos`).

- A read-plan source for HB evals: `wsbench hb plan [evals=] out=` writes `ReadSpec` rows
  (render `captured` or explicit positions, layers `GRID`) under eval names `hb_main`,
  `hb_story`, `hb_agentic`, `hb_overrefusal`, `hb_roleplay`, `hb_sibling`, and `wsbench
  capture plan=<file>` / `wsbench read plan=<file>` take them as they take any cell list. The
  evals are **not** added to `readplan.families()` or the registry (I1, I4), so no family
  count or ledger pin moves.
- Sampling: HB's per-layer unit reads k = 3 prose samples (J-lens: one top-10 list); the joint
  unit uses sample 0 of each layer. The sidecar's `sampling` gains the value `prose3`; `read`
  and `produce` take `samples=3`.
- Size of the one-time capture (read sites only, 11 layers, 5,120 values, bf16): 1.79M vectors
  = 18.3 GB (benign role-play 10.7 GB, agentic 5.7 GB, overrefusal 1.3 GB, main 0.67 GB,
  story 0.02 GB). 1,075 prompts, 302k prompt tokens: the forward passes are minutes; expect
  under 2 GPU-hours including model load. Benign role-play's 86 shared prompts are WSB's
  jailbreak controls and may already be in the store (same token ids and positions → same key).
- Lens generations per prose method: about 1.13M without benign role-play, about 2.2M with it
  (if it is read at k = 1). At the measured 0.022 s per sequence (O-lens, B300) that is about
  7–14 GPU-hours per method through the batch reader; the served engine should be lower. NLA
  cannot use the served engine (it changes more than one input embedding) and reads through
  `produce`.
- The dataset is Agam's (`agu18dec/workspacebench_residuals`); HB's folders go up as a PR to it
  and `evals/residuals/map.json` gets the spliced part, or HB gets its own dataset if he prefers.
- Gate: readouts of O-lens re-read from the store for one layer agree with the stored v8
  readouts where sampling is deterministic (J-lens: token lists identical), and for sampled
  prose the index row is compared with the frozen one.

## 4. Stage C: index version `hindex-v9` (scoring changes; each needs its own plan section and re-judge)

Listed so that stages A and B leave room for them; none is designed here.

1. J-lens summariser: non-semantic tokens are junk, an all-junk readout is junk; the language of
   a token is not evidence about the text. J-lens also gets the interpreting summariser in
   overrefusal and benign role-play.
2. Agentic misalignment: "the CEO" for an executive is always `substitution_sibling`.
3. Overrefusal: renamed (it measures off-topic refusals); a unit counts only if every method has
   a digest, or the commonly refused prompts are removed (her choice, open).
4. User modelling (sibling): WSB's item changes (one item dropped, one rewording), a more
   generous commitment rule, a J-lens score.
5. Benign role-play: after the WSB jailbreak changes, it becomes this checkout's
   `jailbreak_recognition` on the HB-only bank plus the shared controls (the pinned subprocess of
   §1.2 is removed), with the Qwen gate re-checked on the 64 HB-only prompts.

## 5. Stage D: a fast tier (only if she wants one)

Can be designed offline from the frozen v8 cells with no API calls: candidate cuts are fewer
layers for the per-layer rows, a seeded half of the main items, and a position window for
agentic misalignment (every prompt token × 11 layers is 555k cells per method). The test is
whether the three methods' index values and order are preserved with intervals.

## 6. Stage E: retire the HB repository

Archive `camila-blank/hallucination-bench` with a pointer here once stage B1 has merged and a
full run has been made from this repository. `scripts/vendor_hb_index.py` and the `HB_SHA` pin
are removed when `hindex-v9` is built here; until then the vendored v8 file is the record.

## 7. Open, for Camila

- Fast tier: yes or no, after the B1 timing run.
- Overrefusal name and drop rule; how generous the user-modelling rule is; whether v9 replaces
  v8 as the headline.
- Where HB's activations live (PR to Agam's dataset, or a separate dataset).

## 8. Order of work (stage A)

1. `git mv`-free import: copy the trees of §1.1 from `/root/tmp/hb-v8-final`, apply §1.2.
2. `pyproject.toml`, CLI dispatch, CI, conftest.
3. G3, then G4, then G1/G2/G5 in tmux.
4. Docs.
