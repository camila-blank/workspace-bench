# 0101: the multi-token families take the plain keys, family directories move under categories

Camila, 2026-11-20. Follows plan 0098 (which announced these two steps as plans 0099 / 0100;
they ship together here as ONE mechanical PR, because both rewrite the same paths).

This plan changes names and locations only. It changes no bank byte, no judge prompt, no
`prompt_version`, no call key, no option order, no `plan_sha`, no ATI `bank_sha`, no seed, no
floor value, no fast subset file. Every results directory, readouts root, digest store and
adapted set written before it keeps reading as the family it was written for, or is refused
with a message that names this rename. Nothing is ever judged as the other family of a pair.

## 0. Decisions (the owner's, not open)

### 0.1 Key renames (A)

| key today | key after | title after | superseded |
|---|---|---|---|
| `basic_readout_mt` | `basic_readout` | Basic readout | |
| `multihop_mt` (former `multihop_mt_v2`) | `multihop` | Multihop | |
| `multilingual_mt` | `multilingual` | Multilingual | |
| `typo_mt` | `typo` | Typo | |
| `basic_readout` | `basic_readout_st` | Basic readout (single-token) | |
| `multihop` | `multihop_st` | Multihop (single-token) | |
| `multilingual` | `multilingual_st` | Multilingual (single-token) | |
| `typo` | `typo_st` | Typo (single-token) | `superseded_by="typo"` (was `typo_mt`; carried over) |

Every other key is unchanged, including `multilingual_multihop`, `multilingual_typo`,
`relational_multihop`, `model_multihop`. Four live keys (`basic_readout`, `multihop`,
`multilingual`, `typo`) change MEANING: before this plan they name the single-token family,
after it the multi-token one. Below, "pair" = one row pair of this table, "`_st` family" = its
single-token member, "bare key" = the key both members have held.

### 0.2 Directory layout (B)

`evals/<family>/` becomes `evals/<category>/<family>/`, category = `EvalSpec.group`
(`registry.CATEGORIES`). `evals/baselines/`, `evals/fast/`, `evals/hallucinationbench/` stay.
`adapted/<set>/<family>.json` stays flat (renamed for 0.1 only).

| category dir | families (keys after 0.1) |
|---|---|
| `basic` | association, basic_readout, basic_readout_st, directed_modulation, multi_concept_directed_modulation, multihop, multihop_st, multilingual, multilingual_st, multilingual_multihop, multilingual_typo, poetry, typo, typo_st |
| `computational` | arithmetic_intermediates, buggy_code, chain_intermediates |
| `association` | conjunctive_association, user_modeling |
| `safety` | agentic_misalignment, jailbreak_recognition |
| `anti_bag_of_features` | relational_multihop, role_bound_association |
| `logical_processing` | moral_rationale, nyt_connections |
| `model_specific` | hinted_cutoff, introspection, knowledge_cutoff, model_multihop, model_specific_directed_modulation, wrong_intermediate |
| `precision` | jlens_concept_pr |

### 0.3 The Python packages move too (my choice; the trade-off)

`src/wsbench/evals/<family>/` becomes `src/wsbench/evals/<category>/<family>/`, mirroring the
data tree, with NO compatibility shim (no re-export module at the old dotted path).

- For: (1) the owner's goal, one shape in both trees. (2) Safety of the swap: with flat
  packages, a stale `from wsbench.evals.multihop import prompts` in an unmigrated branch,
  script or notebook would import the OTHER family's module and often keep running (`PROMPTS`,
  `SPEC`, `PROMPT_VERSION` exist in both). With the packages moved, every pre-0101 dotted path
  is a `ModuleNotFoundError`. The move turns a silent hazard into a loud one. (3) The cost is
  mechanical: one regex table rewrites the 615 import lines (109 in `src`, 458 in `tests`, 48
  in `scripts`; 209 files).
- Against: a larger diff; every in-flight branch must re-run the rewrite (§8.5); two names need
  care (§2 F8: the family `association` vs the category `association`; `wsbench.evals.basic`
  vs the shared judge package `wsbench.basic`).
- Rejected: packages flat + data categorised (the hazard in (2) stays and the trees disagree);
  packages moved + shims (a shim at `wsbench.evals.multihop` would have to pick one family of
  the pair, which is exactly the silent mis-binding to avoid).

Code that needs a family's module by NAME (`tests/test_prompts_in_readme.py`,
`basic.mcjudge_bank.strings_of`, `baselines.prompt_only`, `registry._spec_for_bank`,
`adapt.run`) goes through one helper, `registry.family_module` (§4), never an f-string.

## 1. Size (measured on origin/main 030dd09b, 8,666 tracked files)

| what | count |
|---|---|
| family data dirs moved (`evals/`) | 32 dirs, 3,770 files (3,595 are `jlens_concept_pr`) |
| family packages moved (`src/wsbench/evals/`) | 32 packages, 111 files; + 8 new category `__init__.py` |
| `adapted/` paths renamed | 157 (125 `<family>.json` in 17 sets; 32 files in 16 `basic_readout_mt/` overlay dirs) |
| `examples/readouts/` renamed | 9 (8 `.jsonl`, 1 `.meta.json`) |
| `evals/baselines/{topics,confusers}/` renamed | 8 |
| test modules renamed | about 20 (§9.1) |
| text files rewritten (keys, paths, imports) | about 460: src 118, tests 212, scripts 68, skills 17, live docs 3, atlas 8, viewer_app 2, root md 4, family READMEs 24, baselines/fast READMEs 2, `adapted/README.md` |
| in-repo JSON with keys renamed IN PLACE | 75: 5 `evals/baselines/*.json`, 8 topics/confusers (inner `"family"` value), 62 under `adapted/` (17 `report.json`, 17 `floors/floors.json`, 16 `floors/prompt_only_summary.json`, 7 `anti_text_inversion*.json`, 5 `lenses/scores.json`) |
| in-repo JSON TRANSLATED AT READ (bytes untouched) | 3 `evals/fast/*.json`, 1 `evals/baselines/archive/*.json`, 2 goldens keyed by family (`plan_sha_pre_0090.json`, `capable_questions.json`) |
| left as history (not rewritten) | `plans/` (75 files), 28 `adapted/*/AUDIT*.md`-style records, 5 results docs under `docs/`, 3 skill audit records, every bank / pool / golden / fixture byte |

## 2. Findings from the code (each is designed around below)

Nothing found makes a hash-neutral rename impossible. Six places embed a family key or a
family path in a hash, seed or cache key and WOULD move without the fixes named here:

| # | where | what embeds the name | fix |
|---|---|---|---|
| F1 | `basic/mcjudge_bank.py:161` `options_of` | the option order of the single-token multihop judge is `mc.seeded_shuffle(content, f"{family}\|{item['id']}")`. Renaming `multihop` -> `multihop_st` would reshuffle every option list, change every MC prompt and miss every cached call. Pinned by `tests/golden/multihop_mc_options{,_v5}.json` | seed with `registry.hash_name(family)`; the docstring says so |
| F2 | `split.py:16` `is_dev` | `seed_int(f"{family}:{key}")` picks the dev/test split of `extras.layer_protocols` (report-only, native runs; callers in `basic/family.py`, `basic/mcjudge_{bank,basic}.py`, `multitoken/family.py`) | `hash_name(family)` inside `is_dev` |
| F3 | `evals/introspection/items_v2.json` `vectors.*.path` | the bank (frozen bytes) names `evals/introspection/vectors/{concept,gaussian}_vectors_L12.npy`; `readplan.vector_file` copies the string into `extra.steering.vector_file` of 1,750 plan rows, which `plan_sha` and the ATI `bank_sha` hash | the plan-row string is KEPT as written (it is hash input); the producer opens it through `registry.repo_path` (§4), which maps a pre-0101 layout path to its current location. Open question Q2 |
| F4 | `basic/prompts.py:108` `DIGEST_TASK_NOTES_TEXT` | a printed record `"<family>: <note>"` per line, in `basic.prompts.PROMPTS["DIGEST_TASK_NOTES"]`; hashed only by the pin in `tests/test_basic_prompts.py` (`PINNED["bank-2026-10-30b"]`) and printed in `evals/typo/README.md`. Never sent to a model as a whole (each family is sent its own line) | keys renamed with the families; the pin's sha constant is re-pinned (the ONE sanctioned sha change, §9.2 V1; the precedent is the same constant's re-pins by plans 0050 and 0058). `PROMPT_VERSION` stays `bank-2026-10-30b` |
| F5 | `capable/run.py:100,136,148,179` | cache keys `f"{family}\|{q.id}\|d{d}"`, `f"{family}\|{q_id}\|g…"` of the capable gate's reply / grade caches | `hash_name(family)` in the four keys (old caches keep hitting; the pair's ids are disjoint, so no key ever collides) |
| F6 | `adapt/generate.py:295`, `adapt/author.py:1463` | `random.Random(f"{family}:{tier}:{seed}")`, `random.Random(f"{model}:{family}:{len(pool)}")`: candidate generation of a set that is mid-refill | `hash_name(family)` in both seeds. (`generate.py:306` builds ids from `family.split('_')[0]`, which is the same string before and after for all eight keys: no change) |

Checked and clean (no family key inside): the `mcjudge.Call` fingerprint
`(prompt_version, model, reasoning, temperature, system, user)`; every cache key of the eight
families (`<item id>|L<layer>[|<role>]`, `digest:<id>|<layer>`, `ntok:<id>|…`); the digest
fingerprint; `mcjudge_basic` option seeds (`<OPTIONS_VERSIONS stamp>|<id>`); `multitoken/options`
seeds (`<name>|<role>`); every result stamp (§3.3 lists them; none contains a key); the task
note each family's digest is told (text only); `EvalSpec.title` (read only by `wsbench list`,
the README tests and the viewer; in no prompt). A scan of every family's cleaned plan rows
(`reads._clean_row`) finds a family key only in the `family` field (hashed through
`hash_name`) and a repo path only in F3.

