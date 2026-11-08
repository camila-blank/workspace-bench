# 0085: freeze the final strict-ATI subset for Qwen3.6-27B (8 families)

Issue #446. Camila, 2026-11-08: the strict anti-text-inversion (ATI) subset must be "totally
frozen on the public repo", so that running the bench produces exactly the subset the plots use.

## The subset

| family | ATI ids | why it is in |
|---|---|---|
| introspection | 124 | panel-v2 entry, n >= 20 |
| moral_rationale | 56 | panel-v2 entry, n >= 20 |
| wrong_intermediate | 42 | panel-v2 entry, n >= 20 |
| knowledge_cutoff | 39 | panel-v2 entry, n >= 20 |
| conjunctive_association | 26 | panel-v2 entry, n >= 20 |
| model_specific_directed_modulation | 25 | panel-v2 entry, n >= 20 |
| model_multihop | 22 | pinned panel-v2 entry (§2); always in (§3) |
| poetry | 16 | panel-v2 entry; explicit exception to n >= 20 (§3) |

Decisions (Camila, 2026-11-08):

1. **nyt_connections is out.** Its survivors are per-slot labels of Qwen's own groupings, which
   are model-specific. Admitting it was an exception she no longer wants.
2. **hinted_cutoff is out.** It is pair-scored and the hint is in both twins, so its subset score
   is noise: the union flag rate is about 0.75 on both twins, and the mean is -0.03 +/- 0.15
   across configs. It stays in the full set.
3. **poetry is in**, as an explicit exception to the n >= 20 minimum (16 survivors), the way
   model_multihop is always included.
4. Nothing else changes. Survivors are not re-derived, the panel and its rule (`panel-v2`,
   all-readers-fail, plan 0071) are unchanged, and no bank item is touched. No other family enters
   or leaves the subset. jailbreak_recognition (8), user_modeling (4), role_bound_association (3)
   and basic_readout (1) stay shown but out of the macro, under n >= 20.

## What main does today, and the gap

- The repo macro (`results.macro`) admits an ATI-scoped row iff its gated ids reach
  `ATI_MACRO_MIN_ITEMS = 20`. On main's frozen file that is the 6 gated families above. poetry
  (16) is excluded as `n<20 (ATI)`.
- `model_multihop`, `nyt_connections` and `hinted_cutoff` are `NOT_GATED`
  (`text_inversion.NOT_GATED`, mirrored in the file's `_not_gated`). `anti_text_inversion=True`
  therefore skips all three (`NotInScope`).
- The analysis that drew the plots (box-local `qmain/tools/compute.py`) added nyt (18 ids), mmh
  (22) and hinted_cutoff v2 (27) by hand from box-local id files. Only mmh survives into the final
  subset. Its 22 ids come from a 2026-11-07 four-reader panel run on the registered bank
  (`final64/ati4/mmh_extra`, base SHA 64fef706): Qwen, Opus, astra and argon, 3 draws each, asked
  with a family-specific answer form `mmh_bridge`, judged by the family's own instrument, and kept
  iff every reader failed every draw. That run's template, ids and statuses never reached the repo.

Verified before writing this plan (no API calls): `text_inversion._gate_panel` on main, run over
that run's stored `judged/<reader>/draw{d}` with the `mmh_bridge` template registered, returns
exactly the 22 ids in the same order. It reports `n_bank` 283, `n_invertible` 261,
`n_unverified` 0, `instrument` `mmh-2026-11-06+mmh-forms-v3+bank-v2` and `bank_sha` `b0ec05ce...`.
Both of the last two equal main's current values; the bank is unchanged since 64fef706.

## 1. The `mmh_bridge` answer form (`text_inversion.FAMILY_PANEL_TEMPLATES`, new)

`PANEL_TEMPLATES` stays exactly `{"free", "guess"}`. `test_variants_share_the_open_model_note_and_set_the_prompt`
pins that, and `ati build variant=` keeps accepting only the generic forms. Add a separate
`FAMILY_PANEL_TEMPLATES: dict[str, str] = {"mmh_bridge": ...}`, verbatim from the 2026-11-07 run:
`OPEN_MODEL_NOTE + _PANEL_BODY + " Answering the question needs one hidden intermediate step
first: which specific country, product, person or organisation the question's subject resolves
to. Give your single best guess of that intermediate the model has in mind, in one sentence."`

`panel_prompt(row)` and `panel_source()`'s `templates` both look a variant up in
`{**PANEL_TEMPLATES, **FAMILY_PANEL_TEMPLATES}`. Otherwise a gate whose prompts.jsonl holds
`mmh_bridge` rows raises `KeyError`.
It must, because `reader_routes` / `current_shas` re-hash each stored prompt through
`panel_prompt`, and an unknown variant raises `KeyError`. `DEFAULT_VARIANT`, `PANEL_TEMPLATE` and
`PANEL_TEMPLATES` are unchanged. `panel_source()` writes `"family_templates":
FAMILY_PANEL_TEMPLATES`, and the frozen file's `_source` gains the same key, so the file carries
the mmh question text and a full re-gate rewrites it. The template is added verbatim to
`evals/baselines/README.md`, on one unwrapped line in a fenced block, and a new test asserts that
every `FAMILY_PANEL_TEMPLATES` value is there. `gate_adapted` drops `family_templates` and
`subset_version` from the `_source` it builds from `panel_source()`. Both are Qwen3.6-27B's, not an
adapted set's.

## 2. model_multihop: a pinned panel entry

`model_multihop` **stays in `NOT_GATED`**. The generic gate (`gated_families`, `gate`,
`gate_adapted`) still never asks or gates it, because its generic `guess` question does not fit
the bank. Its `NOT_GATED` reason is rewritten in code and in the file's `_not_gated` (they must stay
equal; `tests/test_model_multihop.py::test_touchpoints` and `ati_rederive` compare them). The new
reason says that the generic gate does not gate it, and that its strict-ATI entry is pinned from
the 2026-11-07 family-specific panel run (`mmh_bridge`, plan 0085).

