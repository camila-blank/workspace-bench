# 0078: model_multihop on the ten adapted sets

Issue #392. Builds plan 0076's family (`model_multihop`, #380) for every per-model adapted set:
glm-5.3, deepseek-v4-pro, muse-spark-open, kimi-k3, qwen3-8b, qwen3.5-9b, qwen3.5-4b,
qwen3.5-2b, qwen3.5-0.8b, olmo-3-7b (`adapted/<m>/`).

## Camila's ask and decisions (2026-11-06), restated

- Build `model_multihop` for all ten sets; spend uncapped (log it); **no lens / reader runs** on
  the new sets. Only build, gate, freeze, floors.
- **Gate:** every prompt form at >= 16/20 samples at T=0.7 (her 2026-11-05 decision for the
  family; never relaxed).
- **Lucky-guessing bar, STRICT:** the family's blind and described guessers each within chance +
  5 points; a per-item leaker (a guesser >= 3/5 on the gold AND blind + described >= 4/10,
  `leaks.py`) is dropped (rebuilt is not possible here, see §6). If the bar is structurally
  unreachable on a subject, stop and report the numbers; never ship a leaking set.
- **Item-quality review** (skill §4, #391): illegit distractors, an answer reachable without the
  bridge, capital-routed clues, ambiguous gold, near-duplicates: dropped and back-filled, never
  "reported".
- **Small subjects:** the yield-based ooc rule (refill unless truly incapable; measure, don't
  assume).
- Target >= 100 items per set where the subject is capable.

## What stays exactly as plan 0076 (the invariants, restated)

Each is restated again at the step that touches it.
1. **The read prompt is ONE question.** The entity is named only inside it (no "Consider X."
   sentence). Invented: `What is {hop phrase} of {ref_first}? ` + the answer suffix; cutoff: the
   gw single-question route (gw `m2` of the cutoff candidate, which `assemble_v2` swapped into
   `m`). Suffix verbatim: `If you are not sure, give your best guess. Answer with just the {t}.`
2. **Read region = gw `post`** (every position after the entity: the rest of the question, the
   suffix, the assistant header); lite = gw `tail` (the last 12 prompt tokens); `positions ==
   spans.post`, contiguous, every post position > max(`spans.entity`).
3. **The gate:** B, M (the read prompt), M2 (the entity-first route) and A (the hop asked of the
   bridge directly) each have a modal canonical answer with share >= 16/20 at T=0.7, thinking
   off; chain consistency (M mode == M2 mode == A mode); identifiability (the M mode is neither
   the subject's greedy answer to any A_f (the hop asked of a foil) nor any foil's true value);
   leak (no bridge form in M or M2). Date items stay out.
4. **Headline selection:** both Opus prompt-only floors (gw `FLOOR` templates, plain and
   cutoff-aware wordings, 3 each, Opus 5.5) miss the subject's bridge: all 6 picks valid and 0
   of 6 name the bridge.
5. **Decoys = the item's own foils**, never neighbour sets (rev G: the neighbour bank leaked,
   blind 0.546). Invented: the item's own candidate countries (gw rule: Sonnet's top-5 for the
   entity, canonicalised, then the subject's minority answers with >= 2/20 samples), minus the
   bridge; cutoff: the candidate's own foil names minus anything that clusters to the bridge.
6. **The judge is untouched:** prompts, matcher (`judge.option_spans`, containment, blockers,
   `exact_case`), result stamp `mmh-2026-11-06+mmh-forms-v3+bank-v2` (under `WSB_BANK_ROOT` the
   `+bank-v<N>` tag names the adapted header's version, which stays 2). Items keep the bank-v2
   shape: `mc.order` (seeded order `mc.seeded_shuffle(opts, "mmh-2026-11-06|<id>")`), `mc.gold`
   (the bridge name), `mc.forms`, optional `exact_case` / `blockers` / `blocker_of`; a form two
   options share is withheld from both; `answer_names_bridge` is flagged, never scored.
7. **id-keyed bank: NEVER write a `name` key** (`readplan.bank_file` / `judge_inputs.item_id`
   switch on it). The source pool's `name` field is renamed `entity_name` in work files and is
   not copied into items.

## What changes per subject

The bridge (the subject's own modal answer), the hop (re-chosen so the subject's bridge is
identifiable among its foils), the foils (the subject's minority answers join), the hop answer,
`input_ids` (the subject's own chat template, thinking off, no system turn) and the spans.

## 1. Candidate pool (`scripts/build_mmh_pool.py` -> `$WSB_POOL_DIR/mmh_pool.json`)

- **Tier A:** gw `frozen_v2.json` (main `2027ef208`): the 1571 Qwen-gated items' candidates (1373
  invented, 198 cutoff), registered headline ids included.
- **Tier B:** every other candidate gw generated and screened for mmh v1/v2
  (`/root/tmp/mmh-out/cands_invented*.json`: 12,072 unique invented entities after Sonnet's
  real / marker / top1 >= 0.7 screen; `cands_cutoff*.json`: 7,986 cutoff candidates on 2,416
  descriptors). They are the same construction as tier A (same prompts, screen and fields); Qwen
  merely was not stable on them, which does not bind another subject.
- Each row keeps the gw candidate fields (`id`, `cat`, `type`, `cluster`, `entity_name`,
  `pre_text`, `b`, `ref_after`, `ref_first`, `role`, `sonnet_top5`; cutoff: `descriptor`, `m`,
  `m2`, `b`, `a_template`, `answer_type`, `hop_kind`, `foil_cands`, `domain`, `event_date`,
  `truth_bridge`) plus `tier` and `registered` (the id is a registered headline id). Invented rows
  are deduplicated by folded entity name (tier A wins), cutoff rows keep every hop variant
  (selection keeps at most one item per descriptor, §5).
- The file is box-only like the other refill pools and sha256-pinned in
  `refill.POOL_FILES["mmh"]` / `POOL_SHA256["mmh"]`; the builder asserts every frozen_v2 id is
  present and records the source files' sha256 in its header.
- **Tier C (only if needed):** new invented entities from gw's `INVENTED_SIB_GEN` + the Sonnet
  screen (`INVENTED_SCREEN`, same drop rules), written as a new pool file version, used only for
  a subject still under 100 after tiers A + B with a yield that is not ooc.

Why tier B before new generation: the caller's brief says "reuse frozen_v2's 1571 gated
candidates' entities as a starting pool, plus generate new invented entities if a subject's yield
is low". Tier B is gw's own already-generated, already-screened candidates (no new Opus
generation); gating it costs only subject GPU time, and booting the large subjects dominates GPU
cost, so tiers A and B are gated in one generation run per subject.

## 2. Stages (`python -m wsbench.adapt model_multihop model=<m> stage=<s>`, `adapt/mmh.py`)

Work files under `adapted/<m>/model_multihop/` (git-ignored like `kcutoff/`); Claude calls via
`selfans.claude_json` (cached per (model, prompt), ledgered by `WSB_TOKEN_LEDGER`); generation via
`run._runner(model)` (the subject's adapt backend: its Modal app; never OpenRouter: skill rule
"the gate is the subject itself, with its own template and thinking off"). Every ask is one user
message, no system turn, the subject's pinned chat template, thinking off (`template.check`).

1. **`gen1`** — B for every pool row (cutoff: once per distinct B text): 20 samples at T=0.7,
   top_p 0.95, max 64 tokens (gw `_row`), plus the runner's greedy (recorded, not gating).
   `gen1.json` (resumable by key).
2. **`canon1`** — invented: gw `COUNTRY_CANON` over every distinct raw B string and Sonnet top-5
   name (Opus 5.5); B mode/share (gw `mode_share`: None counts in the denominator); B-stable =
   share >= 0.8 (**invariant 3**). Foils (**invariant 5**): canonical Sonnet top-5 in order, then
   the subject's other answers with >= 2/20 samples by count, deduplicated, minus the bridge,
   first 6; < 4 foils fails (`foils<4`; bank shape 5-7 options). gw `COUNTRY_ATTRS` (Opus) for
   every bridge and foil country. Hop: gw's rotation (start at `crc32(id) % 4` over calling code,
   ccTLD, currency, ISO3; the first hop whose bridge value is not among the foils' values; none:
   `no_identifying_hop`). Cutoff: gw `CLUSTER` over the B answers + foil names -> B mode/share,
   aliases, display name (the most common raw spelling of the mode); foils = the candidate's
   `foil_cands` not clustering to the bridge and not an alias, first 4; < 4 fails (cutoff chance
   1/5, as registered). Writes the gen2 asks.
