# Plan 0062: nyt_connections — can a lens read the connection Qwen will choose?

Status: draft (2026-11-02). Family: `nyt_connections` (group `model_specific`). Pilot of record:
global-workspace branch `camila/connections-feas`, `docs/project/experiments/connections/README.md`
(follow-ups 5–6 and the fresh-context `AUDIT.md`).

## 1. Construct

Qwen3.6-27B sees an NYT Connections puzzle (16 words, sort into 4 groups of 4) and answers with
**member words only** ("Do not name or explain the connections"). In a follow-up turn it names the
connection it had in mind for each group; that name is the label. The lens reads the **last 12
tokens of the prompt**, before Qwen writes anything. The question per Qwen group ("slot"): does the
summary of the readouts point to **Qwen's** connection label, among labels for competing groupings
of the same words (the NYT label, red-herring labels) and an unrelated label?

The label is never written in the read prefix, and Qwen is often wrong (31% of its groups match
NYT), so text-only readers that solve the puzzle bet on the NYT label where Qwen chose another.

Pilot evidence (bank v1, 335 puzzles, 905 slots, custom strict summarizer + union of per-layer
groupings; NOT the bench digest — numbers here must be re-measured with it):
- stable slots (n=89): all-layer union of final O-lens 0.69 / whitened 0.70 / ML-NLA 0.66 / s3d 0.64
  vs prompt-only summary 0.47 (p ≤ .014), forced direct guess 0.58 (whitened p=.04), continuation
  floor 0.69 (= Qwen's own 4 member words for the slot). Single layers almost never beat the
  prompt-only floor. The effect sits on Qwen's first group (the words it writes next); groups 2–4 beat
  no floor.
- model-specific slots (stable, Qwen wrong, label ≠ the NYT connection; n=25): 0.32–0.36 vs
  0.08/0.12 prompt-only (ML-NLA union p=.016), at the continuation floor 0.40.

So the family measures "the lens carries the connection behind the upcoming words, before they
are written", with a known ceiling (the continuation floor). That ceiling is reported, not hidden.

## 2. Bank v1 (`evals/nyt_connections/items.json`), ids-only

NYT puzzle text (the 16 words, the NYT group names and members) is **never committed** (same rule
as knowledge_cutoff's nocot items, plan 0018: no text, no readouts, no `cells.jsonl` of these
items in git). It is fetched at plan/judge time by a new module `src/wsbench/nyt.py` from the
public mirror `github.com/Eyefyre/NYT-Connections-Answers` `connections.json` **pinned to commit
`0c42b26054d4d9e2b1d8fc7b1599925a97fb1ec7`** (2026-10-31; 1233 puzzles; the exact file the pilot
used, sha256 `1bd17c9161c665dbdf2ea8011123a574b7e831cc6442fc7a73fd144d0371f534`; the mirror carries
no license, one more reason not to commit its text), sha256-checked, cached as that one file under `$WSB_CACHE/nyt/` (default
`~/.cache/wsbench/nyt/`). A fetch or hash failure raises with the URL and both hashes.

Committed per item (puzzle): `id` (`c<NYT number, 4 digits>`), `nyt_id` (int), `date`, the seeded
word order is recomputed (see below), and per slot:
- `slot` (Qwen output order 0–3), `members` as **indices into the shuffled 16-word list** (never the
  words), `qwen_label` (Qwen's follow-up name: model output, committed), `stability`, `stable`
  (≥ 12/16 samples), `correct`, `gold_same`, `model_specific`, `overlap_with_nyt`;
- `options`: ordered list of `{role, label}` for roles `qwen`, `alt` (Opus-written red-herring
  labels, committed), `foil` (another puzzle's Qwen label, committed), and `{role: "gold",
  nyt_group: k}` for the NYT label (resolved from the fetched JSON at load time: `answers[k].group`).
  The same-connection filter has already been applied (frozen; the judge never re-runs it).

Word order: the pilot shuffles each puzzle's 16 words with `random.Random(f"conn-{nyt_id}")` over
the NYT order `[w for g in answers for w in g.members]`, words stripped. `nyt.py` reproduces it;
the build script asserts the reconstruction equals the pilot bank's `words` for every item.

Header: `family`, `version: 1`, `frozen`, `source` (mirror URL + pinned commit + sha256),
`subject_model`, `answer_format`, `stability`, `options` (construction + filter prompt id),
`read_positions`, `excluded`, `pilot` (global-workspace commit + bank v1 sha256 7621e197…),
`contract: {native_langs: ["en"]}`, `counts` (335 puzzles / 905 slots / 89 stable / 25
model-specific).

Builder: `scripts/build_nyt_connections_bank.py --pilot-bank <gw outputs/.../bank/items_v1.json>`
converts the pilot bank (which holds the text, off-repo) into the ids-only bank and verifies the
round trip (words, members-as-words, NYT labels) through `nyt.py`. It is run once by the author;
the pilot bank is not needed afterwards.

## 3. Read plan (`readplan._plan` branch)

`ReadSpec(family, id, "chat", {"kind": "last_n", "n": 12}, GRID, text=<PROMPT_MEMBERS with the
words>, lite_positions=dict(SAME))` — lite = the same 12 positions (cheap already). `text` is
rendered from `nyt.py` at plan time. `PROMPT_MEMBERS` is copied verbatim from the pilot
(`conn_common.PROMPT_MEMBERS`) into `src/wsbench/evals/nyt_connections/prompts.py` and pinned by a
test (the exact string; a change invalidates every readout). `tests/test_readplan.py` family count
+1. The pilot's custom read (`fanout_custom`, render chat, last_n 12) produced identical positions
and tokens for all 367 puzzles (pilot parity check), so the plan matches the pilot reads.

## 4. Scoring (summary mode = the bench default; chain_intermediates pattern)

1. `digest.collapse` per puzzle (the bench digest, `summ_layers=union`, hierarchical overflow,
   default budgets unless the smoke shows the cap drops groupings, then a `FAMILY_DEFAULTS`
   entry). `DIGEST_TASK_NOTE` (no gold; `tests/test_task_notes.py`): the model is reading a
   16-word grouping puzzle and has not answered yet; note the groupings and connections the
   readouts state.
2. One MC call per slot (`mcjudge.Call`): the digest + the slot's labels (shuffled with
   `mc.seeded_shuffle(f"{id}-{slot}")`) + the escape `mc.CANNOT` appended last
   (`mc.options_with_cannot`). Question: which connection the readouts specifically support for
   a group the model formed. Pass = Qwen's label. Prompts tool-free (`mc.READOUT_PHRASE`), verbatim
   in `evals/nyt_connections/README.md`.
3. Partner null (chain's `null_shuffled`): the same MC with the digest of another puzzle (fixed
   derangement `ids[(i + 7) % n]`), reported as `null_rate`.
4. Rows: one per (puzzle, slot) with `pass` = picked Qwen; `pass_rate_result(rows, n_items=…)`
   over **stable** slots is the headline (pre-declared). Extras (`_columns`): all slots,
   model-specific slots, slot 0 vs slots 1–3, null rate, cannot-tell rate, picks by role.

## 5. Floors and gates

- Prompt-only (`baselines/prompt_only_opus.py`, `po-opus-v1`): Opus answers once per plan row with
  the puzzle prompt (what groupings it expects the model to be considering) → one prose cell →
  the same instrument. Frozen into `evals/baselines/prompt_only_summary.json` via `wsbench freeze`.
  Add whatever per-family hook `prompt_only_opus` needs (find the precedent families).
- Lucky guessing (`baselines/lucky_guessing.py`): an Item builder over the slots (options only, no
  readouts) + `DESCRIPTIONS`. If an options-only guesser is > 5 points above uniform, report it
  (Qwen's labels may be more "answer-like"); do not silently pass.
- Continuation floor: not a bench mechanism. Report the pilot number in the family README
  (0.69 stable) as the known ceiling; do not add a mechanism in this PR.
- `capable/questions.py`: `NO_QUESTION` (the gate is Qwen's own behaviour, like unverbalized_hint).
- `text_inversion.NOT_GATED` with the reason.

## 6. Validation run (after the code is in, before merge)

Readouts for `final-olens-rl600-x4` at GRID via the in-house producer against this branch's bench
SHA (plan_sha must match), `wsbench judge family=nyt_connections limit=3` live smoke, then the full
judge on the union and on single layers L44 and L52, plus the prompt-only floor. Record the
numbers in the family README with the pilot numbers beside them. Bar for keeping the family in the
macro: stable-slot pass rate above the prompt-only floor with a non-overlapping bootstrap CI; if
not met, register it with `not_in_macro` and the reason.

## 7. Files

- New: `src/wsbench/nyt.py`; `src/wsbench/evals/nyt_connections/{__init__,prompts,judge}.py`;
  `evals/nyt_connections/{items.json,README.md}`; `scripts/build_nyt_connections_bank.py`;
  `examples/readouts/nyt_connections.jsonl` (toy rows, no NYT text: synthetic words);
  `tests/test_nyt_connections.py` (offline: monkeypatch `run_calls` and the `nyt.py` fetch with a
  synthetic 2-puzzle fixture).
- Edited: `readplan.py` (branch), `tests/test_readplan.py` (count), `capable/questions.py`,
  `baselines/text_inversion.py`, `baselines/lucky_guessing.py`, `baselines/prompt_only_opus.py` if
  needed, `README.md` (entry with *What it is* / *Example* / *Judged by*, Judges row, Credits),
  `NOTICE.md` (NYT / mirror credit), `plans/README.md` table, `CLAUDE.md` family bullet.
- CI: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` (no network in
  tests).

## 8. Risks

- The bench digest (≤10 + ≤15 claims per layer) may drop the groupings the pilot's uncapped
  extractor kept; the pilot showed the cap changes results. Smoke checks it; a family default
  raises `summ_max` / `summ_rare` if needed.
- Small n: 89 stable slots, 25 model-specific. Bootstrap CIs will be wide; the family README says so.
- The upstream mirror could disappear: the pinned file is cached; document the hash so any copy
  can be verified.