Already hash-safe through `registry.hash_name`: `reads._clean_row` (`plan_sha`, fast plan sha),
`text_inversion._row_dict` (ATI `bank_sha`) and `qwen_seed`, `lucky_guessing._rng` and its call
keys, `gate_sampling.seed`, `scripts/r4_mc_select.py`.

Four more facts that shape the design:

- F7. `baselines/logit_lens.py:146` uses `registry.HASH_FAMILY_NAMES[family]` as a FILE-NAME
  fallback. After this plan `hash_name("multihop_st") == "multihop"`, which is another live
  family's key, so a hash name must never again be used as a lookup key. That line becomes
  `registry.lookup_names(family)`; a test asserts `HASH_FAMILY_NAMES` is referenced in
  `registry.py` only (§9.3 T9).
- F8. Name collisions between levels: the FAMILY `association` (category `basic`) and the
  CATEGORY `association` (families conjunctive_association, user_modeling). After the move
  `evals/association/` and `wsbench.evals.association` are the category; the family is
  `evals/basic/association/`, `wsbench.evals.basic.association`. The migration must move the
  family out before it creates the category dir (the two-phase move of §8.2 does). Also
  `wsbench.evals.basic` (category) is not `wsbench.basic` (the shared bank judge). Category
  packages export nothing, so a stale `from wsbench.evals.association import judge` fails.
- F9. Every bank file of the eight families, registered and adapted (all 125 adapted files
  checked), has a header `"family"` value from the SOURCE repo's vocabulary, which never
  changed with our keys and is the reliable content label of a bank:

  | header `"family"` | family (key after) |
  |---|---|
  | `basic-readout` | basic_readout_st |
  | `basic-readout-hard` | basic_readout |
  | `multihop` | multihop_st |
  | `multihop-hard` | multihop |
  | `multilingual` | multilingual_st |
  | `multilingual-hard` | multilingual |
  | `typo` | typo_st |
  | `typo-hard` | typo |

  Trap: three of these values (`multihop`, `multilingual`, `typo`) are spelled like the bare
  keys and mean the `_st` family. They are bank bytes, are never rewritten, and are compared
  only through this table (`Reused.live_header` / `other_header`, §3.2), never to a key.
- F10. Within each pair the two families share no item id, over every registered bank version
  AND every in-repo adapted bank (checked: basic_readout 606 vs 1,119 ids, multihop 657 vs 427,
  multilingual 123 vs 615, typo 223 vs 728; overlap 0 in all four). So item ids identify the
  family of a readouts file or a cache. A test pins the disjointness (§9.3 T5).

## 3. `src/wsbench/registry.py`

### 3.1 Tables

```python
# Former keys that mean exactly ONE current family (pure renames). canonical() is single-step.
LEGACY_FAMILY_NAMES = {
    "introspection_inject": "introspection",
    "cutoff_confab": "knowledge_cutoff",
    "dm_category_default": "model_specific_directed_modulation",
    "directed_modulation_obscure": "model_specific_directed_modulation",
    "wrong_bridge": "wrong_intermediate",
    # plan 0101: the multi-token families took the plain keys
    "multihop_mt_v2": "multihop",   # was -> "multihop_mt" (2026-11-04); single-step, so it
    "multihop_mt": "multihop",      # now points straight at the current key
    "basic_readout_mt": "basic_readout",
    "multilingual_mt": "multilingual",
    "typo_mt": "typo",
}
```

Retained from the 2026-11-04 rename, restated because the entry is rewritten:
`canonical("multihop_mt_v2")` still resolves to the multi-token multihop family in one step
(its key is now `multihop`), and `legacy_names("multihop")` lists `multihop_mt_v2` BEFORE
`multihop_mt` (dict order), so a file under `multihop_mt_v2` still wins over one under
`multihop_mt`, which may be the removed regex family's. The bare keys `basic_readout`,
`multihop`, `multilingual`, `typo` are NOT in this map: on the command line they mean the
multi-token family (§5.6).

```python
# The key a family hashes under in plan_sha / bank_sha / seeds / option orders: frozen.
HASH_FAMILY_NAMES = {
    "wrong_intermediate": "wrong_bridge",
    "multihop": "multihop_mt_v2",        # retained: was HASH_FAMILY_NAMES["multihop_mt"]
    "basic_readout": "basic_readout_mt",
    "multilingual": "multilingual_mt",
    "typo": "typo_mt",
    "basic_readout_st": "basic_readout",
    "multihop_st": "multihop",
    "multilingual_st": "multilingual",
    "typo_st": "typo",
}
```

Retained: every family hashes under exactly the string it hashes under today (the multi-token
multihop family keeps `multihop_mt_v2`, as since 2026-11-04). `hash_name` stays
un-canonicalized. NEW rule in its docstring: a hash name is an opaque hash input, never a
lookup key or a file name (F7); `hash_name` is not idempotent across a pair
(`hash_name("multihop_st") == "multihop"`, `hash_name("multihop") == "multihop_mt_v2"`), so it
is applied once, to a CURRENT key.

```python
# The key every family had on main just before plan 0101 -> its key now: the map for files that
# are frozen in the old vocabulary (evals/fast/*.json, the archived floor, goldens) and for
# pre-0101 layout paths (repo_path). Never used for user input.
PRE_0101_KEYS = {
    "basic_readout": "basic_readout_st", "basic_readout_mt": "basic_readout",
    "multihop": "multihop_st", "multihop_mt": "multihop", "multihop_mt_v2": "multihop",
    "multilingual": "multilingual_st", "multilingual_mt": "multilingual",
    "typo": "typo_st", "typo_mt": "typo",
}
def from_pre_0101(key: str) -> str: ...   # PRE_0101_KEYS.get(key, canonical(key))
def rekey_pre_0101(d: Mapping[str, V]) -> dict[str, V]: ...  # keys translated, order kept;
                                           # two keys landing on one key -> ValueError
```

### 3.2 `REUSED_FAMILY_KEYS`, generalised (one mechanism, five entries)

Today: `{"multihop_mt": ("multihop_mt_regex", (<live stamp prefixes>))}`. It becomes a map from
EVERY key that two families have held to a record of both holders:

```python
@dataclass(frozen=True)
class Reused:
    live: str                        # the family the key means when typed today (a current key)
    other: str                       # the family that held it before (a current or removed key)
    live_stamps: tuple[str, ...]     # result-stamp prefixes of `live`
    other_stamps: tuple[str, ...] | None  # CLOSED list of the prefixes `other` ever wrote under
                                     # this key; None = every stamp that is not live's
    live_header: str | None = None   # bank header "family" value of `live`'s banks (F9)
    other_header: str | None = None
    since: str = ""                  # for messages: "2026-11-20 (plan 0101)"

REUSED_FAMILY_KEYS: dict[str, Reused] = {
    # 2026-11-04, retained unchanged in behaviour: `other_stamps=None`
    "multihop_mt": Reused("multihop", "multihop_mt_regex",
                          ("mh2-mc-", "mt-regex-2026-10-30+md+fz"), None,
                          "multihop-hard", None, "2026-11-04"),
    # plan 0101
    "basic_readout": Reused("basic_readout", "basic_readout_st",
                            ("mc-brmt-", "mt-regex-", "mc-2026-"), ("br-mc-", "bank-"),
                            "basic-readout-hard", "basic-readout", "2026-11-20 (plan 0101)"),
    "multihop": Reused("multihop", "multihop_st",
                       ("mh2-mc-", "mt-regex-2026-10-30+md+fz"), ("bank-",),
                       "multihop-hard", "multihop", "2026-11-20 (plan 0101)"),
    "multilingual": Reused("multilingual", "multilingual_st",
                           ("mt-regex-", "mc-ml-", "mc-2026-"),
                           ("mcrx-", "bank-", "mt-regex-2026-10-30+leg-regex"),
                           "multilingual-hard", "multilingual", "2026-11-20 (plan 0101)"),
    "typo": Reused("typo", "typo_st", ("mt-regex-", "mc-2026-"), ("bank-",),
                   "typo-hard", "typo", "2026-11-20 (plan 0101)"),
}
```

Retained for `"multihop_mt"`, restated because the entry is rewritten: a stored result under
`multihop_mt` whose stamp starts with `mh2-mc-` or `mt-regex-2026-10-30+md+fz` is the
multi-token multihop family's (read under its current key, now `multihop`); ANY other stamp
(`mc-2026-09-16`, `mt-regex-2026-09-23`, …) is the removed regex family's, read as
`multihop_mt_regex` and excluded from the macro as `removed`. `REMOVED_FAMILIES
["multihop_mt_regex"]` is unchanged except that its text names the live key `multihop`.

Stamp prefixes, as verified against every stamp in the repo's floors, ATI files, adapted
`floors/` and `lenses/scores.json` (the stamps found: single-token `bank-2026-10-10`,
`bank-2026-10-30b`, `bank-mc-2026-11-01-v3…`, `br-mc-2026-11-01-v5b…`, `mcrx-2026-11-0…`;
multi-token `mc-brmt-2026-10-31r…`, `mh2-mc-2026-11-01…`, `mt-regex-2026-09-23`,
`mt-regex-2026-10-30+…`, `mc-2026-09-16`). The longest matching prefix over both tuples
decides (so `mt-regex-2026-10-30+leg-regex`, the single-token multilingual family's
`opts=judge=regex` diagnostic, beats `mt-regex-`). The implementer must confirm two things
before freezing the tuples and add any stamp found: (a) `git log -S` over
`basic/prompts.py`, `multitoken/prompts.py`, `multitoken/regex.py` for every historical value
of the stamp constants; (b) whether the single-token multilingual family can emit a bare
`mc-ml-…` or `mc-2026-…` stamp under `opts=judge=mc` (`multitoken/family.py:1958,2010,2194`);
if it can, that prefix is removed from `live_stamps` and belongs to neither side (such a
result is then refused, §3.3). Import-time invariant, also a test (T2): for each entry whose
two holders are registered, each holder's registered `prompt_version` classifies as that
holder.