3. **`gen2`** — per B-stable item: M (the read prompt, **invariant 1**), M2, A (20 samples each,
   as gen1) and each A_f (greedy). For invented the M / M2 / A texts are gw's templates with the
   re-chosen hop (`What is {phrase} of {ref_first}?`, `{pre_text} What is {phrase} of
   {ref_after}?`, `What is {phrase} of {bridge}?`, + suffix with the hop's answer type); for
   cutoff `m2` / `m` of the candidate and `a_template.format(bridge=display)` + suffix.
4. **`gate`** — gw's round-2 gate verbatim (**invariant 3**): invented answers by `_norm_attr`
   (currency clustered by `CLUSTER`), cutoff answers by one `CLUSTER` call over M, M2, A, A_f and
   the foils' true values; shares >= 0.8 on M, M2, A; M mode == M2 mode == A mode;
   identifiability; leak (the bridge or an alias longer than 2 characters in M / M2).
5. **`floor`** — the Opus prompt-only filter on every gate pass (**invariant 4**): gw `floors`
   verbatim (role = `role` / `descriptor`, prompt = the read prompt, candidates = [bridge, *foils]
   shuffled by `crc32(f"{id}|{arm}|{k}")`, the plain and the cutoff-aware arm x 3 templates,
   Opus 5.5, 1500 max tokens); a pick maps to a candidate by squashed exact match or a unique
   substring; miss = all 6 valid and 0 hits; an invalid row is re-asked on resume and never
   passes.
6. **`review`** — the item-quality review (§5) on the floor misses.
7. **`build`** — one gate record per elicited item, failures included, into
   `gate/model_multihop.jsonl` (`origin` = `original` for a registered headline id, else
   `refill`), with the reasons; a passing row's `item` is the bank-v2 item (§3); then the
   report's refill block (§4). **`all`** = 1-7.

Gate legs in the record: `b`, `m`, `m2`, `a` (rate = share, decided = 20, `own` = the mode),
`note:af` (annotation), `note:floor` (the 6 picks); `gate.pass` = every check above.

## 3. The item (bank v2 shape, **invariants 1, 2, 6, 7**)

- `id` (the gw candidate id), `cat`, `type` / `hop` / `cluster` (invented) or `domain` /
  `hop_kind` / `answer_type` / `event_date` (cutoff), `text` = the read prompt, `entity` (the
  mention as written in the read prompt, gw `_entity_in`: the descriptor / entity name, or it
  without a leading "the"; not found -> `entity_not_in_question`).
- `input_ids` = the subject's ids for the read prompt as a single user turn with the generation
  prompt (`RemoteRunner.prompt_ids`, Kimi-K3's `template_ids` path included: the exact ids the
  gate sent). `spans` from gw `regions` (ported: incremental decode with the subject's tokenizer,
  the read prompt located by its last occurrence; `ent` = positions overlapping the entity
  mention, `post` = every later position), `spans.tail` = the last 12 post positions,
  `positions` = `spans.post`. Asserted: ent and post non-empty, post contiguous and after ent,
  < len(input_ids), `len(post) >= 16` (`min_full_positions`), the entity span decodes to text
  inside `text`.
- `bridge` = the subject's bridge (invented: the canonical country; cutoff: the display name),
  `mc` from `mc_block(id, bridge, foils)`, `answer` = the subject's raw modal M answer,
  `answer_canonical` = the canonical mode, `answer_names_bridge` (the family's `named`), `b_share`,
  `floor_opus` (`{plain, cutoff}` = 0.0 by construction), `subject_fields_for` = the model key,
  `adapt` provenance (`origin`, `tier`, `hop_changed` when the hop differs from Qwen's).
- **Forms.** The country alias / blocker / exact-case tables move from
  `scripts/build_model_multihop_bank.py` into `src/wsbench/evals/model_multihop/forms.py`
  (with `mc_block`, `raw_forms`, `blockers_of`, `country_of`); the build script imports them, and
  a test asserts the registered bank rebuilds byte-identical. A subject country missing from the
  table fails its items (`forms: no table entry`) until a reviewed entry is added (the table
  stays a reviewed literal; additions never touch a registered option's forms, asserted by the
  byte-identical rebuild). **Cutoff options** of adapted items get their forms from one Opus
  FORMS call per item (the option string itself first, then only unambiguous short forms; longer
  names that contain a form but name something else as blockers; case-sensitive forms flagged),
  stored in `model_multihop/forms_cutoff.json` and copied into `mc.forms` at build; the
  shared-form rule, containment and blockers apply unchanged. The audit reviews them (§7).

## 4. Targets, status and freeze

- **Target 100** per set (the caller's brief) in `refill.TARGET_OVERRIDE["model_multihop"]`
  (`adapted_target` returns it for this family only; "never lowered" stays true for every other
  family). Freeze ships every passing item up to the registered size, 283
  (`run.SHIP_CAP["model_multihop"]`), so status is `full` at >= 100.
- **ooc / partial** (skill §3 stop rules): yield = shipped items / B-gated candidates. `ooc`
  when the yield over tiers A + B is below 3% on >= 60 candidates AND the bank is under 50 (half
  the target); otherwise a set under 100 runs tier C (new entities) until it reaches 100 or tier
  C's own yield falls below 3% on >= 60 new candidates, and ships `partial` with that stop
  recorded. Above 100 it is `full`.
- **Selection** (`build`, before freeze, deterministic in pool order with tier A first): one item
  per cutoff descriptor and per folded entity name (near-duplicates, §5); a per-bridge cap of 15%
  of the passing set (answer-class share cap; Qwen's registered bank is at 16% United States),
  applied in pool order, the excess failing with `cap: bridge`.
- `families.REGENERATE["model_multihop"]` (its own stage, like `kcutoff`); `__main__` gains the
  `model_multihop` subcommand (`stage limit llm regate pool`); freeze ships rows as built
  (`origin` original or refill, the item already rebuilt on the subject) and only when named
  (`freeze families=model_multihop`).
- **Header post-freeze** (`mmh.post_freeze`, like nyt_connections'): `subject` = the subject,
  `selection` / `gate` describe this plan's rule, `n_items` / `counts` / `n_invented_bridges` /
  `n_positions` recomputed, `source_frozen*` kept as the pool's provenance with the pool sha256,
  `reference_results` and `decoy_decision` (Qwen's numbers) removed, `version` stays 2,
  `adapt` = {model, hf_id, frozen, plan "0078"}.
- `report.json` `families.model_multihop`: `refill` = `{route: "adapt model_multihop", unit:
  "items", target, have, tried (B-gated candidates), landed, yield, pool_size, pool_used, stop,
  tiers}` and a `note` with the funnel (B-stable, gate, floor miss, review drops, lucky drops).

## 5. Item-quality review (skill §4, #391)

Mechanical, in `build` (every candidate):
1. **No option echo:** no option form of any option in `text` (registered assert, now a fail).
2. **No capital route:** `lint._CAPITAL` / `lint.goes_through_capital` on the read prompt, M2 and
   B, plus no capital city of any option country named in the read prompt (capitals from one
   cached Opus call over the option countries).
3. **Near-duplicates:** one item per folded entity name and per cutoff descriptor; an entity
   whose folded name is a substring of another selected one's is dropped too.
4. **Answer-class cap:** §4.

Judged, in `review` (Opus 5.5, one call per floor-miss item, cached; the item's read prompt, the
entity-first context sentence and the options, never the subject's answers): flags
(a) `shortcut`: the read prompt or the entity name points to one option without the subject's
belief (a national marker, a place name, a language only one option speaks);
(b) `ambiguous_gold`: another option is equally correct for the subject's answer (cutoff: two
options can both be "current"; invented: the hop value of the bridge is shared by an option, a
check identifiability already enforces);
(c) `illegit_options`: an option of a different type from the rest, or a synonym / alias /
part of the gold.
Any flag fails the item (`review: <flag>`), no appeal. The bridge being the most plausible option
is NOT a flag: that is measured by the guessers (§6).

## 6. Lucky-guessing bar (strict) and floors

After `freeze families=model_multihop`, under `WSB_BANK_ROOT=adapted/<m>`:
1. **Screen:** `wsbench baseline kind=lucky_guessing variant=blind,described draws=5
   families=model_multihop out=<run>/lucky_r0` -> `leaks.py` -> every item a guesser hits >= 3/5
   with blind + described >= 4/10 is dropped (`adapt drop ... reason="lucky-guessing leaker
   (plan 0078 strict chance+5)"`). A list cannot be rebuilt here: the foils are the item's own
   candidates and the gate's identifiability legs (A_f) were asked on exactly them, so a rebuilt
   list would be ungated; leakers are dropped and back-filled from the passing pool.
2. Re-freeze; **confirm** with fresh draws (`out=<run>/lucky_r1`, a new dir, so every draw is
   asked again). The family ships only if blind AND described are each <= chance + 0.05 on the
   confirm run (chance = mean 1/|options| over the shipped items, the uniform floor). Leakers in
   the confirm run are dropped and the confirm rate is reported both before and after that drop.
3. At most 3 screen/confirm rounds. If the confirm run still fails, or the set falls below 50,
   stop: the family is not shipped (`note status=blocked`, the numbers in the note and the
   report); never a leaking set.
- **Floors:** stock-subject-v1 (`ADAPT_FLOORS_FAMILIES=model_multihop adapt floors
  stage=generate` then `stage=judge`; the family's native judge at the one remapped layer,
  `opts=aggregate=native`); the lucky floors frozen into `adapted/<m>/floors/` with `wsbench
  freeze ... dst=adapted/<m>/floors/...`; the bench protocol po-opus-v1 waits for #350 (Camila's
  other session). The Opus prompt-only floor is ~0 by construction (invariant 4), as on the
  registered bank; the README footnote applies.

## 7. Audit, docs, PRs

- Per set an independent fresh-context agent appends a `model_multihop` section to
  `adapted/<m>/AUDIT.md`: 20 random items read whole (gate answers, options, forms, read
  spans decoded), the five item-quality checks, the lucky numbers, the funnel; its drops are
  applied and re-frozen before the data PR.
- Docs: `adapted/README.md` row note, `evals/model_multihop/README.md` § Adapted sets (the rule
  above), `capable/questions.py`'s NO_QUESTION reason points to `adapt model_multihop`, skill
  `references/families.md` / SKILL.md table entries, `CLAUDE.md` one line.
- **Native languages:** the registered family is English-only and has no language unit, so the
  lab-language rule (American labs en, Chinese labs en+zh) does not change this family: every
  adapted set is English, like the registered bank (recorded in the README; Camila can ask for zh
  items as a follow-up).
- Code PR (this plan + `adapt/mmh.py` + forms move + pool builder + tests), fresh-eye review, CI,
  gated merge. Then data PRs (batched small / large), each rebased on main; `report.json`
  conflicts are resolved by taking main's file and re-applying only `families.model_multihop`
  (re-running freeze/note for the family if needed), never overwriting the other agents'
  entries. Only `adapted/<m>/model_multihop.json`, `report.json`'s family entry, `floors/`
  records for the family and `AUDIT.md` are committed; work files stay local
  (`/root/tmp/wsb-0078-runs`, rsynced to `/workspace/wsb-runs/0078`).

## Tests (`tests/test_adapt_model_multihop.py`, offline)

Fake runner + fake `ask`: canon / foils / hop rotation / gate checks (each failure reason),
floor miss logic (invalid picks never pass), the regions port (a toy tokenizer: ent / post /
tail), the item builder (bank-v2 shape, no `name`, positions == post, `mc_block` order and
withheld forms), the selection (descriptor dedupe, 15% cap), header post-freeze, the target
override and ship cap, the registered bank rebuild byte-identical through `forms.py`, and the
adapted item passing `missing_required("model_multihop", ...)` and the judge's `dry_run`.
No pre-existing test is modified, except `tests/test_readplan.py`-style registries if a count
moves (none expected).

## Cost estimate

Per subject: Opus canon / attrs / cluster ~$20-40, floor filter (6 calls on each gate pass,
~1-2k passes) ~$60-120, review ~$15, forms ~$5, lucky screens ~$10, stock floors ~$20: about
$150-200 per set, ~$1.5-2k for ten. GPU: small subjects on the shared `adapt-subject-llm`
(H200) ~20-40 min each; the four large subjects on their B300:8 apps ~1-1.5 h each including
boot (two runs: gen1 then gen2 + floors generate, kept warm). At most 4 Modal containers at a
time (shared cap 16 across three agents).

## Files

```
plans/0078-model-multihop-adapted.md
scripts/build_mmh_pool.py
scripts/build_model_multihop_bank.py           (imports the tables from forms.py; output unchanged)
src/wsbench/evals/model_multihop/forms.py       (moved literal tables + mc_block)
src/wsbench/adapt/mmh.py                        (stages, regions port, builder, post_freeze)
src/wsbench/adapt/{__main__,families,refill,run}.py   (subcommand, REGENERATE, pool pin,
                                                 TARGET_OVERRIDE, SHIP_CAP, post_freeze hook)
src/wsbench/capable/questions.py                (reason text)
tests/test_adapt_model_multihop.py
evals/model_multihop/README.md, adapted/README.md, CLAUDE.md, .claude/skills/adapt-bank/...
adapted/<m>/{model_multihop.json, report.json (family entry), floors/, AUDIT.md}   (data PRs)
```

## Revisions after critique round 1 (these replace the sections above where they differ)

R1. **Every pool row comes from the gw cands files**, tier A included. frozen_v2 supplies only
    the id list (tier A) and the `registered` flag: its rows have `pre_text == ""` and no
    `ref_after` / `ref_first` / `sonnet_top5` / `foil_cands`, and their `m` / `m2` are already
    swapped. **Swap convention (stated once):** in a cands row, cutoff `m2` is the single read
    question and `m` the entity-first route; invented rows carry `ref_first` (read) and
    `pre_text` + `ref_after` (entity-first). The builder asserts that convention on every
    registered cutoff id (cands `m2` == frozen `m`, cands `m` == frozen `m2`) and on every
    registered invented id whose hop the subject keeps (re-built read text == frozen `m`).
R2. **Ids.** `cands_cutoff_r2_partial.json` (an interrupted dump of r2's first 57 rows) is
    excluded; the builder asserts unique ids per category. Pool counts: invented 12,070 rows
    (tier A 1,373, B 10,697), cutoff 7,929 rows on 2,382 descriptors (A 198, B 7,731); sha256
    `2adc5f0c…` (deterministic: two builds, same bytes).
R3. **Stop rules fitted to this family's base yield.** Qwen's own end-to-end yield on this
    construction is ~1.4% (283 headline items from ~20k candidates). Yield = shipped items /
    pool rows whose B question was asked (gen1). The family's thresholds: `n_min` = 2,000 rows
    through gen1 and `y_min` = 0.35% (a quarter of Qwen's reference yield). `ooc` = yield below
    `y_min` over tiers A + B AND the set under 50. A set under 100 that is not ooc runs tier C in
    batches of 2,000 new invented entities (about 7 expected items per batch at `y_min`) until it
    reaches 100 or a batch lands under `y_min`; then `partial` with that stop. Tier C is
    implemented in a follow-up code PR only if a subject needs it.
R4. **Selection moves to freeze; `gate.pass` is purely per-item.** `build` records every
    per-item pass/fail (gate, floor, review, forms, regions, echo, capital) and never fails a row
    for set-level reasons. Freeze's `run.select` gets a model_multihop branch
    (`mmh.select_rows`) over the LIVE passing rows (originals included, so drops back-fill):
    lucky-screen leakers out (R5), one item per folded entity name / cutoff descriptor (an
    entity whose folded name is inside a kept one's, or the reverse, is a near-duplicate), then
    rows in ascending lucky-screen hits (ties: pool order, tier A first) up to the ship cap with
    at most floor(0.15 x the shipped size) items on one bridge, computed on the shipped set.
R5. **Lucky-guessing procedure (no circular decisions).**
    1. *Pre-selection screen:* `build` also writes every per-item pass as a bank-shaped file
       `mmh/candidates.json`; stage `lucky` runs `wsbench baseline kind=lucky_guessing
       variant=blind,described draws=5 families=model_multihop` with
       `WSB_BANK_FILE_MODEL_MULTIHOP=<that file>` and stores per-item hits in
       `mmh/lucky_screen.json`. A screen leaker (a guesser >= 3/5 AND blind + described >= 4/10)
       never ships; selection prefers low-hit items (R4).
    2. Freeze, the independent audit, its drops and the re-freeze come BEFORE the confirm run.
    3. *Confirm* with fresh draws on the frozen bank (`WSB_BANK_ROOT`, a new out dir). **The ship
       decision uses the confirm rate as measured, before any drop:** blind AND described each
       <= chance + 0.05 (chance = mean 1/|options| over the shipped items). Confirm leakers are
       dropped afterwards, and any drop after the confirm needs another fresh confirm round
       whose pre-drop rate decides.
    4. If a confirm round fails, the next screen round tightens the screen rule (round 2:
       blind + described >= 3/10 excluded; round 3: >= 2/10), re-selects, re-freezes and confirms
       fresh. At most 3 rounds; still failing, or under 50 items: not shipped
       (`note status=blocked` with the numbers). The diffuse excess the registered bank shows
       (blind 0.278 vs 0.196) is exactly what the screen-ranked selection targets; it is measured,
       not assumed away.
    5. The frozen lucky floor and the stock-subject floors are measured on the final shipped
       bank, after the last re-freeze (stock floors re-run with `ADAPT_FLOORS_MISSING_ONLY=1` if
       the bank changed after they were generated).
R6. **Review flags redefined around the latent (the subject's belief is the gold).** The review
    is shown the gold (the subject's bridge) explicitly. (a) `shortcut` fires only when the
    question text itself states the bridge or lets one derive the bridge or the hop answer
    without any belief (a place, landmark or marker inside the bridge's territory only; the hop
    answer derivable from the text); a name that merely sounds like a foil is NOT a flag (the
    subject's divergent belief is the latent), and plausibility toward the bridge is measured by
    the 0/6 Opus floor and the guessers. (b) `ambiguous` fires only when two options are names
    of the same thing, or co-holders of the role at once; a foil that is the TRUE current holder
    while the subject believes a stale one is the family's latent, never a flag (gw prefers
    bridge != truth). (c) `illegit_options` as before; (d) `capital` as before.
R7. **Cutoff forms are reviewed and mechanically restricted.** Each Opus form must be the option
    string or a contiguous word subsequence of it after folding punctuation and spaces
    ("RTX 3050" from "GeForce RTX 3050", "EOS R5" from "Canon EOS-R5"); any other form is
    dropped. An option whose string is a common English word gets its option form
    case-checked. The independent audit reviews the forms and blockers of EVERY shipped cutoff
    item (few per set), not a sample.
R8. **Native languages: an open product question, surfaced, not settled.** The registered family
    is English only and has no language unit; this round builds English items for all ten sets.
    Whether Chinese-lab subjects (GLM, DeepSeek, Kimi, the five Qwen sets) should also get zh
    items (zh prompts gated in zh, the same floor and lucky bar; options and forms stay English
    because readouts are judged in English) is flagged to Camila in the report and the PR body
    as a deviation from the lab-language rule, with the en sets shipped meanwhile.
R9. **Header provenance:** freeze rebuilds `adapt` after the hook, so the plan id goes into
    `selection` / `source` (no `adapt.plan` key).

## Revisions after critique round 2 (these replace R3-R7 where they differ)

R10. **The confirm round that passes is final.** The ship decision is the confirm run's pre-drop
     rate (blind AND described each <= chance + 0.05). Its per-item leakers are then dropped WITHOUT
     back-fill: `lucky_screen.json` records the confirmed id set and the confirm hits, and
     `select_rows` then restricts selection to confirmed ids minus confirm leakers (no new item
     enters). Both rates (pre-drop: decides; post-drop: reported) go in the note. A new confirm
     is needed only when an item is ADDED (e.g. after an audit drop no item is added, so no new
     confirm; the audit's drops shrink the set).
R11. **Rounds 2-3 re-select on pooled draws, not on tighter noisy cuts.** After a failed confirm
     the screen and confirm draws are pooled (10 per guesser per item) and an item is excluded
     when either guesser's pooled hits are significantly above chance + 0.05 (one-sided binomial,
     p < 0.10, against 1/|options| + 0.05); the rest are re-selected in ascending pooled hits and
     confirmed on fresh draws. No fixed >= 3/10 or >= 2/10 cut.
R12. **No "< 50 = blocked" rule.** R3 decides full / partial / ooc on yield. `blocked` is used
     only when the lucky bar fails after 3 rounds (the numbers in the note).
R13. **Every identifying hop is asked; the first that passes the full gate is used.** gen2 asks
     M, M2, A (20 samples) and A_f (greedy) for every identifying hop of an invented item (crc32
     rotation order, hops whose bridge value is among the foils' values are skipped). The gate is
     run per hop and the first hop, in rotation order, whose B/M/M2/A shares, chain,
     identifiability and leak checks all pass is the item's hop (`hop_tried` records the rest).
     Each hop's item passes the unrelaxed gate on its own; only which hop is used is chosen, so
     a subject's yield measures its capability, not the rotation.
R14. **No hop whose answer names the bridge.** A hop whose true bridge value names the bridge by a
     form of `forms.raw_forms(bridge)` (ISO-3 "USA" for the United States, "UAE dirham") is not
     identifying (skipped), and an item whose built `answer_names_bridge` is still true fails
     (`answer names the bridge`). The registered bank kept such items only for gw parity, which
     the adapted sets do not have.
R15. **An options-only legitimacy check.** A second Opus call per item sees ONLY the options (no
     question, no entity) and the gold, and flags `gold_stands_out`: the gold is the odd one out
     on its own (the only famous country, the only one outside the others' region, a different
     type, length or spelling pattern). A flag fails the item (skill §4.1). The REVIEW call's
     "do not flag a surprising or less plausible gold" instruction is kept (it is about the
     question, not the list).
R16. **Tests and minor points.** The offline test recomputes the order-independent `mc` fields
     (forms, blockers, blocker_of, exact_case) of every registered item from items.json through
     `forms.py` and compares them; the full byte-identical rebuild needs gw's frozen_v2 (not in the
     repo) and is a box-only check run once and recorded in the PR (done: identical).
     Cutoff B prefilter (cost control, not in gw): a B whose top NORMALISED answer has under
     5/20 samples is not clustered (clustering only merges aliases; a mode at >= 16/20 with its
     most common spelling under 5/20 is implausible); its rows fail `b_share (prefilter)`.
     The lucky screen always runs on the FULL candidates bank (the described guesser's examples
     come from the whole bank); ids screened earlier keep their first hits.

**Sanctioned pre-existing test change (the only one):** `tests/test_adapt_refill.py::
test_pool_dir_default_and_no_pin_yet` asserts the exact set of pool names
(on main after plan 0077: `{"msdm", "kcutoff", "mh2", "mh2v2", "kcutoffv2", "mh2v3"}`); it gains
`"mmh"`, the pool this plan pins. The test's intent
(every `POOL_FILES` name is pinned in `POOL_SHA256`, and the default pool dir) is unchanged.

## Revisions after critique round 3 (supersede R4, R5, R10, R11 where they differ)

R17. **Every stage runs with `ADAPT_BACKEND=modal`**, the five box subjects (qwen3.5-9b/4b/2b/0.8b,
     olmo-3-7b) included: the shipped `input_ids` must be the exact ids the gate sent, which only
     `RemoteRunner.prompt_ids` gives. `mmh.subject_runner` refuses a runner without `prompt_ids`;
     gen1 / gen2 record `_meta.backend` (runner class + Modal app) and `build` refuses when gen1,
     gen2 and its own runner differ.
R18. **The stop rule is implemented as stage `status`**, run after the last freeze: yield = shipped
     items / pool rows whose B was asked; `full` at >= 100; `ooc` when yield < `Y_MIN` (0.35%) on
     >= `N_MIN` (2,000) rows AND under 50; else `partial` with its stop. It writes the report's
     `refill` block `{route, unit, target, have, tried, landed, yield, n_min, y_min, stop, tiers,
     pool_size}` and the status.
R19. **Pooled rounds are never looser than round 1** (a round-1 screen leaker stays out; the pooled
     binomial test is added on top), and ranking uses hits per draw, not raw counts.
R20. **Round 1 does not rank by hits**: after dropping screen leakers, near-duplicates and the
     bridge cap, eligible items ship in pool order (tier A first) up to the cap. Ascending-rate
     ranking is used only in an R11 round after a failed confirm. The confirm record flags a rate
     far below chance (< chance - 0.05; reported, never deciding), and the data PR reports the
     shipped set's gold-slot distribution.
R21. **A confirm must be fresh and cover the frozen bank:** `confirm` refuses an out dir used before
     (or a screen dir), refuses a run whose judged ids differ from `adapted/<m>/model_multihop.json`,
     and records the bank's sha256.
R22. **The bridge cap holds on the final path too:** after confirm-leaker (or audit) drops,
     over-cap bridges are trimmed (highest hit rate first); trimming only removes items, so no new
     confirm is needed (R10).
R23. **Floor answers that map to no candidate are never cached as valid** (`valid` = the pick maps
     to a candidate), so they are re-asked on resume.
R24. **Audit before confirm covers back-fill:** if the audit's drops make freeze back-fill (the
     non-final path), the back-filled ids are audited (cutoff forms and blockers included, R7)
     before the confirm run.

## Revisions after critique round 4 (supersede R10, R11, R5.5, R17 where they differ)

R25. **Multiple testing on the confirm.** A failed confirm can never be re-rolled: `confirm` refuses
     while the last confirm failed and the round has not advanced (`stage=lucky level=<round+1>`
     starts an R11 round). In an R11 round the selection is materially different: after the
     pooled exclusions and the bridge cap, the ascending pooled-rate list is truncated to its
     longest prefix whose POOLED observed blind and described rates are each <= that prefix's
     chance + 0.05 (`mmh.bar_prefix`); no such prefix = `blocked` (never re-confirmed unchanged).
     The new set is then confirmed on fresh draws. After 3 failed fresh confirms `confirm` writes
     `status=blocked` with the numbers. So at most 3 fresh tests are ever run per set, each on a
     different set, and the bar is never relaxed.
R26. **Cutoff near-duplicates:** one cutoff item per (subject bridge, answer type), in selection
     order, besides the whole-word entity containment (paraphrased roles such as two wordings of
     "Samsung's current flagship Exynos chip" ask the same latent).
R27. **No backend auto-stamp.** An unstamped generation file reads `unknown (pre-R17)` and `build`
     refuses it. The qwen3-8b / olmo-3-7b gen1 files were stamped by an earlier draft; their
     provenance is verified independently: this box has no GPU (the box `Runner` cannot run here)
     and every step ran through `step.sh`, which exports `ADAPT_BACKEND=modal`, so both were
     generated by `RemoteRunner` on `adapt-subject-llm`.
R28. **The frozen lucky floor is a fresh run.** After the last re-freeze, `wsbench baseline
     kind=lucky_guessing variant=blind,described,uniform draws=5` runs into a NEW out dir and is
     frozen into `adapted/<m>/floors/`; the confirm's post-drop rate is a reported diagnostic only.
     Round-1 order is "registered ids first, then pool order" (freeze's select hook puts
     originals first); the plan's earlier "tier A first" wording means this.

## Revisions after critique round 5 (supersede R20, R25, R27, R28 where they differ)

R29. **An explicit ship order** (`mmh.ship_order`, no reliance on a select hook, which this family
     does not have): registered ids first in pool order, then the rest with one cutoff row after
     every 9 invented rows while both last. Pool order alone would put every cutoff row after
     every invented row, so a subject with more than 283 passes would ship no cutoff item; with
     the interleave a capped set keeps about the registered cutoff share (28 / 283).
R30. **In a pooled round the bar prefix and the bridge cap alternate until neither changes the
     set** (each step only removes items), so the shipped set meets both the pooled bar and the
     15% cap computed on its own size.
R31. **Backend stamps are written only on an empty generation file.** A file with rows but no stamp,
     or a stamp that differs from the current runner's, is refused (never re-stamped), so a
     resumed file cannot hide mixed provenance.
R32. **The empty-prefix stop is recorded:** before freeze aborts, `families.model_multihop` gets
     `status: blocked` and the note, as on the 3-failed-confirms path.

## Revisions after critique round 6 (supersede R25 / R32 where they differ)

R33. **Each confirm tests a different set.** A confirm records its id set; `confirm` refuses a
     frozen bank whose id set equals any earlier failed confirm's, and a pooled round whose
     selection equals a failed set takes the empty-prefix stop (blocked). The round counter is
     no longer the only guard.
R34. **Blocked means not shipped.** On either block path the frozen `adapted/<m>/model_multihop.json`
     is moved into the work dir (`mmh/blocked_bank.json`) and the report entry gets `status:
     blocked`, `n_items: 0`, `bank: null` and the note; `select_rows` (so freeze) refuses a blocked
     family; `status` keeps `blocked` and refuses to grade any set as `full` / `partial` unless a
     passed confirm covers the frozen bank (its ids within `final.ids` minus `final.leakers`).

## Revisions after critique round 7 (supersede R12, R18, R34 where they differ)

R35. **Freeze never grades this family.** A frozen bank that no passed confirm covers is written
     `status: unconfirmed`, `exclude_from_macro: true` (freeze's model_multihop hook,
     `mmh.freeze_status`); a failed confirm that does not block writes the same with its rates.
     Only `status` writes `full` / `partial` / `ooc`, and only for a covered bank.
R36. **A confirm must show the set does not leak.** It requires the standard run (blind and
     described, exactly 5 draws per item; otherwise refused) and passes when each guesser's
     rate is <= chance + 0.05 AND, when a guesser has fewer than 500 draws (a set under 100
     items), its one-sided 90% upper bound (normal approximation) is also <= chance + 0.05.
     This only tightens the bar for small sets.
R37. **No `partial` before the refill is exhausted.** Under 100 and not `ooc`: while the pool is not
     fully gated or tier C has not run, `status` writes `refill_pending` (tier C then runs, in a
     follow-up code PR if a subject needs it); `partial` only after tiers A-C, with tier C's
     batch under `Y_MIN`.
R38. **A confirmed set cannot change unseen:** the passed confirm records each item's hash of
     (`mc.order`, `mc.gold`); the final path and `status` refuse any shipped item whose hash
     differs (a patch or rebuild after the confirm needs a new selection and confirm).

## Revisions after critique round 8 (supersede R35, R36, R18 where they differ)

R39. **A covered bank rejoins the macro.** The `unconfirmed` marking carries its own marker
     (`mmh_unconfirmed`); when a later freeze or `status` finds the bank covered by a passed
     confirm, it removes the marker, `exclude_from_macro` and the failed-confirm note it wrote.
R40. **Rounds 2-3 always need the upper bound:** in an R11 round the confirm passes only when each
     guesser's one-sided 90% upper bound is also within chance + 0.05 (round 1 keeps R36: the
     upper bound only under 500 draws). This only tightens the later, more-selected rounds.
R41. **An incapable subject reaches `ooc` without a confirm.** When fewer than 4 items pass per item
     (the guessers cannot run, nothing is confirmed), `status` applies the yield rule first:
     yield < `Y_MIN` on >= `N_MIN` rows gives `ooc` (any bank moved aside, `n_items: 0`). A set
     with >= 4 items keeps the confirm requirement, so a tiny set that fails its bar is `blocked`
     (leakage) and an incapable one is `ooc` (capability), never confused.

## Revisions after critique round 9 (supersede R28 where they differ)

R42. **The floor of record is a re-test of the shipped set.** After a passed confirm, any further
     confirm whose ids are the shipped set (the R28 fresh lucky-floor run is ingested this way,
     `stage=confirm src=<floor dir>`) is a re-test: a pass keeps `final`; a failure removes
     `final`, writes `unconfirmed`, counts as a failed confirm, and the set needs an R11 round.
     `status` grades only while `final` stands, so a set whose fresh floor of record is above
     chance + 0.05 cannot ship. **Amended by R49 (2026-11-07):** the per-item leakers of a
     passed floor of record are dropped once (no back-fill), followed by a single re-measure that
     becomes the new floor of record, whose own per-item flags are reported, not dropped; the
     strict set bar (blind AND described each <= chance + 0.05) still decides that re-measure,
     and the Opus prompt-only floor stays at 0 on every shipped item.
R43. **A confirm must have measured the frozen bank's options:** for every item, the run's gold
     slot, option count and option strings (in its prompt) must match the frozen bank, or the
     confirm is refused (so a patch between the run and its ingestion cannot be hashed as
     confirmed).
R44. **Only live draws count:** a confirm with any failed (None) guesser draw is refused (re-run
     into a new dir), and a screen item with a failed draw stays unscreened (excluded), so every
     count used by the screen, the pooled test, the prefix and the upper bound rests on 5 live
     draws per item per run.

R45. **Floor / review budget (cost, not selection).** The Opus floor (6 calls per item) and the
     reviews run over the gate passes in the SHIP order (`ship_order`: registered ids first, then
     invented rows with one cutoff row after every 9), in chunks of 400, and stop once
     3 x 283 = 849 items clear the floor and every judged review flag. Gate passes after that
     point are not asked (`floor: not asked (R45 budget reached)`). Freeze selects in the same
     order, so the budget never changes which items would ship; it bounds the Opus spend for
     strong subjects (qwen3-8b passes B on ~13k rows). The yield reported for a set that reached
     the budget is a lower bound, which matters only below the target, where the budget cannot
     bind.

## Revision after critique round 10 (supersedes R36 / R40's bound)

R46. **The confirm's upper bound is item-level.** An item's 5 draws share one prompt and are
     correlated, so the bound uses per-item rates p_i = hits_i / 5: upper = mean(p_i) + 1.2816 x
     sd(p_i) / sqrt(N_items) (one-sided 90%), recorded as `se_item`. It applies to sets under 100
     items and to every R11 round (R36 / R40, thresholds now in items).

R47. **Invalid guesser picks are not misses.** An out-of-range or non-integer pick (`n_invalid` on
     a draw) is treated like a failed draw (R44): a confirm with any is refused (re-run into a
     new dir) and a screen item with any stays unscreened, so no rate is lowered by unusable
     answers. (Critique round 11; no other substantive issue was found.)

## Revisions after the code review (PR #394)

R48. **A covered bank gets this family's grade at freeze too** (`mmh.grade`, shared with `status`):
     freeze's generic full / partial never stands for model_multihop (an audit drop below 100
     re-freezes as `refill_pending`, not `partial`).
- Resume safety: a cached generation answer counts only for the identical question text, a gate
  verdict carries a fingerprint of the canon1 state it used (bridge, foils, hop questions) and is
  re-gated when that changes, an item is gated only when every M / M2 / A / A_f answer is present,
  and a failed gate CLUSTER call leaves the item ungated (retried) instead of failing it.
- An incapable subject (no item gets a hop) still writes a stamped, empty gen2 file, so `build` and
  `status` run through to R41's `ooc`.
- `scripts/build_mmh_pool.py` asserts R1's swap convention on all 283 registered items (the pool
  bytes, and its pinned sha256, are unchanged).
- Items record `pool_tier`; `hop_changed` (plan §3) is not written (the hop is the subject's own,
  from R13's rule; Qwen's hop is irrelevant to the adapted item).
- Work files live in `adapted/<m>/mmh/` (plan §2's `model_multihop/` was renamed: that name is the
  judge overlay directory).
- An audit `adapt patch` that changes an item's `mc` after its confirm makes the next freeze refuse
  (R38); such a fix needs a new selection and confirm.

- **Accepted deviations (code review round 5):** every Claude call, the Opus floor included, runs
  with a 12000-token cap (gw used 1500 for floors; a higher cap only avoids truncation, never
  changes a valid pick). The R45 budget counts items that clear the floor and the judged reviews;
  later mechanical checks, the lucky screen, dedupe and the bridge cap remove more, so with 849
  cleared it binds only for subjects whose shipped set would reach the 283 cap anyway, and the
  budget's stop is recorded (`mmh/floor_budget.json`).

## Follow-up (Camila 2026-11-07: "strict rule: drop the flagged questions"; supersedes R42 / R22 where they differ)

R49. **Floor-of-record leakers are dropped once, then the set is re-measured once.** R42 kept the
     per-item leakers of a passed floor-of-record re-test; they are now dropped. For every shipped
     set (full, refill_pending, partial, and an ooc set that has any), every per-item leaker
     (`leaker()`: a guesser >= 3/5 AND blind + described >= 4/10) of its floor of record (the
     last re-test, run on exactly the frozen ids) is dropped with `adapt drop` (reason
     `floor-of-record lucky leaker (plan 0078 R49 ...)`), WITHOUT back-fill (R10: nothing that a
     passed confirm did not cover enters), and the set is re-frozen (the final path: the bridge
     cap re-applied by dropping only). Then ONE fresh lucky run (blind, described, uniform; a new
     out dir) is ingested with `stage=confirm` as the new floor of record and frozen into
     `floors/`; its own per-item leakers are reported in the note and NOT dropped (no further
     drop / re-measure cycle: each re-test re-draws noise and flags new borderline items, so
     iterating never converges). **The retained invariants hold unchanged on that re-measure:**
     the set ships only while its fresh floor of record passes the STRICT set bar (blind AND
     described each <= chance + 0.05, plus the item-level 90% upper bound under 100 items, R46);
     a failure removes `final` exactly as R42 says (`unconfirmed`, out of the macro; an R11 round
     is then needed), and the Opus prompt-only floor stays 0 by construction (invariant 4: every
     shipped item still has 0 of 6 Opus picks on the bridge; dropping items cannot change that).
     A set that falls below 100 items is `refill_pending` (R37; the target is never lowered).
     The stock-subject floors are re-judged on the smaller bank (`ADAPT_FLOORS_MISSING_ONLY=1`:
     the kept ids' stored rows, nothing generated).
R50. **Code follow-ups.** (a) `status` (and freeze's R48 grade) writes `exclude_from_macro: true`
     with an `ooc` grade (marker `mmh_ooc`; a later non-ooc grade removes only that exclusion),
     instead of relying on `adapt note`. (b) R22 holds wherever the bridge cap trims, round-1
     selection and audit re-freezes included: an over-cap bridge keeps its lowest-hit-rate items
     among those the ship-order scan reached (ties: ship order) and its highest-rate items go
     first; the kept set is still shipped in ship order and other bridges are untouched, so round
     1 still does not rank the set by hits (R20). (c) Identifiability compares calling codes at
     the prefix level: a bridge code that is a prefix of a foil's code, or the reverse (+1 vs
     Jamaica's +1-876), is not identifying (canon1's hop choice and the gate's check against the
     foils' true values and the A_f answers; invented items only); every other hop, and every cutoff
     item (whose values are cluster labels), compares exact values as before. An exclusion
     written by `adapt note` before an `ooc` grade is never claimed by the `mmh_ooc` marker. A
     scan of the ten shipped banks finds no shipped item this check would fail (the two NANP cases
     were dropped by the #407 audit).
