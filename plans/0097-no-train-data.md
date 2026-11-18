# Plan 0097: the banks leave the plaintext tree (do-not-train protection)

Status: draft. Owner: Camila (2026-11-18: "make it so that it's not allowed to train on this
data, kind of like nocot bench"; her choices: **encrypted zip, like nocot**, no history
rewrite; scope **registered banks + adapted sets**; examples, test fixtures and README example
lines stay as they are).

## What changes, in one paragraph

Today `git ls-files evals adapted` is 7,626 plaintext files (646 MB) that any crawler of the
repo page or of a raw URL reads. After this plan the git tree holds, for those two
directories, only **password-protected zips** under `data/` plus Markdown docs; the plaintext
is materialised into the working tree (the same paths as today, now git-ignored) by one
command that also runs automatically. No loader, judge, script or test changes the path it
reads or the bytes it gets. A do-not-train notice and a canary string are added. Git history
before this commit keeps the plaintext (Camila: no rewrite).

Why "unpack to the working tree" and not "read from the zip at runtime" (nocot's own reader
does the latter for 43 items): about 350 loader call sites, 40 direct reads in `src/`, 70
reads and 60 writes of `adapted/<m>/` intermediates in `src/wsbench/adapt/`, 95 scripts and
130 test files open these paths, and the adapt pipeline writes there. A virtual filesystem
would touch all of them; unpacking touches none, and the protection is the same: nothing a
crawler fetches is plaintext.

## Invariants

- **Byte-identical plaintext.** After `unpack`, every file that was tracked under `evals/` or
  `adapted/` on the parent commit exists at the same path with the same bytes (a test
  compares against a committed list of `(path, sha256)`). Hence no `bank_sha`, ATI
  `bank_sha`, `plan_sha`, sha-pinned test or golden moves, and the full suite passes with no
  test edited.
- No judge prompt, call key, fingerprint or `prompt_version` moves.
- The password is not a secret: it is printed in the README for people. The goal is that no
  URL of the repo serves bank text to a crawler, not access control.

## 1. Layout

```
data/
  README.md                 what this is, the password, the do-not-train request, the canary
  MANIFEST.json             {version, canary, units: {<unit>: {zip, zip_sha256, files: {<path>: sha256}}}}
  evals/<family>.zip        one unit per evals/<entry> (a family dir; also baselines, fast, hallucinationbench)
  adapted/<model>/<entry>.zip   one unit per adapted/<model>/<entry> (a bank file or a subdirectory)
```

- A **unit** is one second-level entry of `evals/` (`evals/<x>/`) or one third-level entry of
  `adapted/` (`adapted/<model>/<x>`, file or directory). Small units keep a bank edit to one
  small new blob in git instead of a 600 MB one, and every zip stays far below GitHub's
  100 MB file limit (the largest plaintext unit today is ~15 MB; the implementer verifies the
  largest zip and stops if any exceeds 50 MB).
- **Packed:** every file under `evals/` and `adapted/` that is tracked on the parent commit,
  EXCEPT `evals/**/*.md` and `adapted/README.md` (the family docs and the adapted index stay
  plaintext and tracked: Camila left README example lines out of scope, and
  `tests/test_prompts_in_readme.py` reads them). `adapted/<m>/**/*.md` (AUDIT files: they quote
  items, golds and answers) ARE packed. Going forward "tracked" no longer defines the set, so
  the rule is: every file under the two directories that is not excluded by
  `data/PACK_EXCLUDE` (the work-product patterns that `.gitignore` lists today for `adapted/`,
  moved there verbatim with the same semantics, plus `*.lock`, `__pycache__`), and is not one
  of the plaintext Markdown keepers above. A test pins that this rule, applied to the unpacked
  tree, yields exactly the manifest's file set.
- **Encryption:** AES-256 zip via `pyzipper` (new core dependency: every user needs `unpack`).
  Stdlib `zipfile` cannot write encrypted zips. Each zip also holds `CANARY.txt`.
- Zips are not byte-deterministic (random salts), so `pack` rewrites a unit's zip only when its
  plaintext file set or any file sha differs from the manifest; an untouched unit keeps its
  committed zip byte for byte.

## 2. `src/wsbench/datapack.py` and the CLI

- `wsbench unpack [force=True]`: for each unit whose stamp is missing or stale, verify the
  zip's sha256 against the manifest, extract to the plaintext path, verify each file's sha256.
  A stamp file `data/.unpacked.json` (git-ignored) records `{unit: zip_sha256}` so a second
  call is a no-op costing one small read. Files of a unit that are in the working tree but not
  in the zip are left alone (work in progress); a plaintext file that DIFFERS from the zip's
  is never overwritten without `force=True`: `unpack` reports it and exits 2 (local edits are
  never silently lost).
- `wsbench pack [units=a,b] [check=True]`: recompute the plaintext set and shas per unit,
  rewrite the zips and manifest entries that changed, delete zips of units that no longer
  exist. `check=True` writes nothing and exits 1 listing the units whose plaintext differs
  from the manifest.