### 3.3 Functions (exact behaviour)

```python
class RenamedKey(SystemExit): ...   # exit 2; the message names the rename and both keys
```

- `canonical(name)`: unchanged (`LEGACY_FAMILY_NAMES.get(name, name)`).
- `legacy_names(name)`: unchanged.
- `partner(name) -> str | None`: the other member of `name`'s 0101 pair (`multihop` <->
  `multihop_st`, …), else None.
- `shared_key(name) -> str | None`: the bare key `name`'s pair has shared (`multihop` for both
  members), else None.
- `lookup_names(name) -> list[str]`, the keys a file or directory of the current family `name`
  may sit under, in lookup order:
  - a family that is the `other` of a reused key (the four `_st` families): `[name, <bare key>]`;
  - a family that is the `live` of a 0101 reused key (the four multi-token families):
    `[name, *legacy_names(name)]`, e.g. `["multihop", "multihop_mt_v2", "multihop_mt"]`,
    `["basic_readout", "basic_readout_mt"]`;
  - every other family: `[name, *legacy_names(name)]` (unchanged:
    `["wrong_intermediate", "wrong_bridge"]`).
  Retained from 2026-11-04: `multihop_mt_v2` is still tried before `multihop_mt`. Changed: the
  current key is tried first for the 0101 pairs, which is safe only because every hit under a
  bare key is checked by content (`owner`, below); the old "former key first for a reused key"
  order existed because `multihop_mt` files could not be told apart by content.
- `owner(key, *, stamp=None, header=None, ids=None, plan_sha=None) -> str | None`: which
  holder of the reused `key` the evidence identifies; None with no evidence. Rules, in order:
  `stamp` (non-empty): `Reused` prefixes, longest match; a non-empty stamp matching neither
  tuple raises `RenamedKey`. `plan_sha`: equal to `reads.plan_sha(live)` or `(other)` (passed
  in by the caller as a pre-computed pair, so `registry` does not import `reads`). `header`:
  `live_header` / `other_header`. `ids`: `live` when the ids intersect only `family_ids(live)`,
  `other` when only `family_ids(other)`, None when neither, `RenamedKey` when both. Two kinds
  of evidence that disagree raise `RenamedKey`.
- `family_ids(family) -> frozenset[str]`: every item id of every `items*.json` under
  `family_root(family)` plus, under `$WSB_BANK_ROOT`, the set's bank of the family
  (`bank_path`); cached per (family, root).
- `stored_family(stored_key, prompt_version) -> str`. SIGNATURE CHANGE: it takes the key AS
  STORED (not canonicalized), because the two reused keys of the multihop family resolve
  differently. `stored_key` not in `REUSED_FAMILY_KEYS` -> `canonical(stored_key)`. Else, by
  stamp: live -> `r.live`; other -> `r.other`; EMPTY or missing stamp -> `r.live` (the bare key
  means the live family unless the content says otherwise; hand-built test results have no
  stamp); a non-empty stamp of neither -> `RenamedKey`. For `multihop_mt`
  (`other_stamps=None`) a non-live stamp, empty included, is `multihop_mt_regex`, exactly as
  today.