New `text_inversion.PINNED_ENTRIES: dict[str, str] = {"model_multihop": <provenance>}`. These are
families whose frozen entry in the **registered** file (`FROZEN`) comes from a family-specific
panel run outside the generic gate. Each entry is a normal `panel-v2` entry with per-item
`status` and `draw_pass`, so `ati_rederive.py --check` re-derives it like any other.

- `subset_entry(family)`: for a `PINNED_ENTRIES` family, if `dst` holds an entry for it, that entry
  is used. The not-gated check is skipped for it. That check is the MERGED one, `NOT_GATED` plus
  the file's `_not_gated`, and both still list mmh. **Every other check is retained unchanged**:
  instrument, retired gate, `bank_sha` (it is a `BANK_SHA_GATES` entry), empty ids and
  `ati_reportable`. The entry is now looked up before the not-gated check, which changes nothing
  for a non-pinned family. With no entry (any adapted set: none has an mmh entry), the `NOT_GATED`
  path applies as today and raises `NotInScope`, so adapted-set behaviour is unchanged.
- `gate()` full rewrite (no `families`): a `PINNED_ENTRIES` family's prior entry in `dst` is
  carried over unchanged, like a superseded or construction-gated one. Otherwise a full re-gate
  would silently drop mmh. `gate(families=[...model_multihop])` still raises (`NOT_GATED`).
  `gate_adapted` is unchanged and still excludes it.
- The entry is written into `evals/baselines/anti_text_inversion.json` exactly as `_gate_panel`
  returns it from the stored draws (above), plus two fields: `"variant": "mmh_bridge"` and
  `"pinned"` (one line naming the run date, base SHA 64fef706, the 4 readers x 3 draws, and plan
  0085). `scripts/ati_rederive.py --check` must pass on the new file. The entry must be a full
  four-reader panel entry: `panel` keys qwen / claude / astra / argon, with no `provisional` or
  `status_order` (`test_ati_panel.py::test_frozen_argon_decisive_counts_match_the_statuses`). The bulky raw run (answers,
  readouts, judged rows) stays box-local; the entry carries the per-item, per-reader, per-draw
  verdicts the subset rests on.
- Re-gating mmh again is a family-specific procedure (ask with `variant=mmh_bridge`; readouts with
  a `prompt_only:<model>` reader sidecar per readout file; native judge;
  `_gate_panel(families=["model_multihop"])`). It is out of scope here and stated as not frozen in
  the README.

## 3. Macro admission (`results.macro`)

New `results.ATI_MACRO_EXCEPTIONS: dict[str, str]` = `{"poetry": <Camila 2026-11-08, 16
survivors>, "model_multihop": <always included, plan 0085>}`.

The `n<20 (ATI)` clause becomes: an ATI-scoped row is excluded as `n<20 (ATI)` iff
`min(ati_n_ids, n_items) < ATI_MACRO_MIN_ITEMS` **and it is not an exception row**. An exception
row is one whose family is in `ATI_MACRO_EXCEPTIONS`, whose `extras.ati_gate` is
`text_inversion.GATE_VERSION` (the registered Qwen3.6-27B panel, not `adapted-panel-v1`), and that
judged every gated id (`n_items >= ati_n_ids`, so a `limit=` smoke run stays out). "Always in" for
model_multihop (22 >= 20 today) means in at any size under those two conditions. **Retained
unchanged:** `ATI_MACRO_MIN_ITEMS = 20`, counting gated ids rather than judged rows (jailbreak,
plan 0071 §1a), the smoke-run guard for non-exception rows, the `stale ATI gate` clause and its
order, and every other macro clause. On an adapted set an n < 20 poetry row stays out.