- **Automatic unpack.** `wsbench.registry` (imported by every loader) calls
  `datapack.ensure()` once per process: when `data/MANIFEST.json` exists and the stamp is
  missing or stale it runs `unpack` (a file lock makes concurrent processes safe: `run`'s
  per-family children, pytest); when the stamp is current it does nothing. `WSB_NO_UNPACK=1`
  disables it. A wheel install has no `data/` beside `src/` and is unaffected (as today it
  has no banks). Scripts that read `REPO/"evals"` without importing `wsbench.registry` need
  `wsbench unpack` once; the README says so.
- The password constant lives in `datapack.py` beside a comment like nocot's ("published in
  data/README.md: for people, not for crawlers").

## 3. The cutover commit

- `python -m wsbench.datapack pack` builds `data/` from the current tree; `git rm --cached`
  every packed file; `.gitignore` gains `/adapted/*` with `!/adapted/README.md`, and for
  `evals/`: ignore everything but directories and `*.md`. The existing `adapted/…`
  work-product ignore lines move to `data/PACK_EXCLUDE`.
- `tests/golden/data_files_0097.json`: the `(path, sha256)` list of the packed files, computed
  from the parent commit's blobs (`git ls-tree`), not from the working tree; the invariant test
  reads it.
- `.github/workflows/ci.yml`: no new step is required (the registry unpacks), but an explicit
  `uv run wsbench unpack` step before `pytest` makes a failure legible; add it.
- Modal images that `add_local_dir("evals")` keep working from an unpacked checkout
  (`scripts/lucky_guess_modal.py`, the wsb-new-questions skill scripts): each gets a guard
  that calls `datapack.ensure()` before building the image. `atlas/` and `viewer_app/` read a
  hard-coded external checkout and are not changed.

## 4. Notice and canary

- A canary string, one fresh GUID for the benchmark: `WORKSPACEBENCH CANARY GUID <uuid4>`,
  in `data/README.md`, `data/MANIFEST.json`, `CANARY.txt` inside every zip, README.md and
  NOTICE.md. It is NOT added to any bank file (that would move every `bank_sha`).
- README.md gets a short section "Do not train on this data" (above Quickstart) and NOTICE.md
  the same request: the banks and adapted sets are evaluation data; please do not train on
  them or republish them unpacked; the data is shipped in password-protected zips for that
  reason; the password; the canary and what it is for. LICENSE stays MIT for code and in-house
  items (the request is a request, as nocot's is; changing the licence is Camila's call and
  is not made here). CITATION.cff untouched.
- README §Quickstart: `wsbench unpack` happens automatically; `wsbench pack` before committing
  a bank change. CLAUDE.md (Workflow + a "Data packing" bullet), AGENTS.md, `adapted/README.md`
  and the two skills (`adapt-bank`, `wsb-new-questions`) get the pack-before-commit rule; the
  adapt-bank push recipe ("copy the set's files into a fresh clone") becomes "copy, then
  `wsbench pack`".

## 5. Not covered (stated in the PR)

- Git history before the cutover, forks and existing clones keep the plaintext.
- `src/wsbench/adapt/data/*.json` (the agentic pools, 10.8 MB of scenario text, shipped in the
  wheel and sha-pinned), `tests/fixtures/` and `tests/golden/` files that hold item text,
  `examples/readouts/`, and the README / family README example lines: out of the scope Camila
  chose.
- Branches and local trees with unmerged bank edits (the running adapted-set rebuilds) will
  conflict with the removal of the plaintext paths (modify / delete): keep the plaintext
  files, take `data/` from main, run `wsbench pack`.
- A crawler that runs code, or a person, can unpack: the protection is against scraping.

## Tests

- pack / unpack round trip on a temp tree (units, excludes, Markdown keepers, a deleted unit,
  `check=True`, a locally modified file is not overwritten, `force=True`, the stamp no-op,
  two processes racing on the lock, a corrupt zip or sha mismatch exits 2 with the unit named).
- the committed `data/` reproduces `tests/golden/data_files_0097.json` exactly (path set and
  sha256), and `pack check=True` is clean on a fresh unpack.
- no file under `evals/` or `adapted/` other than the Markdown keepers is tracked
  (`git ls-files`, skipped when not in a git checkout).
- the canary is in the five places and in no bank file.
- the whole existing suite passes unedited.

## Files

```
plans/0097-no-train-data.md
src/wsbench/datapack.py, src/wsbench/cli.py (pack / unpack), src/wsbench/registry.py (ensure)
data/**                                   new (zips, MANIFEST.json, README.md, PACK_EXCLUDE)
evals/**, adapted/** (non-Markdown)       removed from the index, git-ignored
.gitignore, .github/workflows/ci.yml, pyproject.toml (+pyzipper), uv.lock
README.md, NOTICE.md, CLAUDE.md, AGENTS.md, adapted/README.md, .claude/skills/*/SKILL.md
scripts/lucky_guess_modal.py, .claude/skills/wsb-new-questions/scripts/modal_*.py
tests/test_datapack.py, tests/golden/data_files_0097.json
```