- `family_dir(root, name) -> Path`: for each key of `lookup_names(name)` whose directory
  exists: a key not in `REUSED_FAMILY_KEYS` is returned as today; a reused key is returned only
  if `dir_owner(path)` is `name` or None-and-`key == name`; a directory owned by the partner is
  skipped. If nothing is returned and `root/name` exists but belongs to the partner:
  `RenamedKey` ("`<root>/multihop` holds results of `multihop_st`, written before 2026-11-20
  when the single-token family was keyed `multihop`; use a fresh `out=` or rename the directory
  to `multihop_st`"). Else `root/name`. Retained from v1.2.0 / 2026-11-04: an existing
  directory under a former key is reused so its cached verdicts are kept; `multihop_mt`
  directories keep today's rule (no content check: the removed family has no bank to compare).
- `dir_owner(path) -> str | None`: `results.json` present -> `stored_family(its family, its
  config.prompt_version)`; else the item ids of the first 2,000 rows of `cells.jsonl` or (a
  digest store dir) `L*.jsonl`, each id = the row `key` minus a `digest:` / `ntok:` prefix, cut
  at the first `|` -> `owner(key, ids=…)`; else None.
- `bank_path(family)`: `$WSB_BANK_FILE_<FAMILY>` as today (current key, then former keys), plus
  a check: when the named file has a header `"family"` that is the partner's value, exit with
  the rename message (a pre-0101 shell that sets `WSB_BANK_FILE_MULTIHOP` to a single-token
  bank). Under `$WSB_BANK_ROOT`: the first `lookup_names` file that exists and, when its name
  is a reused key, whose header does not say the partner (`_st` families: whose header SAYS
  `other_header`, a positive match is required of them under the bare key). Otherwise the
  existing "has no bank for" exit, with the rename named when a skipped partner file was seen.
  Without overrides: `REPO_ROOT / spec.bank` as today.
- `overlay_path`: unchanged apart from `lookup_names` (overlays stay
  `adapted/<m>/<family>/<file>`; the only overlay dirs of the eight families are the 16
  `basic_readout_mt/` dirs, renamed to `basic_readout/`; an unmigrated set's
  `basic_readout_mt/` is still found through `lookup_names("basic_readout")`, and the
  single-token family has no overlay dir in any set, so the bare-key overlay dir is the
  multi-token family's by construction; `overlay_path(<_st family>, …)` therefore never looks
  under the bare key).
- `load_all()`: walks two levels (§4).
- `register(spec)`: additionally raises `ValueError` when `spec.name` is in the package layout
  and `spec.group != category_of(spec.name)` (a family's directory is its category).

## 4. Path resolution: one helper family, one rule

```python
@functools.cache
def _layout() -> dict[str, str]:
    """family -> category, read from the package tree without importing anything:
    wsbench.evals.__path__/<category>/<family>/__init__.py. A family in two categories, or a
    category that is not a key of CATEGORIES, is a ValueError."""
def category_of(family: str) -> str            # canonical first; unknown -> KeyError
def family_rel(family: str) -> Path            # Path("evals") / category / family  (EvalSpec.bank, messages, README links)
def family_root(family: str) -> Path           # REPO_ROOT / family_rel(family)
def family_module(family: str, sub: str = "") -> ModuleType
    # importlib.import_module(f"wsbench.evals.{category}.{family}" + (f".{sub}" if sub else ""))
def repo_path(rel: str | Path) -> Path
    # REPO_ROOT / rel; a path of the pre-0101 layout, evals/<pre-0101 key>/<rest> with
    # <pre-0101 key> not baselines|fast|hallucinationbench, resolves to
    # family_root(from_pre_0101(<key>)) / <rest>. ONLY for path strings frozen inside data
    # (F3 and the informational "bank" strings of adapted report.json). Never for user input.
```

- `resource_path(family, name)` = `family_root(family) / name`. `frozen_bank_path` and
  `bank_path` are unchanged in form (`REPO_ROOT / spec.bank`).
- `EvalSpec.bank` stays a REPO_ROOT-relative `Path`, now built from `family_rel`:
  `bank=registry.family_rel("multihop") / "items_v6.json"`. The two factories' defaults
  (`basic/family.py:61`, `multitoken/family.py:~286`) become `family_rel(name) / "items.json"`.
- `load_all()`: `for category in pkgutil.iter_modules(evals_pkg.__path__)` then
  `for mod in pkgutil.iter_modules(<category path>)` -> `import_module(f"wsbench.evals.
  {category}.{mod}")`. `adapt/run.py:865-870` does the same walk through a new
  `registry.family_packages() -> list[str]` (dotted names) instead of its own `iter_modules`.
- By-name imports replaced by `family_module`: `registry._spec_for_bank` (365),
  `basic/mcjudge_bank.strings_of` (139), `baselines/prompt_only.py` (81, 99),
  `tests/test_prompts_in_readme.py:29`.
- `readplan.frozen_lite_rule` (128), `nocot.OBSCURE` (16), `lucky_guessing.py:1128`,
  `evals/jlens_concept_pr/judge.BANK_DIR`, `evals/typo_mt` `BANK`, and every other
  `REPO_ROOT / "evals" / family / …` or `"evals/<family>/…"` literal in `src/wsbench` (72 lines
  in 65 files) -> `registry.family_root(...)` / `family_rel(...)` / `resource_path(...)`.
- `readplan.families()` (1387-1392): iterates `registry._layout()` and keeps the families whose
  `family_root` has `items*.json` or `manifest.json`, instead of `(REPO_ROOT / "evals").iterdir()`.
- `readplan.vector_file` keeps returning the bank's string unchanged (F3);
  `produce/producer.py` `_vector` opens `registry.repo_path(steer["vector_file"])` when the
  path is relative. `adapt/moral.py:992` and `adapt/mcstage.py:127` ("is this path under
  `evals/`") keep working at any depth.
- Env values: `WSB_BANK_FILE_<F>=evals/<old>/items.json` is user input and is NOT translated; a
  missing file whose path has the pre-0101 shape exits with "family directories moved under
  category folders on 2026-11-20 (plan 0101): `<family_rel(...)>/…`".

The rule: outside `registry.py`, no code builds a family's data path or module name by hand.
Enforced by `tests/test_family_paths.py` (TP):

1. an `ast` scan of `src/wsbench/**/*.py`, `atlas/*.py`, `viewer_app/*.py`,
   `.claude/skills/**/*.py`: no non-docstring string constant matches
   `^evals/(<category>|<family or pre-0101 key>)(/|$)`; no `BinOp` chain `X / "evals" / Y` whose
   next operand is anything but the literals `"baselines"`, `"fast"`, `"hallucinationbench"`;
   no f-string or literal starting `wsbench.evals.` passed to `import_module`. `registry.py` is
   the one exempt file.
2. `scripts/*.py` are one-off builders that ran once; their literals are rewritten by the
   table and stay literals. The test holds the list of script files that contain such a
   literal (`tests/golden/script_path_literals_0101.txt`) and fails when a file NOT in the list
   gains one (the list may only shrink).
3. every `evals/…` path written in `README.md`, `CLAUDE.md`, `AGENTS.md`, `NOTICE.md`,
   `docs/{producing_readouts,summarizer,adapt_judge_inputs}.md`, `.claude/skills/**/*.md`
   (audit records excluded), every family README, `tests/**/*.py` and the listed scripts
   exists on disk (globs and `<placeholders>` skipped).

## 5. Where each lookup happens (fail closed everywhere)

### 5.1 `results.py`

- `read_results(out_dir)`: `family = registry.stored_family(result.family,
  result.config.get("prompt_version"))` (the `canonical(...)` wrapper is dropped: the function
  canonicalizes non-reused keys itself). A `RenamedKey` propagates: `report` already exits 2 on
  unreadable results, and the message names the directory.
- `adapted_exclusions()`: the report's `families` entries are attributed with
  `adapt.store.report_key` semantics (§5.5), not by raw `LEGACY_FAMILY_NAMES` lookup. Retained:
  an entry under the current key wins over one under a former key.
- `_spec_of` (332) and every `registry.FAMILIES.get(registry.canonical(family))` are unchanged:
  they see families that `read_results` already attributed.
- `scripts/audit_run.py:57` (`registry.canonical(res["family"])`) -> `stored_family`.

### 5.2 `reads.py`

`load(args, family, path)` is the last guard, whatever the file is called. New block right
after `meta = read_sidecar(path)`, only when `registry.partner(family)` is not None:

1. sidecar with `plan_sha`: if it equals `plan_sha(partner)` (or, for a fast file,
   `fast_plan_sha(partner, declared=…)` when the partner is in that subset) -> `fail` with
   "`<path>` holds `<partner>` readouts (the sidecar's plan is `<partner>`'s; before 2026-11-20
   `<bare key>` named the single-token family, plan 0101): judge it with `family=<partner>`".
   Otherwise guard (iii) runs unchanged (own plan, lineage, or the existing "different plan"
   refusal).
2. no sidecar `plan_sha`: `readouts.ids_in(path)` (new: the distinct `id`s of the file,
   streamed, malformed lines skipped) -> `registry.owner(shared_key, ids=…)`; the partner ->
   the same `fail`; both -> `fail` (mixed file); own or None -> continue as today (None ends in
   the existing missing-cells exit 2).

`reads.owner_of(path, family) -> str | None` exposes exactly this decision for the runner.
`lineage_drops` and `min_full_positions`'s report lookups go through `store.report_key`
semantics (§5.5) instead of `lookup_names` membership alone. `_clean_row` is unchanged
(`hash_name`).

### 5.3 `runner.py`

- `readouts_path(root, name)`: for each key of `registry.lookup_names(name)` whose
  `<key>.jsonl` exists: a key not in `REUSED_FAMILY_KEYS` is returned; `multihop_mt` is
  returned (retained: no content check); a 0101 bare key is returned unless
  `reads.owner_of(path, name)` is the partner (the multi-token family accepts unidentified
  content under its own key; an `_st` family accepts the bare key's file ONLY when it is
  positively identified as its own). So a pre-0101 root (`multihop.jsonl` single-token,
  `multihop_mt.jsonl` multi-token) serves both families, and a post-0101 root serves both.
- `admit` (765-775): the "reading legacy `<file>`" line stays; when the family's own-key file
  was skipped as the partner's, the skip message says so (`no readouts: <root>/multihop.jsonl
  holds multihop_st readouts (pre-0101 name)`).
- `adapted_bank_stamp` (240-241), `judge_argv`, `_one`, `_one_proc`: keys only; no logic
  change. The process-mode child is spawned with the CURRENT key and must not print the swap
  notice (§5.6).
- `run_families` `dest`: `registry.family_dir(out, s.name)` (837), now content-checked (§3.3).

### 5.4 `digest.py`, `fast.py`, `tiers.py`, `readplan.py`

- `digest.py:2468`: `family_dir(Path(store).resolve(), name)` -> the content-checked
  `family_dir` (a pre-0101 `summ_store` keeps serving both families; a store directory of the
  partner under the family's own key is `RenamedKey`).
- `fast.py`: `_file(version)` returns the file with `families` keys and `dropped_families`
  values passed through `registry.rekey_pre_0101` / `from_pre_0101` when `fast.KEY_ERA[version]
  == "pre-0101"` (all three existing versions). The files are NOT edited
  (`tests/test_fast_subset.py` keeps pinning their sha256). A future subset file is written in
  current keys and registered with era `"0101"`. Effect: fast-v2's `basic_readout` entry is
  served as `basic_readout_st`, its `basic_readout_mt` entry as `basic_readout`, its
  `multihop_mt` entry as `multihop`; `fast.families()` is the same 22 families under their
  current keys; `bank_sha` (file bytes) is untouched; `fast_plan_sha` is untouched
  (`hash_name`). `PAIR_FAMILIES` / `MAJORITY_FAMILIES` hold none of the eight.
- `tiers.py`: no change (`CORE` holds none of the eight keys; `full()` drops `typo_st`, as it
  dropped `typo`).
- `readplan.py`: its family tables (`LITE_SITE_FAMILIES` etc., lines 116, 585-594, 746-761,
  802-829) are renamed by the script; `plan()` keeps `canonical(family)`; rows carry the
  current key in `family` (so `wsbench plan` output shows `multihop`, hashing as
  `multihop_mt_v2`). A PLAN FILE written before 0101 and fed to `produce plan=FILE` afterwards
  carries old keys: its rows hash (through `hash_name`) to a sha that matches neither family,
  so the judge refuses the result at guard (iii); `Producer` additionally refuses a plan-file
  row whose `family` is a 0101 bare key and whose `id` is in `family_ids(partner)`.

### 5.5 `adapt/` and adapted sets

In-repo sets are renamed in place (§6). Unmigrated sets exist outside git (sets stay local
until frozen), so reads must still work on them:

- `store._legacy_ok` / `bank_file` -> `registry.bank_path`-style lookup with the header check.
- `store.report_key(section, family)`: among `lookup_names(family)` present in the section, a
  reused key is taken only when the set's own bank file identifies the holder (the bank under
  that key has `live_header` / `other_header`), or, for the `drops` / `patches` sections,
  when the entry's item ids do (`registry.owner(key, ids=…)`). Retained from 2026-11-04,
  restated because this function is rewritten: the removed regex family's entries
  (`drops.multihop_mt_regex`, ids `multihop-au-*`) are never read as the live family's, and a
  set written under `multihop_mt_v2` is still read.
- `gate_path` / `cand_path` (`adapted/<m>/gate|cand/<family>.jsonl`, untracked work files):
  same rule, ids as evidence.
- WRITES are fenced by a marker: `report.json` gains `"family_keys": "0101"` (written by the
  migration and by every new build). `adapt` subcommands that write (`build`, `refill`,
  `freeze`, `drop`, `floors`, `gate_adapted`, …) exit 2 on a set without the marker:
  "`adapted/<m>` was written before plan 0101; run `scripts/migrate_0101.py set <dir>`". Reads
  (judging under `WSB_BANK_ROOT`) need no marker.
- Family tables keyed by name (`adapt/families.py` `FAMILIES` / `REGENERATE` /
  `SUBJECT_FIELDS` / `REQUIRED_ITEM_FIELDS`, `judge_inputs.py`, `lint.py`, `author.py`,
  `mcstage.py`, `refill.py`, `onecue.py`, `ml_native.py`, `native_langs.py`, `rebuild.py`,
  `dataround.py`, `mhmt2.py` `FAMILY`, `mhstage.py`, `gate.py`): renamed by the script (269 key
  hits in 24 files). Function names such as `_basic_readout_mt` are renamed with them.
- New adapted banks keep writing the header `"family"` value of the registered bank
  (`store.original_bank` copies the header), never the key (T6).
- `adapt/__main__.py:173-187`: uses `cli._family` semantics (§5.6) for its family arguments.

### 5.6 `cli.py`

- `_family(name, *, notice=True) -> str`:
  - a former key -> the current one, with today's line on stderr
    (`note: family 'multihop_mt' was renamed to 'multihop'`; same for `multihop_mt_v2`,
    `basic_readout_mt`, `multilingual_mt`, `typo_mt`);
  - one of the four bare keys -> itself, and ONE line on stderr per key per process:
    `note: 'multihop' is the multi-token family since 2026-11-20 (was multihop_mt); the
    single-token family is 'multihop_st'`. Printed while `registry.SWAP_NOTICE` is True
    (a constant with a removal note: delete it and the test after the transition); silenced by
    `WSB_NO_RENAME_NOTICE=1`, and never printed by `JudgeFamily` when `outcome=` is set (the
    process-mode child, which the parent calls with a current key).
  - an `_st` key: silent.
- Every command already routes `family=` / `families=` through `_family` / `_families`
  (lines 340, 412, 1030, 1173, 1265, 1360, 1605) and `ati_opt_in` through `canonical` (255);
  no new call site.
- `JudgeFamily` (352) and `RunFamilies` (537): `registry.family_dir` (§3.3).
- `_n_items` (1713): `registry.frozen_bank_path(spec.name)`.
- `wsbench list` shows the new keys and titles; `typo_st`'s tier column is `—` (superseded).

### 5.7 Baselines (`baselines/*.py`)

- `text_inversion.py`: `NOT_GATED` (141-147), the gate-field tables (207-212), `ATI_EXCLUDE`
  (246, 279, 310) renamed by the script. `_by_family(d, family)` (365): `d[family]` unless the
  entry's own stamp says the partner (entries carry the instrument stamp:
  `registry.owner(key, stamp=…)`), else a former key; the `bank_sha` and instrument guards stay
  the backstop (a mis-keyed entry is `stale`, exit 2) and their message gains the rename hint
  for the eight families.
- `prompt_only.py` / `lucky_guessing.py` / `logit_lens.py`: frozen files are read by current
  key after the in-place rename (§6). The loaders run one check on load: an entry under a bare
  key whose `prompt_version` is the partner's (by `registry.owner`) is a `RenamedKey` (a stale
  local floors file). A floor is drawn only when its stamp equals the row's, so a mis-keyed
  floor never draws even without the check. `prompt_only.ARCHIVE` (the 2026-10-29 archive) is
  read through `rekey_pre_0101` and not edited. `logit_lens._plan_sha` (146): F7.
  `lucky_guessing.TOPICS_DIR / f"{family}.json"`: files renamed (§6); `TOPIC_FAMILIES` and the
  other tables renamed by the script.
- `capable/questions.py` (256-269, 317): table keys and their `_asked("<family>", …)` /
  `_with_bridges("<family>")` arguments renamed; `capable/run.py` keys: F5.

## 6. Every in-repo artefact keyed by family

"in place" = renamed by the script, value-preserving, checked by `json.loads(new) ==
translate(json.loads(old))`; all of these files are `json.dumps(indent=1, ensure_ascii=False)`
plus a newline, so the script rewrites them by load -> translate -> dump and requires the
untouched remainder to be byte-identical (it aborts if a file does not round-trip before the
change). "at read" = bytes untouched, translated by `rekey_pre_0101`. "history" = untouched.

| artefact | count | decision |
|---|---|---|
| `evals/<family>/**` | 3,770 files | moved byte-identical to `evals/<category>/<new key>/` (R100 renames); bank headers keep their `"family"` value (F9), as in every earlier rename |
| `evals/<family>/README.md` | 32 | moved, then rewritten (keys, paths, title line); prompt blocks are checked verbatim by `test_prompts_in_readme` |
| `evals/<family>/*.md` audit notes (e.g. `multihop_mt/V1_AUDIT_2026-10-15.md`) | 6 | moved; history, not rewritten |
| resource files read by path (`basic_readout_mt/exonyms.json`, `multihop/derived_forms.json`, `multihop/split_subjects.json`, `*/lite_rule.json`, `*/r4*_distractors.json`, `knowledge_cutoff/obscure_items.json`, `introspection/vectors/`, `jlens_concept_pr/manifest.json`) | — | moved with their dir; every reader goes through `registry.resource_path` / `family_root` (§4) |
| `evals/baselines/prompt_only.json`, `prompt_only_summary.json`, `logit_lens.json`, `lucky_guessing.json`, `anti_text_inversion.json` (incl. `_not_gated`) | 5 | in place: top-level family keys and inner `"family"` values (7, 7, 7 + 7, 7 and 8 occurrences). No value, stamp, `bank_sha`, id list or `_source` field changes |
| `evals/baselines/lucky_guessing_moral_v5.json`, `_v7.json` | 2 | no key of the eight: untouched |
| `evals/baselines/topics/{basic_readout_mt,multihop_mt,multilingual,multilingual_mt}.json` | 4 | renamed to `{basic_readout,multihop,multilingual_st,multilingual}.json` (two-phase, §8.2); inner `"family"` value in place |
| `evals/baselines/confusers/` (same four names) | 4 | same |
| `evals/baselines/archive/prompt_only_summary.qwen-2026-10-29.json` | 1 | at read (an archive is a record); keys `basic_readout`, `multihop`, `multilingual`, `typo` there mean the `_st` families |
| `evals/baselines/README.md`, `evals/fast/README.md` | 2 | rewritten (207 + 9 key hits); a dated note says what the keys were before |
| `evals/fast/fast_v1.json`, `fast_v2.json`, `fast_safety_v1.json` | 3 | at read (`fast.KEY_ERA`), never edited: sha-pinned byte for byte. Each holds `basic_readout` and `basic_readout_mt` (they swap) and `multihop_mt` as `families` keys, and `typo_mt`, `multihop`, `multilingual`, `multilingual_mt` in `dropped_families` |
| `evals/hallucinationbench/index.json`, `methods.json` | 2 | hold no family key: untouched; `hbindex.py` unchanged |
| `adapted/<m>/<family>.json` | 125 | renamed (two-phase): 17 `basic_readout` -> `basic_readout_st`, 17 `basic_readout_mt` -> `basic_readout`, 17 `multihop` -> `multihop_st`, 6 `multihop_mt` + 10 `multihop_mt_v2` -> `multihop`, 17 `multilingual` -> `multilingual_st`, 17 `multilingual_mt` -> `multilingual`, 7 `typo` -> `typo_st`, 17 `typo_mt` -> `typo`. Bytes identical. This also retires the last in-repo `multihop_mt_v2.json` files |
| `adapted/<m>/basic_readout_mt/{exonyms,hidden_rivals}.json` | 32 in 16 dirs | dir renamed to `basic_readout/`; bytes identical |
| `adapted/<m>/report.json` | 17 | in place: keys of `families`, `drops`, `patches` (272 keys), plus the marker `"family_keys": "0101"`. Per section the script asserts no two keys land on one (`multihop_mt` and `multihop_mt_v2` never share a section: checked) and, for `drops` / `patches` under `multihop_mt`, that the ids are `mh2-*` (-> `multihop`); `multihop_mt_regex` entries are left. The informational `"bank": "evals/<family>/items_vN.json"` strings (in 16 reports) are left as written and read through `repo_path` |
| `adapted/<m>/floors/floors.json`, `floors/prompt_only_summary.json` | 17 + 16 | in place (494 + 107 keys) |
| `adapted/<m>/floors/lucky_guessing.json` | — | holds none of the eight today; covered by the same rule if it gains one |
| `adapted/<m>/anti_text_inversion.json`, `.with_qwen27b.json` | 6 + 1 | in place (64 keys); `bank_sha` values untouched and still valid (`hash_name`) |
| `adapted/<m>/lenses/scores.json` | 5 | in place: 424 row `"family"` values, each checked against its row's `instrument` stamp with `registry.owner` |
| `adapted/<m>/{mh2_refill,kcutoff,msdm,moral,agentic,introspection,jlens_concept_pr}/` stage dirs | — | not named by a key of the eight: untouched |
| `adapted/README.md` | 1 | rewritten |
| `adapted/<m>/AUDIT*.md`, `NOTES.md`, `AUDIT_RESPONSE.md`, set READMEs | 28 | history: not rewritten; `adapted/README.md` gains the key table of §0.1 for readers of those records |
| `examples/readouts/<family>.jsonl[.meta.json]` | 9 of the eight | renamed (two-phase); bytes identical; the files of the other 24 families do not move (the directory is flat) |
| `tests/golden/*` | about 60 | names and bytes untouched. `plan_sha_pre_0090.json` and `capable_questions.json` are keyed by pre-0101 keys and read through `rekey_pre_0101` in their tests (values unchanged); `tests/golden/README.md` gains the key table |
| `tests/fixtures/*` | 12 | untouched. `adapted_v1/` (`multilingual.json`, `multilingual_mt.json`, …) is now by construction a pre-0101 set, and becomes the fixture of the header-identification tests (T4) |
| `src/wsbench/adapt/data/*.json` (sha-pinned pools; "typo" occurs there as an English word) | — | never touched |
| `scripts/*.py`, `scripts/*.sh` | 68 with a key or path | rewritten mechanically (289 key hits, 141 path hits, 48 import lines); they are one-off builders, so the rewrite keeps them importable and their paths valid, nothing more. `scripts/ati_restamp_rename.py`, `scripts/drop_frac_0083.py` and other dated migration scripts are rewritten the same way (paths only) |
| `.claude/skills/**` | 20 | rewritten (351 key hits, 40 path hits), except `adapt-bank/SKILL_AUDIT.md` and `SKILL_AUDIT_RESPONSE.md` (history). `wsb-new-questions/scripts/*.py` are live code: under the §4 rule |
| `docs/producing_readouts.md`, `summarizer.md`, `adapt_judge_inputs.md` | 3 | rewritten |
| `docs/summary_mode_validation.md`, `plan0079_results.md`, `plan0012_results.md`, `summary_mode_audits/*` | 5 | history: not rewritten; each gains one line at the top pointing at the key table |
| `atlas/` (`export_cb.py`, `build.py`, `normal_gate.py`, `normal_gated.py`, `run_normal_judge.sh`, `site/app.js`, `site/index.html`, `CB_EXPORT.md`) | 8 | python rewritten by the script; `site/*` and the shell script by hand (§8.4). Run trees are read through `registry.family_dir` + `read_results` |
| `viewer_app/index.py`, `README.md`; `src/wsbench/viewer/build.py` `GROUPS` | 3 | rewritten (keys); `GROUPS` keeps its family set |
| `README.md`, `CLAUDE.md`, `AGENTS.md`, `NOTICE.md` | 4 | rewritten, by hand where prose (§10) |
| `plans/*.md` | 75 | history: not rewritten. `plans/README.md` gains one line: "family keys and paths in plans before 0101 are the pre-0101 ones; see 0101 §0.1" |
| `src/wsbench/basic/family.py` | | `MC_FAMILIES = {"basic_readout_st", "multihop_st"}`, `MC_BANK_FAMILIES = {"multihop_st"}`; default bank via `family_rel` |
| `src/wsbench/basic/prompts.py` | | `FAMILY_PROMPT_VERSIONS`, `DIGEST_TASK_NOTES`, `FAMILY_RULES` keys -> `_st` (F4) |
| `src/wsbench/basic/mcjudge_basic.py`, `mcjudge_bank.py` | | `CANNOT_OF`, `OPTIONS_VERSIONS`, `CALL_VERSIONS` keys -> `_st`; the `family: str = "multihop"` defaults (364-526) -> `"multihop_st"`; F1 |
| `src/wsbench/multitoken/{family,prompts,options,mcrx,regex}.py` | | `MC_FAMILIES`, `CALL_VERSIONS`, `BRMT`, `MC_PROFILES` users, `RAW_REGEX_FAMILIES`, `DIGEST_TASK_NOTES`, `ML_BANK_TAGS`, `ALIAS_FAMILIES`, `PREMISE_FAMILIES`, `LANG_CONFUSABLES`: multi-token keys lose `_mt`, the single-token `multilingual` entries become `multilingual_st`. The VALUES (stamps, notes, tags) do not change |

## 7. File tree (what moves; `->` = rename, bytes identical unless marked `*` = rewritten)

```
evals/
  basic/                         (new)   association/ basic_readout/ basic_readout_st/
                                         directed_modulation/ multi_concept_directed_modulation/
                                         multihop/ multihop_st/ multilingual/ multilingual_st/
                                         multilingual_multihop/ multilingual_typo/ poetry/
                                         typo/ typo_st/
  computational/                 (new)   arithmetic_intermediates/ buggy_code/ chain_intermediates/
  association/                   (was the family; now the category)
                                         conjunctive_association/ user_modeling/
  safety/                        (new)   agentic_misalignment/ jailbreak_recognition/
  anti_bag_of_features/          (new)   relational_multihop/ role_bound_association/
  logical_processing/            (new)   moral_rationale/ nyt_connections/
  model_specific/                (new)   hinted_cutoff/ introspection/ knowledge_cutoff/
                                         model_multihop/ model_specific_directed_modulation/
                                         wrong_intermediate/
  precision/                     (new)   jlens_concept_pr/
  baselines/                     (stays) *.json keys in place*; topics/ confusers/ 8 renames
  fast/                          (stays) untouched
  hallucinationbench/            (stays) untouched
    evals/basic_readout_mt/ -> evals/basic/basic_readout/
    evals/basic_readout/    -> evals/basic/basic_readout_st/
    evals/multihop_mt/      -> evals/basic/multihop/
    evals/multihop/         -> evals/basic/multihop_st/
    evals/multilingual_mt/  -> evals/basic/multilingual/
    evals/multilingual/     -> evals/basic/multilingual_st/
    evals/typo_mt/          -> evals/basic/typo/
    evals/typo/             -> evals/basic/typo_st/
    evals/association/      -> evals/basic/association/     (before evals/association/ is re-created)
    evals/<other family>/   -> evals/<category>/<family>/
src/wsbench/evals/
  __init__.py                    *
  <category>/__init__.py         (8 new, docstring only)
  <category>/<family>/…          (same mapping as evals/; every file *)
src/wsbench/registry.py reads.py runner.py results.py cli.py fast.py readplan.py digest.py
            split.py readouts.py nocot.py                                      *
src/wsbench/basic/*.py multitoken/*.py baselines/*.py capable/*.py adapt/*.py
            viewer/build.py produce/producer.py                               *
adapted/<17 sets>/               125 bank renames, 16 overlay dir renames, 62 JSON keys in place*
examples/readouts/               9 renames
scripts/migrate_0101.py          (new)
tests/test_rename_0101.py test_family_paths.py                               (new)
tests/golden/rename_0101_pre.json  script_path_literals_0101.txt  bank_sha256_0101.txt  (new)
tests/test_<family>*.py          about 20 renames; 212 files *
README.md CLAUDE.md AGENTS.md NOTICE.md adapted/README.md evals/*/README.md
docs/{producing_readouts,summarizer,adapt_judge_inputs}.md .claude/skills/**    *
```

## 8. Execution: `scripts/migrate_0101.py` (run once; kept in the repo as the record)

The implementer writes the script and the hand edits; the script does every move and every
mechanical rewrite from literal tables at its top. Subcommands:

| subcommand | does |
|---|---|
| `snapshot` | BEFORE anything moves: writes `tests/golden/rename_0101_pre.json` (per pre-0101 key: `plan_sha`, fast `plan_sha` per subset version, ATI `bank_fingerprint`, `prompt_version`, call versions, a sha256 over every upper-case constant of the family's `prompts` module as `test_rename_multihop_mt.py` does, the shown option lists' sha per item for the MC families, `lucky_guessing` seeds of 3 probe items, `split.is_dev` of every id, `capable` cache keys of 3 probe items) and `tests/golden/bank_sha256_0101.txt` (sha256 of every file under `evals/`, `adapted/`, `examples/`, `tests/golden/`, `tests/fixtures/`, `src/wsbench/adapt/data/`, keyed by path) |
| `plan` (dry run, the default) | prints every planned `git mv`, every file it would rewrite with its hit count per rule, every JSON key change, and the RESIDUE (below); changes nothing |
| `apply` | refuses a dirty tree; runs phases 1-5 of §8.2; ends with `check` |
| `check` | the post-checks of §8.3; exit 1 on any failure |
| `set <dir>` | migrates one adapted set outside git (bank renames, overlay dir, `report.json` / floors / ATI / scores keys, `gate/` and `cand/` work files, the marker); `--dry-run` by default; refuses a set that already has the marker |
| `rewrite <paths…>` | phase 3 only, on the given files: for rebasing an in-flight branch (§8.5) |

### 8.1 Tables and rules

- `KEYS` = `PRE_0101_KEYS` (a test asserts the script's literal equals `registry.PRE_0101_KEYS`).
- `CATEGORY` = the family -> category map of §0.2 (a test asserts it equals the registry's
  groups), giving `DIRS`: `evals/<old>` -> `evals/<category>/<new>` and
  `src/wsbench/evals/<old>` -> `src/wsbench/evals/<category>/<new>` for all 32 families.
- Text rewriting is ONE pass per file with ONE compiled alternation and a callback, so a swap
  cannot chain (`multihop` -> `multihop_st` is never re-read as `multihop`), and alternatives
  are ordered longest first. A key matches only with
  `(?<![A-Za-z0-9_])<key>(?![A-Za-z0-9_])` AND inside one of these contexts; anything else is
  left and reported:

  | rule | pattern (key = any of the 9 old keys unless stated) | becomes |
  |---|---|---|
  | R1 import | `wsbench.evals.<any family>` (dotted; also `from wsbench.evals import <family>`, `wsbench/evals/<family>` in paths) | `wsbench.evals.<category>.<new>` |
  | R2 data path | `evals/<any family>` not preceded by `wsbench/` or an identifier character, followed by `/`, a quote, whitespace or `)` | `evals/<category>/<new>` |
  | R3 path join | `"evals" / "<family>"` | flagged for hand edit to `registry.family_root` in live code; rewritten to `"evals" / "<category>" / "<new>"` in `scripts/` |
  | R4 quoted key | `"<key>"`, `'<key>'` (the whole string literal) | new key |
  | R5 CLI | `family=<key>`, `families=…,<key>,…`, `opts`-free shell words in `.sh` / `.md` code spans | new key |
  | R6 file stem | `<key>.jsonl`, `<key>.json`, `<key>.jsonl.meta.json`, `/<key>/` under `adapted/`, `readouts`, `runs` path text | new key |
  | R7 markdown | `` `<key>` `` (whole code span), `**<key>**` | new key |
  | R8 env | `WSB_BANK_FILE_<KEY upper>` (5 spellings occur: `BASIC_READOUT`, `BASIC_READOUT_MT`, `MULTIHOP`, `MULTIHOP_MT_V2`; 7 files) | upper-case new key |
  | R9 identifiers | python identifiers that embed a key in the listed files only (`_basic_readout_mt`, `test_typo_mt_…`): an explicit per-file list in the script | per list |

  The word-boundary rule alone already keeps `multilingual_multihop`, `multilingual_typo`,
  `relational_multihop`, `model_multihop`, `multihop_mt_regex`, `multihop_mc_options`,
  `basic_readout_mt_options_v2`, `typo_mt_v2_options` whole (an identifier character follows or
  precedes the key). The context rules are needed because `typo`, `multilingual` and
  `multihop` are also English words and field values: `producer.py:78` "so a typo fails fast",
  `readplan.first_diff(typo, target)`, bank fields.
- RESIDUE: every word-boundary hit that no rule rewrote. `plan` prints it; `check` compares it
  to the committed allowlist `scripts/migrate_0101_residue.txt` (`path:line:text`, reviewed by
  the implementer line by line: prose words, bank-header vocabulary of F9, golden file names,
  historical stamps and messages inside `REMOVED_FAMILIES`). An unlisted residue line fails.

### 8.2 Phases of `apply`

1. `snapshot` must exist and match the current tree (re-computed and compared).
2. Moves, two-phase to make every swap and the `association` case collision-free:
   phase A `git mv <src> <staging>/<n>` for every source (dirs: `evals/`, `src/wsbench/evals/`;
   files: adapted banks, overlay dirs, examples, topics, confusers, test modules) into
   `.migrate_0101_staging/` (same filesystem, inside the repo, removed at the end);
   phase B create the category dirs and `git mv <staging>/<n> <dest>`. Then write the 8
   category `__init__.py`.
3. Text rewrite (§8.1) over: `src/**/*.py`, `tests/**/*.py`, `scripts/**/*.{py,sh}`,
   `atlas/**/*.{py,sh}`, `viewer_app/*.py`, `.claude/skills/**/*.{py,md}` (minus the two audit
   records), the three live docs, every moved family `README.md`, `evals/baselines/README.md`,
   `evals/fast/README.md`, `adapted/README.md`, `pyproject.toml` (package data globs, if any).
4. JSON key rewrite (§6 "in place") with the per-file checks.
5. `ruff format` + `ruff check --fix` on the rewritten python (import order changes when a
   dotted path grows a segment).

The script must NOT touch: `plans/`; `evals/fast/*.json`; `evals/baselines/archive/`; any
`items*.json`, `manifest.json`, `*.npy`, pool or resource file (bank bytes); `tests/golden/`
and `tests/fixtures/` (except writing its own three new files); `src/wsbench/adapt/data/`;
`adapted/*/AUDIT*.md`, `NOTES.md`, `AUDIT_RESPONSE.md`, set READMEs; the five results docs;
the two skill audit records; `uv.lock`; `CITATION.cff`; `.github/`; `LICENSE`; any stamp,
sha, number or id list anywhere.

### 8.3 `check`

- every path in `bank_sha256_0101.txt`, mapped through `DIRS` / the rename tables, has the same
  sha256 now (prints a manifest diff; any difference, missing or extra data file fails);
- `git diff -M100% --diff-filter=R --stat` covers every moved data file (no data file shows as
  modified); `git status` has no stray file; the staging dir is gone;
- no directory other than the 8 categories and `baselines`, `fast`, `hallucinationbench` under
  `evals/`; no module other than the 8 category packages under `src/wsbench/evals/`;
- the residue equals the allowlist;
- `python -c "from wsbench import registry; registry.load_all()"` registers 32 families whose
  `group` equals their directory.

### 8.4 Hand edits after `apply` (not mechanical)

1. `registry.py` (§3, §4): all of it.
2. `reads.py`, `runner.py`, `results.py`, `cli.py`, `fast.py`, `digest.py`, `readouts.py`
   (§5): the guards; `split.py`, `basic/mcjudge_bank.py`, `capable/run.py`,
   `adapt/generate.py`, `adapt/author.py` (F1, F2, F5, F6); `baselines/logit_lens.py` (F7);
   `produce/producer.py` (F3); `adapt/store.py`, `adapt/run.py`, `adapt/__main__.py` (§5.5).
3. The R3 sites: `REPO_ROOT / "evals" / family / …` in live code -> helpers.
4. The eight `__init__.py` of the renamed families: titles (§0.1), docstrings,
   `superseded_by="typo"`, `bank=registry.family_rel(…) / …`. In `multilingual_st`,
   `DIGEST_TASK_NOTES["multilingual_st"]` and `ML_BANK_TAGS["multilingual_st"]` must select the
   SAME strings as today's `["multilingual"]` entries.
5. The two earlier rename tests and the new tests (§9).
6. Prose: `README.md` (family list regrouped paths, Judges table, the rename paragraph,
   Quickstart examples), `CLAUDE.md` (§10), `AGENTS.md`, `NOTICE.md`, family README titles,
   `atlas/site/*`, `atlas/run_normal_judge.sh`, `scripts/adapt_set.sh`.
7. `tests/golden/README.md`, `adapted/README.md`, `plans/README.md`: the key table.

### 8.5 In-flight branches

A branch cut before this PR rebases as: rebase onto the commit BEFORE the merge; run
`scripts/migrate_0101.py rewrite $(git diff --name-only <base>)` plus the moves for files it
adds; then rebase onto the merge. Stated in the PR body. Local adapted sets: `migrate_0101.py
set <dir>`.

## 9. Tests

### 9.1 Sanctioned mechanical edits

The script may rewrite tests for keys (R4-R9), paths (R2) and imports (R1), and rename test
modules by this table (the implementer confirms each by the module the file imports; a file
that tests both members of a pair or all four multilingual families keeps its name):

| today | after |
|---|---|
| `test_basic_readout_mt.py`, `test_basic_readout_mt_sarin_drop.py` | `test_basic_readout.py`, `test_basic_readout_sarin_drop.py` |
| `test_basic_readout.py`, `test_basic_readout_{bank_v7,quote_line,slices,sum_check}.py` | `test_basic_readout_st.py`, `test_basic_readout_st_{…}.py` |
| `test_multihop_mt.py`, `test_multihop_mt_kant_option.py` | `test_multihop.py`, `test_multihop_kant_option.py` |
| `test_multihop.py`, `test_multihop_{bank_v5,derived_forms,dropped_items,mc_bank_skip,quote_line,target_prefix}.py` | `test_multihop_st.py`, `test_multihop_st_{…}.py` |
| `test_multilingual_mt.py` / `test_multilingual.py` | `test_multilingual.py` / `test_multilingual_st.py` |
| `test_typo_mt.py` / `test_typo.py` | `test_typo.py` / `test_typo_st.py` |
| `test_rename_multihop_mt.py`, `test_legacy_family_names.py`, `test_mt_*.py`, `test_multilingual_{cited_case,language_leg,lexicons,multihop,typo}.py`, `test_final_r1_multilingual.py`, `test_r3_multilingual_anchor.py` | names kept |

Review rule for the main session: `git diff -- tests/` is read separately. A changed line in a
pre-existing test is acceptable only if it is (a) a key, path, import or module-name
substitution from the tables, or (b) listed in §9.2. Any other changed number, stamp, sha, id
list, count or expected string is a red flag and blocks the merge until explained.

### 9.2 Assertions whose VALUE changes (the complete list; anything else is a defect)

| id | test | change | why it is right |
|---|---|---|---|
| V1 | `tests/test_basic_prompts.py` `PINNED["bank-2026-10-30b"]` | the sha constant | F4: the printed record's keys are renamed; `PROMPT_VERSION` and every sent prompt are unchanged. Same kind of re-pin as plans 0050 and 0058; a comment is added in that style. The test additionally asserts the record with its keys mapped back through `hash_name` hashes to the OLD constant, which proves that only the keys moved |
| V2 | `tests/test_rename_multihop_mt.py` | `test_new_name_is_the_family`, `test_old_name_is_a_former_key`, `test_former_key_files_win_under_the_reused_key`, `test_adapted_set_under_the_old_name*`, `test_real_adapted_sets_plan_multihop_mt`, `test_adapted_reports_keep_the_removed_familys_drops_apart` are rewritten for the key `multihop`: `lookup_names("multihop") == ["multihop", "multihop_mt_v2", "multihop_mt"]`; `canonical("multihop_mt_v2") == canonical("multihop_mt") == "multihop"`; the note text names `multihop`; the real-sets test asserts NO in-repo set ships `multihop_mt_v2.json` any more and every set that has the family plans it. KEPT with their constants: `PRE_PLAN_SHA`, `PRE_BANK_SHA`, `PRE_PROMPTS_SHA`, `PRE_STAMP`, `PRE_CALL_VERSION`, `test_hashes_unchanged_by_the_rename`, `test_instrument_and_prompt_bytes_unchanged_by_the_rename`, `test_seeds_unchanged_by_the_rename`, and `test_stored_results_are_told_apart_by_stamp` with the same five stamps and outcomes (`multihop_mt_regex` twice, the live family three times) | the plan rewrites the decision these tests encode; the retained invariants are restated in §3.1 / §3.2 |
| V3 | `tests/test_legacy_family_names.py` | `test_map_and_canonical` gains the five new entries; nothing else | §3.1 |
| V4 | tests asserting `spec.bank == Path("evals/<f>/…")`, `EvalSpec.group` directory, README link text | path values per `DIRS` | 0.2 |
| V5 | `wsbench list` / README / viewer title assertions | titles per §0.1 | decision A |
| V6 | tests asserting the exact `note: family … was renamed to …` line or `fast.families()` / `tiers` / `test_tiers.py` family-to-category literals | key names only | 0.1 |

No count, pass rate, floor, `plan_sha`, `bank_sha`, stamp, call key, option list or id list
changes.

### 9.3 New tests

`tests/test_rename_0101.py` (parametrized over the four pairs unless noted):

- T1 hash neutrality: for each family, by its PRE key in `rename_0101_pre.json`: `plan_sha`,
  fast `plan_sha` per subset version, ATI `bank_fingerprint`, `prompt_version`, call versions,
  prompts-module constants sha (V1's record excluded by name), shown option lists, lucky
  seeds, `split.is_dev`, `capable` keys all equal the snapshot. Plus `plan_sha_pre_0090.json`
  holds with keys through `rekey_pre_0101` and values unchanged.
- T2 stamps: `stored_family(<bare key>, <st stamp>) == <_st>` and `… <mt stamp>) == <bare key>`
  for every historical stamp listed in §3.2 with and without `+digest-…`, `+reads-lite`,
  `+fast-…` suffixes; empty stamp -> the live family; an unknown stamp -> `RenamedKey`; old
  keys (`basic_readout_mt` etc.) -> the live family; each registered holder's current
  `prompt_version` classifies as itself; `read_results` + `macro` over a pre-0101 results tree
  (old `typo` with `bank-2026-10-30b` -> `typo_st`, excluded `superseded`; old `typo_mt` ->
  `typo`, in the macro).
- T3 readouts: a pre-0101 root (`<bare>.jsonl` with single-token ids, `<bare>_mt.jsonl` with
  multi-token ids) and a post-0101 root each serve BOTH families through
  `runner.readouts_path`; `reads.load(family=<bare>)` on a single-token file fails with the
  rename message (a) by sidecar `plan_sha`, (b) sidecar-less by ids; with `allow_missing=True`
  too; a mixed-id file fails; `judge family=<bare> readouts=<st file>` exits 2 with no call.
- T4 banks under a root: `tests/fixtures/adapted_v1` (pre-0101 names) resolves
  `bank_path("multilingual")` to `multilingual_mt.json` and `bank_path("multilingual_st")` to
  `multilingual.json` by header; a root holding only the single-token bank under the bare key
  gives "has no bank for 'multihop'" naming the rename; `WSB_BANK_FILE_MULTIHOP=<st bank>`
  exits naming the rename; every in-repo set resolves all of its eight-family banks, and each
  resolved file's header value is its family's (F9 table).
- T5 id disjointness within each pair over every registered bank version and every in-repo
  adapted bank.
- T6 adapt: a write subcommand on a marker-less set exits 2; `store.report_key` on a pre-0101
  report (the mixed `multihop_mt_v2` / `drops.multihop_mt` shape of glm-5.3 before migration,
  as a fixture dict) attributes every entry correctly; a new adapted bank's header value is the
  registered one.
- T7 out dirs and digest stores: `family_dir` on a pre-0101 out root returns `out/multihop` for
  `multihop_st` and `out/multihop_mt` for `multihop` (by `results.json`, and by `cells.jsonl`
  ids when there is no `results.json`); `out/multihop` holding single-token results and no
  other candidate -> `RenamedKey` for `multihop`; the same for a `summ_store`.
- T8 CLI: `families=multihop_mt` and `multihop_mt_v2` resolve to `multihop` with the rename
  note; a bare key prints the one-line notice once, not under `WSB_NO_RENAME_NOTICE=1`, not
  for `judge outcome=…`; `_st` keys are silent; `wsbench list` shows the 32 keys of §0.1 /
  §0.2 with the new titles.
- T9 `HASH_FAMILY_NAMES` is referenced only in `registry.py` (source scan);
  `hash_name` of each of the nine entries equals §3.1.
- T10 fast: `fast.subset(v)` for the three versions has the translated keys, the file sha pins
  still hold, and each translated entry's `bank_sha` equals `fast.bank_sha(<new key>)`.
- T11 floors: every entry of the five `evals/baselines/*.json` files and of every adapted
  `floors/*.json` under a bare or `_st` key carries a stamp that `registry.owner` attributes
  to that key; the archive read through `rekey_pre_0101` has `_st` keys.

`tests/test_family_paths.py` (TP): the three scans of §4, plus `registry._layout()` equals the
§0.2 table as a literal, plus `family_module` / `family_root` / `repo_path` unit cases
(`repo_path("evals/introspection/vectors/concept_vectors_L12.npy")` exists;
`repo_path("evals/baselines/prompt_only.json")` is unchanged).

## 10. Docs and conventions

- `README.md`: each family entry's link becomes `](evals/<category>/<family>/README.md)`
  (`tests/test_readme.py:51` builds it from `registry.family_rel`); the eight entries get
  their new titles and keys; the "Renamed families" paragraph gains the table of §0.1 with
  the date; Quickstart and Judges rows use the new keys.
- Family README per directory: unchanged convention, one level deeper. `test_prompts_in_readme`
  reads `registry.family_root(family) / "README.md"` and `registry.family_module(family,
  "prompts")`. No category-level README is added (Q4).
- `tests/test_adapt_skill_docs.py`: unchanged logic (it checks subcommands, scripts and the
  registered-bank table against the registry); the skill's bank table is rewritten by the
  script, and scan 3 of §4 covers the skill's paths.
- `CLAUDE.md`: "Renamed families" gains a 0101 paragraph (the tables of §3.1 / §3.2, the
  content rule, the retained `multihop_mt_v2` / `multihop_mt_regex` behaviour, F7's rule);
  "Family conventions" paths and keys; "Fast subset" (keys translated at load, files frozen);
  "Tiers and categories" (a category is a directory); "Invariants" (the §4 rule and its test);
  "Adding a family": `src/wsbench/evals/<category>/<family>/…`, `evals/<category>/<family>/…`,
  `bank=registry.family_rel(...)`, and "the directory is the category; `register` checks it".
- `AGENTS.md`, `NOTICE.md` (7 `evals/<family>` paths, one import path), `docs/producing_
  readouts.md` (keys; the note that `extra.steering.vector_file` is a pre-0101 layout string
  resolved by `registry.repo_path`, F3).

## 11. Verification gates (all required before merge; run the long ones in tmux, e.g.
`tmux new -s wsb0101` with output to `logs/migrate_0101_<timestamp>.log`)

1. `scripts/migrate_0101.py check` passes (§8.3), the manifest diff is empty.
2. `uv run pytest -q` green; `uv run ruff check .`; `uv run ruff format --check .`.
3. T1 (the golden hash-neutrality test) green, and `git diff --stat -- tests/golden
   tests/fixtures` shows only the three new files.
4. `scripts/ati_rederive.py --check` passes on `evals/baselines/anti_text_inversion.json` and
   on the adapted ATI files it covers.
5. `uv run wsbench list` shows the new keys, titles, categories and tiers; `uv run wsbench plan
   families=all` writes rows whose per-family sha equals the snapshot.
6. Replay, zero new calls, identical values. BEFORE running `apply`, on main, copy these run
   dirs and record each `results.json`'s `value`, `ci95`, `counts`, `n_items` and row passes:

   | pre-0101 key | readouts | cached out dir |
   |---|---|---|
   | basic_readout | `/root/tmp/wsb-fast-v2/readouts/s3d-rl600/basic_readout.jsonl` | `/root/tmp/wsb-fast-v2/runs/s3d-rl600/basic_readout/` |
   | basic_readout_mt | `…/readouts/s3d-rl600/basic_readout_mt.jsonl` | `…/runs/s3d-rl600/basic_readout_mt/` |
   | multihop_mt | `…/readouts/s3d-rl600/multihop_mt.jsonl` | `…/runs/s3d-rl600/multihop_mt/` |
   | multihop, multilingual, typo_mt | the `readouts_root` recorded in the run's `run.json` | `/root/tmp/x3q11/judged/qglm-rl600-x4/all/{multihop,multilingual,typo_mt}/` |
   | multilingual_mt | as recorded in its `run.json` | `/root/tmp/wsb-0079/judged/qwen3.5-2b/qwen3.5-2b.sft/multilingual_mt/` (an adapted set: run with its `WSB_BANK_ROOT`) |
   | typo | no cached run dir found on this box | make one on main first: `wsbench judge family=typo` over `examples/readouts/typo.jsonl` with a live `limit=3` smoke, then replay it |

   AFTER, with the same flags (`--fast` for the fast-v2 dirs), judge each copy under the NEW
   key (`family=basic_readout_st readouts=…/basic_readout.jsonl out=<copy>` etc., and once as
   `run readouts_root=… out=<copy of the whole pre-0101 root>` to exercise the lookups) with
   `dry_run` off. Pass = exit 0, the copy's `cells.jsonl` (append-only) gained ZERO rows, spend
   is the preflight ping only, and every recorded number is identical; the only differences
   allowed in `results.json` are `family` and timestamps. Also judge each
   single-token file under its bare key and confirm the refusal of §5.2.
7. `scripts/gated_merge.sh <pr>` (fail closed), after an independent review of the diff with
   `git diff -M --stat` and `git diff -- tests/` read against §9.

## 12. PR #510 (plan 0097, banks as password zips)

This PR lands first. Afterwards #510 changes one thing: its unit definition. A unit under
`evals/` is a family directory `evals/<category>/<family>/` (enumerate with
`registry._layout()` / `family_root`, not by listing `evals/*`), while `evals/baselines`,
`evals/fast` and `evals/hallucinationbench` remain units at the second level;
`adapted/<model>` units are unchanged. Its sidecar names and pack / unpack paths follow from
that. Nothing else in #510 changes.

## 13. Risks and their fail-closed behaviour

| risk | behaviour |
|---|---|
| a pre-0101 readouts file of the single-token family is judged under the bare key | refused in `reads.load` by sidecar `plan_sha`, else by item ids (§5.2); `run` does not even select it (§5.3) |
| a pre-0101 `results.json` under a bare key | attributed by stamp; an unknown non-empty stamp is `RenamedKey`, exit 2 (§3.3) |
| a pre-0101 out dir or digest store reused | `family_dir` picks by `results.json` / cache ids; the partner's directory under the family's own key is `RenamedKey`; never appended to |
| a pre-0101 adapted set (local, unpushed) | reads: bank picked by header (F9), report entries by bank / ids; writes: refused without the marker until `migrate_0101.py set` |
| a stale floors or ATI file keyed the old way | a floor draws only on stamp equality; ATI entries fail their `bank_sha` / instrument guard (exit 2); loaders also refuse a partner-stamped entry |
| `WSB_BANK_FILE_<BARE>` set by an old shell | header check -> exit naming the rename |
| an old plan file fed to `produce` | rows refused by id; any output fails guard (iii) |
| a stale import or path in an unmigrated branch, script or notebook | `ModuleNotFoundError` / missing file (§0.3); pre-0101 path messages name the move |
| the stamp tables miss a historical stamp | `RenamedKey` (refused, never guessed); T2 pins the known ones; the implementer's `git log -S` check (§3.2) |
| a hash name used as a lookup key (F7) | T9 forbids it |
| option order of single-token multihop moves (F1) | `hash_name` in the seed; pinned by the existing option goldens and T1 |
| the script rewrites an English word or a bank value | context rules + residue allowlist + untouched-bytes manifest (§8.3) |
| the `association` family / category collision (F8) | two-phase move; `_layout()` rejects a family in two categories; category packages export nothing |
| fast files edited by mistake | sha pins in `test_fast_subset.py` unchanged and still asserted |
| process-mode children spam the swap notice | suppressed under `outcome=` (§5.6), tested (T8) |
| a published plan row names a vectors path that no longer exists (F3) | `Producer` resolves through `repo_path`; documented; Q2 |
| external consumers (HF datasets, the viewer app's caches, HallucinationBench) keyed by old names | out of scope here; Q3 |

## 14. Open questions for the owner

- Q1. The category directory `association` now sits where the family `association` was, and
  the family lives at `evals/basic/association/`. It is consistent with "category =
  `EvalSpec.group` key" and fails loudly for stale paths, but it reads oddly. Default: keep
  as specified (no category rename).
- Q2. Introspection's steering-vector path (F3). Default: keep the plan-row string
  `evals/introspection/vectors/…` (hash-neutral, zero golden changes) and resolve it in the
  producer. Alternative: emit the real path in the row and map it back to the old string only
  when hashing; rows are then self-describing for external producers, at the cost of a second
  hashing rule and changed plan-row goldens. Does any external producer read `vector_file`
  itself?
- Q3. Public artefacts outside this repo that are keyed by family name (the pushed adapted
  sets on HF, viewer / atlas exports, HallucinationBench's WSB method names): rename there
  too, or leave and document the table? Default: leave, document; the code reads both.
- Q4. A short `README.md` per category directory (title + its family list)? Default: no; the
  README's grouped list is the index.
- Q5. How long the bare-key notice stays (§5.6). Default: until the next tagged release, then
  delete `registry.SWAP_NOTICE` and its test.
- Q6. `tests/golden/` and `tests/fixtures/` file NAMES keep their pre-0101 keys (e.g.
  `basic_readout_options.json` pins the single-token family) so that the pins show a zero
  diff. Default: keep, with the key table in `tests/golden/README.md`. Rename them in a
  follow-up if the names bother.
