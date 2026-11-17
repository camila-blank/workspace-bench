# 0079: mOre-lenses O-lenses and neuronpedia J-lenses on the adapted sets (full + ATI subsets)

Issue #400. Camila (2026-11-06): score the per-subject oracle lenses of `agu18dec/mOre-lenses` and
the neuronpedia J-lenses on the adapted WorkspaceBench sets of the subjects that have both, on each
subject's full adapted set (`adapted/<m>/`) and on that set's anti-text-inversion (ATI) subset.
Spend is uncapped (§9 ledger + estimate). Three other agents keep changing the sets while this
runs (plans 0075 refill, 0077 pools, 0078 model_multihop), so everything is keyed by item (§5).

## 1. Scope (decisions restated)

Subjects (order of runs): **qwen3.5-9b, qwen3-8b, qwen3.5-4b, qwen3.5-2b, qwen3.5-0.8b.**

| subject | O-lens arms (README WSB tag -> run dir under the repo) | J-lens arm (neuronpedia file) |
|---|---|---|
| qwen3.5-9b | `qwen3.5-9b.inv.end` (`inverter/ao.qwen3.5-9b.s0`), `qwen3.5-9b.inv.L20.end` (`inverter/ao.qwen3.5-9b.L20.s0`), `qwen3.5-9b.sft` (`distill/ao.qwen3.5-9b.distill.s0`), `qwen3.5-9b.rl.i600` (`rl/rl.qwen3.5-9b.ddp600.s0`) | **`jlens-base`**: `qwen3.5-9b-pt/jlens/Salesforce-wikitext/Qwen3.5-9B-Base_jacobian_lens.pt` |
| qwen3-8b | `qwen3-8b.inv.s6795` (`inverter/ao.qwen3-8b.wsd.s0`), `qwen3-8b.sft`, `qwen3-8b.rl.i600` | `jlens`: `qwen3-8b/.../Qwen3-8B_jacobian_lens.pt` |
| qwen3.5-4b | `.inv.end`, `.inv.L20.end`, `.sft`, `.rl.i600` | `jlens`: `qwen3.5-4b/.../Qwen3.5-4B_jacobian_lens_n1000.pt` (the n1000 file, as `scripts/adapt_jlens.py`) |
| qwen3.5-2b | `.inv.end`, `.sft`, `.rl.i600` | `jlens`: `qwen3.5-2b/.../Qwen3.5-2B_jacobian_lens.pt` |
| qwen3.5-0.8b | `.inv.end`, `.sft`, `.rl.i600` | `jlens`: `qwen3.5-0.8b/.../Qwen3.5-0.8B_jacobian_lens.pt` |

17 O-lens arms (every checkpoint with a WSB tag for these subjects; the RL i600 arm is the
headline) + 5 J-lens arms. Pinned: `agu18dec/mOre-lenses@441fd264`, `neuronpedia/jacobian-lens@
e166deb5`, each subject's weights at its `wsbench.adapt.subjects` revision.

- **9B J-lens = the BASE-model lens** (Camila 2026-11-06; an instruct 9B fit of Agam's,
  `adapted/README.md` "Fitted J-lenses", lived on his box and is deliberately not used): neuronpedia has no instruct 9B lens; the
  `Qwen3.5-9B-Base` lens is applied to the chat subject's activations. Every table labels it
  `jlens (base-model lens)` and the results doc says it was fitted on Qwen3.5-9B-Base.