`nyt_connections` and `hinted_cutoff` stay `NOT_GATED`, with no entry, so they never produce an
ATI-scoped row. Their `NOT_GATED` reasons gain the 2026-11-08 decision (code and file, kept
equal). Their gating mechanism is not otherwise changed. hinted_cutoff on the adapted sets (open
PR #445) is untouched; #445 may touch the same `NOT_GATED` line, and whichever merges second
rebases.

## 4. The subset version stamp

New `text_inversion.SUBSET_VERSION = "ati-subset-v2"`. v1 is the plan-0071 macro (n >= 20 only);
v2 is this plan (n >= 20, plus poetry and model_multihop, with nyt_connections and hinted_cutoff
out).

- `panel_source()` writes `"subset_version": SUBSET_VERSION`. The frozen file's `_source` gains
  the same key (`ati_rederive` keeps unknown `_source` keys).
- The runner stamps `result.extras["ati_subset"] = text_inversion.SUBSET_VERSION` on every
  ATI-scoped result whose `ati_gate` is `GATE_VERSION`, beside `ati_gate`. Adapted rows are not
  stamped, because the definition is Qwen3.6-27B's.
- `macro()` excludes a registered-panel (`GATE_VERSION`) ATI row whose `extras.ati_subset` is
  **present and different** (an adapted row is never checked),
  reason `stale ATI subset (<v>)`, checked right after `stale ATI gate`. A row with no stamp is a
  pre-0085 row of the same gate: its per-family value does not depend on the macro rule, so it is
  admitted under the current rule. When any ATI-scoped row is present, `macro()` returns
  `"ati_subset": SUBSET_VERSION`, so a report says which definition its ATI macro used.

## 5. Freeze tests (`tests/test_ati_freeze.py`, new)

- The tests run with `WSB_BANK_ROOT` unset (autouse). Synthetic rows are complete and
  `pinned_instrument`, with metric `pass_rate` and a value, so only the ATI clauses decide. The
  test fails whenever a family's instrument or bank moves (`subset_entry` exits "re-gate"). That is
  intended: the freeze is re-checked.
- **The admitted set is exactly the 8.** For every family in `registry.FAMILIES`, take what the
  runner takes: call `subset_entry`, count `NotInScope` as skipped, and let any other exception
  fail the test. Build one synthetic ATI row per returned entry (n_items = ati_n_ids =
  len(ids), `ati_gate` = the entry's gate, `ati_subset` stamped) and run it through `macro()` with
  the real registry exclusions. Its `families` must equal the literal set of 8. This catches nyt
  or hc coming back by any path.
- `subset_entry("model_multihop")["ids"]` has the 22 ids. nyt_connections and hinted_cutoff raise
  `NotInScope`, and neither has an entry in the file.
- poetry with 16 gated ids is admitted on `panel-v2`, excluded on `adapted-panel-v1`, and
  excluded as a smoke run (`n_items` 3 < `ati_n_ids` 16).
- A pinned family with no entry (tmp file) raises `NotInScope`, and an entry with a stale
  `bank_sha` still fails. A full `gate()` carries a pinned entry over.
- `macro()` excludes a row stamped with another `ati_subset`, admits an unstamped one, and returns
  `ati_subset`.
- An adapted row stamped with another version is not excluded, and `gate_adapted`'s `_source`
  has no `subset_version` and no `family_templates` (`tests/test_adapted_ati.py`, a new test).
- `"subset_version"` in the file's `_source` equals `SUBSET_VERSION`, and `_source.family_templates`
  equals `FAMILY_PANEL_TEMPLATES`. Every `FAMILY_PANEL_TEMPLATES` value appears verbatim in the
  baselines README.

No pre-existing test assertion is expected to change. If one must change, it is allowed only
where it directly encodes the old admitted set (nyt/hc in, poetry out), and it is listed in the PR.

## 6. Docs

These state the 8 families and the reasons: `README.md` (the ATI paragraph), `CLAUDE.md` (the
macro sentence), `evals/baselines/README.md` (the subset section, the `mmh_bridge` template
verbatim, and the pinned-entry note), `evals/poetry/README.md` (in by exception, no longer "out of
the macro"), `evals/model_multihop/README.md`, `evals/nyt_connections/README.md`, and
`evals/hinted_cutoff/README.md` (out of strict ATI, and why). Also the `plans/README.md` index.
Adapted-set docs and adapted/ data are not touched.

## 7. After merge (box-local, $0)

The qmain `values.json` strict-ATI macro is re-scored cache-only with qmain's tooling, and
`sets.strict_ati.macros.admitted` becomes the 8. That step is not part of this PR.