- **No J-lens is fitted** (Camila's standing rule); `scripts/fit_jlens_modal.py` is not run.
- **OLMo-3-7B: no J-lens.** neuronpedia's `olmo-3-1025-7b` lens was fitted on
  `allenai/Olmo-3-1025-7B` (its `config.yaml` `hf_model_name`), the BASE model; the subject is
  `allenai/Olmo-3-7B-Instruct` (adapted/olmo-3-7b/report.json `hf_id`, an Instruct-DPO
  descendant). Per the brief, not used; reported.
- **GLM-5.3 out of scope** (Camila, relayed 2026-11-06): its O-lens and J-lens WSB results exist.
- **Families judged:** every family the set plans (`wsbench run all=True`, its own superseded /
  retired skips), **minus** `jlens_concept_pr` (blocked on all five sets; a native J-lens
  precision family, excluded from the GLM lens tables too) and `introspection` (ooc on all five).

## 2. Read spec (the checkpoint's own contract)

From the mOre-lenses README and each run's `olens.json` (`read`), asserted at load:

- **Activation:** output of decoder block L at the plan cell (`hidden_states[L + 1]`), read from
  the BASE model (adapter disabled: `wsbench.produce.backend.Backend.capture`).
- **Layers = the adapted plan's own layers that lie in the lens band, never remapped or snapped.**
  The plan's depth remap (`round(L * n / 64)`) gives (checked with `readplan.plan` under each
  root): 9B / 4B 10..30 even (+ L31 on agentic_misalignment only); 8B 12..22 and 26..34 even (its
  `read_band`; no L24); 2B / 0.8B **8, 9, 10, 12, 14, 15, 16, 18, 20, 21, 22** (+ L23 on agentic).
  The mOre-lenses were trained on the README's even bands only (inverter band 9B / 4B L10-L30,
  8B L12-L34, 2B / 0.8B L8-L22; `olens.json read.layers`), and the J-lens has no source layer
  for the last block. So each subject's **read set** = plan layers in its even band: 9B / 4B
  10..30 even (11 layers), 8B 12..22 + 26..34 (11, the plan's), 2B / 0.8B **8..22 even (8
  layers; the plan's odd 9 / 15 / 21 and the last-block 23 / 31 are not read)**. This is a subset
  of the plan's own layers, not a snap (snapping is a `read_band` re-freeze of the set, which is
  the set owners' call, `adapted/README.md`), so every judged cell is at a plan layer. Every arm
  of a subject (O-lens stages AND its J-lens) is read and judged on the SAME read set with an
  explicit `layers=<read set>` (guard (ii) then requires exactly those layers; a `layers=` run is
  a complete, defined variant). The 27B grid's 11 layers become 8 on the two smallest subjects;
  reported beside their numbers. (Agam's adapted mode snapped the plan into the band,
  `wsbench_adapt.plan(..., qs.BAND)`; on 9B / 4B / 8B the two rules coincide.)
- The **L20 inverters** (trained on 20..30) are READ at the whole read set, as Agam's producer did
  (out-of-band layers = an off-distribution probe); the headline is judged at the whole read set
  (comparable with the other arms and with Agam's tag) and a second view at the trained layers
  only (`layers=20,22,24,26,28,30`; per-layer digests shared through `summ_store`, so only the
  stage-2 and judge calls are new: ~45% of an inverter arm's output cost, in §9).
- **Transform:** `scaled` (inverters) = `global_scale * h` with the run's frozen scale
  (`olens.json read.global_scale`); `unit` (distill, RL) = `alpha * h / |h|`, alpha 16000.
- **Prompt:** `continuation_raw` (inverters) / `concepts_raw` (distill, RL), the template text of
  `olens.json`; the subject's chat template, one user turn, thinking off; the injected vector
  REPLACES the input embedding at the slot token (`read.slot_id`, asserted to occur exactly once
  in the carrier at every layer).
- **Sampling of record:** T 1.0, top-p 0.95, top-k 64, 256 new tokens, k = 1; stop at eos /
  `<|im_end|>` / the generation config's eos; decoded with special tokens skipped, stripped. Sidecar
  `sampling: prose1` (multi-bullet and continuation lenses = one sample per activation, plan 0032
  Q1).
- **Adapter:** `olens.json adapter_subfolder` at the pinned revision: `.../lora` for inverters and
  distill (the run's END checkpoint, what Agam's `_end_lora` read; the README's `lora_hf/` copy is
  never used), `.../iter_000600` for RL. Loading refuses a silent no-op (no nonzero `lora_B`) or
  any LoRA tensor PEFT did not attach (count in the file == count on the model).
- **Carrier:** built from `olens.json` (template, `slot_char`), the training contract: the slot
  char must encode to exactly `[slot_id]` and occur exactly once in the carrier at every layer
  (enforced). Agreement with the bench's own search `wsbench.produce.methods.OLens._carrier` is
  recorded per layer (`carrier_matches_bench` in the job log), not enforced: it agrees on the
  Qwen3.5 sizes (the 27B's tokenizer, where Agam asserted it), but on Qwen3-8B that search picks
  U+320E while the 8B checkpoint was trained on U+321C / 144490 (README recipe table), so the
  checkpoint's contract wins.
- **J-lens:** the bench's `wsbench.produce.methods.JLens` readout of record, `(W_U J h) /
  ||J^T W_U[t]||`, top 10 (`tokens10`), W_U = the subject's `lm_head`, batched per layer; every
  job re-reads up to 8 cells through `JLens.read` itself and refuses any difference.

## 3. Producer: `scripts/lens_readouts_modal.py`

A port of global-workspace `scripts/ola/port/subject_wsbench_qwen_modal.py` (Agam's producer of
record for the README's small-Qwen WSB tags: HF + PEFT, batched `inputs_embeds` decode; same image
pins `torch==2.9.1` cu128, `transformers==5.11.0`, `peft==0.19.1`, `flash-linear-attention==0.5.1`,
the causal-conv1d wheel), reading the ADAPTED plan (`WSB_BANK_ROOT=adapted/<m>`,
`readplan.plan(family)`) instead of a remapped 27B plan, and rendering with
`wsbench.produce.render` on the subject's tokenizer (the plan's own render / positions rules).

- Modal app `wsb-0079-lens-readouts`, one function `read_job`, GPU H100 (fallbacks H200, B200),
  **`max_containers=4`** (the account cap is 16 containers shared with three agents; checked with
  `modal app list` before each launch). Weights / lenses cache: volume `wsb-0079-hf`.
- A job = one (arm, family, row pack) of ~40k cells; the local driver maps jobs
  (`order_outputs=False`, each result names its job), appends each result to the store as it
  lands (§5; a driver crash loses at most the in-flight jobs), logs per-job timing / GPU /
  per-layer median `|h|` and injected norm / empty-sample count to
  `<store>/<subject>/produce_log.jsonl`. Decode batch per subject: 1024 for the Qwen3.5 hybrids
  (smoke: 4B RL 42 cells/s at 1024 vs 30 at 512), 512 for Qwen3-8B (full-attention KV cache).
- Seeds: `torch.manual_seed` per job from `sha256(arm|family|first row_sha)`.
- A row whose positions rule selects nothing is recorded (a marker, never re-read) and reported.
- Rows needing steering (`introspection`) or captured ids from another lens (`jlens_concept_pr`)
  are refused (out of scope, §1).

## 4. Smoke and validation (before any full run)

Done once per lens type on current main (the first numbers in §9 come from these):
1. qwen3.5-0.8b RL + J-lens, 2 rows of typo_mt / basic_readout / association: the image builds,
   the adapter loads, injected-norm median 16000.0 for `unit`, readouts plausible (J-lens top-10
   names the sports at the answer site).
2. qwen3.5-9b RL + inverter, 12 user_modeling rows (7.7k cells each): inverter empty share per
   layer 0.78 at L10 falling to 0.12 at L30, matching Agam's OLENS_NOTES ("9B up to 98% empty at
   L10"), RL bullets ~470 chars, no empties; the read spec of record reproduces the known
   behaviour. Judged (user_modeling, 12 items, summary mode) for the cost measurement in §9.
3. Each subject's first job prints the carrier slot index per layer, the stop ids and the
   per-layer norms; a mismatch with `olens.json` (base model, slot id, transform) stops the job.

## 5. Incremental by item (the sets keep changing)

- **Store** (`/root/tmp/wsb-0079/store/<subject>/<arm>/<family>.jsonl`): each cell stored with
  `row_sha` = sha256 of its plan row as `wsbench plan` writes it, minus `lite_positions`,
  `lite_layers`, `note`, a top-level `reads` and `extra.reads` (layers kept: a changed layer list is a changed read).
- `produce` reads only plan rows whose `row_sha` has nothing in the store: a new id is read, a
  changed row (same id, new render / positions / layers) is re-read, an unchanged row never is.
- `assemble` writes `<readouts>/<subject>/<arm>/<family>.jsonl` from the store for the CURRENT
  plan's rows only (a dropped id leaves the file; a re-read row's cells replace the old ones) and
  the sidecar (`reads: full`, sampling, `reader` = the arm label, `arm`, layers, today's
  `plan_sha` from `wsbench.reads.plan_sha`, `source` = repo / revision / path). A family with
  unread rows is not written (no partial file) and any older file of it in the readouts dir is
  deleted (it would fail guard (iii) anyway), so a judged file is always the whole set. Rows are
  filtered to the subject's read set (§2) before hashing and reading. A row whose positions rule
  selects nothing (none expected; the 9B smoke had none) is listed in `assemble_report.json`;
  if any exists, that family is judged with `allow_missing=True` and its row stays incomplete.
- **Judging is cached by content fingerprint**: re-running `wsbench run` into the same `out=` dir
  after a set's PR merges calls the judge only for new or changed items' cells; a dropped item's
  cache rows are simply unused.
- **ATI** answers are keyed by `(family, id, prompt_sha)` (existing `text_inversion` rule), so a
  re-gate after a set change asks the panel only for new or changed items (§6).
- **ATI re-gate after a set change:** `ati_panel_judge.sh` skips a family whose
  `judged/<a>/draw<d>/<f>/results.json` exists; the refresh deletes those `judged/*/draw*/<f>/`
  result dirs for every family whose `bank_fingerprint` moved (their judge caches survive in
  `judged/<a>/draw<d>/_runs/<f>/`, so only new / changed items are judged), and also for every
  family whose judged `results.json` `prompt_version` differs from `gate_instrument(family)` (a
  judge-instrument change), then re-gates; it loops until every family the set gates has an entry
  or a `_not_gated` reason (a family skipped as "not judged for every draw" has neither and would
  fail the ATI run with exit 2).
- **Refresh protocol:** before each subject's run and at the end, `git fetch` + compare main's
  `adapted/<m>/report.json` / family files with the worktree; `gh pr list` for open set PRs; when
  a set changed: fast-forward the run checkout, `produce` (new rows only), `assemble`, re-judge
  into the same out dirs, re-gate ATI, re-tabulate. The final refresh runs when no set PR is open
  for the five subjects (or when the other agents report done).

## 6. ATI subsets of the adapted sets (plan 0071's follow-up, built here)

**Rule (Camila 2026-11-05/06, restated):** an item is in a set's ATI subset iff **every** reader
fails **every** one of 3 draws in the panel's `guess` form: the subject itself (in the Qwen slot),
the Claude slot (Opus 5.5; Sonnet 5.5 for a family Opus refuses on > 20% of items, the existing
`claude_slot.json` rule), astra (`openai/gpt-6-astra`, **`--route openai-wif`**) and argon
(`google/gemini-4-argon`, **`--route gemini-wif`**). Any solved draw takes the item out; a reader
with no decided draw leaves it unverified (out). The readers see the prompt (rendered with the
SUBJECT's tokenizer, decoded through the last read position), never activations. This is the
all-readers-fail rule of open PR #359 (plan 0071), applied to adapted sets; it does not depend on
#359 merging (the adapted gate is its own function and gate version).

Code (the registered 27B gate's behaviour is untouched; every change is a no-op without a root):
- `text_inversion.build`: under `WSB_BANK_ROOT`, render with the subject's tokenizer
  (`wsbench.adapt.author.tokenizer(report["model"])`, as the 2026-11-05 GLM pilot did); without a
  root, byte-identical behaviour (Qwen3.6-27B tokenizer). Each prompt row keeps its `layer`
  (`read_layer`: 44 if on the grid, else the grid middle; on an adapted grid always the middle).
- answerer key **`subject`** (`PANEL["subject"]` = the set's `hf_id`), accepted by
  `ANSWERERS`, `write_readouts` and `scripts/ati_panel_judge.sh`'s answerer filter.
- `scripts/ati_subject_modal.py`: the subject's answers, same request contract as
  `scripts/ati_qwen_modal.py` (`qwen_prompt_ids`: the subject's chat template around a user span
  no added / special token can match, thinking off, T 1.0, `n=3` with `qwen_seed`, the same max
  tokens; vLLM 0.31.0 + transformers 5.17.0 = the adapt pipeline's `adapt_modal.py` default recipe
  that serves these subjects, with the Subject's own engine kwargs, e.g. 9B `enforce_eager`,
  `gpu_memory_utilization=0.93`), `max_containers` from the shared budget (below).
- `scripts/ati_openrouter_modal.py` with `--route openai-wif` (astra) / `--route gemini-wif`
  (argon, `modal run -e camila-blank`), unchanged code, on the set's `prompts.jsonl`, launched
  with **`--shards 3`** (CPU containers count against the same 4-container budget as the
  producer and the subject, and the script's preflight is a container of its own: 3 + 1; the
  driver never has more than 4 of this plan's containers up).
- judging: `scripts/ati_panel_judge.sh` with `WSB_BANK_ROOT=adapted/<m>` (native, Opus 5.5, as for
  the registered gate). **Fix:** the script hardcodes `layers=44` for every non-native family; it
  now takes each family's layer from `prompts.jsonl` (the `layer` `build` wrote, which is what
  `write_readouts` writes at), so on the 27B grid it is still 44 (unchanged) and on an adapted
  grid the middle layer.
- **Gate = the registered `_gate_panel`, parameterized, not a copy:** `_gate_panel` gains
  `subject="qwen"` (the panel's first reader key) and `verdict=panel_verdict` arguments (defaults =
  today's behaviour, byte-identical entries). `gate_adapted(out, draws, dst)` (+ `wsbench
  text-inversion stage=gate_adapted`) calls it with `subject="subject"` and the all-fail verdict
  (`ati` iff every reader's `item_status` is `failed`, `invertible` iff any is `solved`, else
  unverified), so everything else is shared with the registered gate: `gated_ids`, the Claude
  slot per family (`claude_slot.json` from `stage=slots` / `decide_slots` on the set's out dir),
  `_answerer_draws` (with `STRICTER_PASS_FIELD`, `GATE_ANY_BASIS`, `ROWLESS_IS_FAIL`), the
  refusal and > 20% unverified "not gated" rules, `CRAFTED` stop-and-report, `ATI_EXCLUDE_ALL`
  (multilingual_typo out by construction), `ATI_EXCLUDE` (hand-audited 27B ids; applied where the
  same id is in the set, reported), and `ATI_MIN_RULE` / `ati_reportable` (<= `MIN_ATI_ITEMS`).
  It writes **`adapted/<m>/anti_text_inversion.json`** in the registered entry schema with
  `gate: "adapted-panel-v1"` (a `gate_version` argument of `_gate_panel`; also the panel list,
  the `readers` key and the judged-config path take the `subject` key, and the subject's model id
  is passed in, so `PANEL`'s registered contents never change), the reader map incl. the subject, `bank_sha` =
  `bank_fingerprint(family)` under the root (follows `registry.bank_path` / `readplan.plan`),
  `instrument` = `gate_instrument(family)`, plus `_not_gated` (the registered `NOT_GATED`
  reasons apply unchanged). The default family list under a root is `gated_families()` ∩ the
  families with a bank under the root and at least one gated id (`adapted_gated_families`;
  `build` uses the same list), so e.g. introspection's missing bank never reaches `readplan`.
- `text_inversion.subset_entry` / `subset`: with no `dst`, read `adapted/<m>/anti_text_inversion.json`
  under `WSB_BANK_ROOT` (today it would read the 27B file and fail on bank_sha), the registered
  file otherwise (unchanged). The bank_sha staleness check covers `adapted-panel-v1` entries too:
  **a set that changed since gating exits 2 until re-gated** (fail-closed, §5).
- **#359 interaction:** #359 (open) adds `PANEL_GATES` and `results._stale_ati_gate`, which drops
  from the macro any ATI row whose `extras.ati_gate` is not its `GATE_VERSION`. Whichever PR
  merges second makes `_stale_ati_gate`'s `gate == GATE_VERSION` comparison a set membership
  that includes `"adapted-panel-v1"` and adds it to `PANEL_GATES` (an adapted entry is current
  under its own version); #359 also renames today's `panel_verdict` to `panel_verdict_min` and
  makes `panel_verdict` all-fail, so post-#359 `gate_adapted` may pass that verdict instead of
  `all_fail_verdict` (same rule). If this PR merges first, a comment on #359 names the lines. Until then main has
  no stale-gate rule, so nothing is dropped.
- Then `wsbench run ... anti_text_inversion=True` under the root scores each arm on the subset
  from the SAME judge cache (cache-only for every item already judged in the full run), and
  `results.ATI_MACRO_MIN_ITEMS` (20) keeps families with fewer survivors out of the ATI macro
  (shown, flagged).
- Shared answers across sets: the frontier readers' answers depend only on the prompt, and the
  four Qwen3.5 sets share a tokenizer and many items; a `seed` step copies another set's answer
  rows whose `(family, id, prompt_sha)` match the current prompts (never the subject's answers).

Sizes per family per set are reported; families with n < 20 are flagged.

## 7. Judging

`wsbench run all=True readouts_root=<readouts>/<subject>/<arm> out=<judged>/<subject>/<arm>`
under `WSB_BANK_ROOT=adapted/<subject>`, `WSB_ANTHROPIC_AUTH=sdk ANTHROPIC_PROFILE=default` (no API
key exported), the bench defaults (summary mode, layered digest for multi-layer runs, Opus 5.5 +
the family pins), `summ_store=<judged>/<subject>/<arm>/_store` (shared per-layer digests),
`WSB_TOKEN_LEDGER` set (one ledger per subject), and `layers=<read set>` on every run (the
digest cache stem carries the layers tag, so the ATI run passes the IDENTICAL `layers=` list to be
cache-only). Known row quirk: buggy_code's per-item `layer` field falls back to the bank's
`read_cells` layer on a multi-layer run (2B: L21, outside its read set); it only labels rows,
scoring reads every judged layer. Every arm is judged on the full set first, then
`anti_text_inversion=True` into a sibling `out=` (cache copied, so subset runs are cache-only).
Long runs in named tmux sessions, logs under `logs/0079/` (copied to
`/workspace/wsb-runs/logs/0079`).

## 8. Results

- `docs/plan0079_results.md` (the results doc, as `docs/plan0012_results.md`): per subject the
  table **family x lens stage, full set and ATI subset side by side** (value, CI, n), the macro row
  (bench `results.macro` rules: incomplete families out and footnoted), the **stock-subject-v1**
  and **po-opus-v1** floors from `adapted/<m>/floors/floors.json` beside them, read straight from
  that file (not through `wsbench report`, which hides a floor whose stamp differs, and never draws a
  po-opus-v1 floor, a single-layer digest instrument, beside a layered all-layer row); each floor
  cell carries its protocol, n and stamp, and a po-opus-v1 floor that still waits for #350 is
  marked stale. On the ATI subset the floors are re-scored from the per-item `rows` of the floor
  runs' `results.json` (stock-subject: `/workspace/wsb-runs/adapted/<m>/floors/results/<family>/`;
  po-opus-v1: its run dir where one is found) restricted to the ATI ids; a floor without per-item
  rows is shown as missing, never estimated. Floors are not the lens instrument: stock-subject-v1
  rows are native single-layer reads and po-opus-v1 rows a single-layer digest (`layers_judged:
  [44]`), beside 8- / 11-layer layered lens rows; said under every table, ATI sizes per family, the J-lens availability notes (9B base lens, OLMo not used), and
  a comparison with Agam's README `headline_23` numbers (reference only: bench, banks and sets
  changed since). An arm with > 5% empty items is incomplete under summary mode (`results.py`) and
  stays out of the macro: expected for the shallow-layer-empty inverters on the small subjects,
  footnoted, not a surprise.
- `adapted/<m>/lenses/scores.json` (+ `table.md`): the machine-readable rows (family, arm,
  scope, value, ci, n, complete, instrument stamp, plan_sha, set commit). No readouts, no
  `cells.jsonl`, no NYT text, no nocot text are committed (repo rules).
- `adapted/README.md`: one short section pointing at the results doc.
- Readouts and judged dirs: produced on /root/tmp, rsynced to `/workspace/wsb-out/0079/`, local
  copies deleted after verification (the dev disk is ~96% full).

## 9. Cost (ledger + estimate)

Measured in the §4 smokes (H100; Opus 5.5 $4 / $20 per MTok, Sonnet 5 $2 / $10):
- GPU: RL / distill ~28 cells/s (9B), inverter ~68 cells/s; J-lens is capture-bound (minutes).
  Full-read cells per arm: 9B 1.84M, 8B 2.00M, 4B 1.69M, 2B 1.29M, 0.8B 1.03M (7.85M). Estimate
  **~165 H100-hours (~$700)** for the 17 O-lens arms, ~41 h wall at 4 containers.
- Judge: 12 user_modeling items (7.7k cells): RL arm 2.95M in / 0.36M out tokens, inverter
  0.72M / 0.34M. Scaled by cells (input) and items x layers (output): ~$4k per RL / distill arm
  on the 9B set, ~$2k per inverter / J-lens arm, smaller sets proportionally less, plus the two
  L20 trained-layer views (~$1k each: stage-2 + judge calls only). **Estimate
  ~$40-50k Claude** for all 22 arms on the full sets (the ATI re-scoring is cache-only), **~$5k**
  for the five ATI panels (answers + native judging). Stated in #400 before the first large
  fan-out; the ledger (`WSB_TOKEN_LEDGER` rows + Modal GPU-hours) is tallied per subject in the
  results doc.

## 10. Tests (new only)

- `tests/test_lens_readouts_store.py`: `row_sha` ignores lite / note keys and `extra.reads` and
  moves with layers / text / positions; `assemble` writes only the current plan's rows (a dropped
  id leaves, a changed row's new cells are used, a family with an unread row is not written), the
  sidecar carries today's `plan_sha` and judges through `reads.load` without a guard failure.
- `tests/test_adapted_ati.py`: `gate_adapted` keeps an item only when all four readers fail all
  draws (one solved draw out; an undecided reader out; `subject` in the Qwen slot); entry schema
  fields; `_not_gated` carries the registered reasons; `subset_entry` under `WSB_BANK_ROOT` reads
  the set's file, a changed bank exits 2, no root reads the registered file byte-identically;
  `build` under a root uses the subject tokenizer (stubbed).
- **No pre-existing test is modified.** If one turns out to depend on the old behaviour, the PR
  stops and this plan is amended first with the exact test and assertion.

## 11. Not in this plan

- Fitting any J-lens; OLMo / GLM lenses; the J-lens precision family `jlens_concept_pr`.
- `reads=lite` variants (the bench default `reads=full` is the number of record).
- Changing any judge, bank, floor or the registered ATI file.
