# Plan 0097: the banks leave the plaintext tree (do-not-train protection)

Status: draft v2 (critique round 1 applied). Owner: Camila (2026-11-18: "make it so that it's
not allowed to train on this data, kind of like nocot bench"). Her choices: **encrypted zip,
like nocot; no git-history rewrite; scope = registered banks (`evals/`) + adapted sets
(`adapted/`)**; examples, test fixtures, README example lines, the agentic pools under
`src/wsbench/adapt/data/` and audit Markdown under `evals/` stay as they are; **the zips live
as GitHub release assets, not in git** (2026-11-20, after the review measured ~130 MB at
cutover and ~180 MB a month of undeltable blobs if committed).

## What changes, in one paragraph

Today `git ls-files evals adapted` is 7,626 plaintext files (646 MB) that any crawler of the
repo reads. After this plan the git tree holds, for those two directories, only Markdown docs
and small **sidecars** under `data/` (file lists and sha256s, no item text); the content is in
**password-protected zips published as release assets** of this repo, downloaded once, cached,
and unpacked into the working tree at the same paths as today (now git-ignored). No loader,
judge, script or test changes the path it reads or the bytes it gets. A do-not-train notice
and a canary string are added.

Why unpack to the working tree rather than read from zips at runtime: ~350 loader call sites,
40 direct reads in `src/`, 130 reads / writes of `adapted/<m>/` intermediates in
`src/wsbench/adapt/`, 95 scripts and 130 test files open these paths. Unpacking touches none
of them and protects the same thing: no URL of the repo serves bank text.

## Invariants

- **Byte-identical plaintext.** After `unpack`, every file tracked under `evals/` or
  `adapted/` on the parent commit, except the seven empty `adapted/*/.report.lock` files (lock
  files the adapt store recreates; dropped from the data), exists at the same path with the
  same bytes. Verified ONCE at cutover (section 5), not by a committed golden.
- No `bank_sha`, ATI `bank_sha`, `plan_sha`, sha-pinned bank test, golden, judge prompt, call
  key, fingerprint or `prompt_version` moves.
- The password is not a secret (it is in `data/README.md`, for people, not for crawlers).

## 1. Layout

In git:
```
data/README.md                       what this is, the password, the request, the canary, how to unpack / pack
data/INDEX.json                      {"version": 1, "canary": "...", "repo": "camila-blank/workspace-bench"}
data/evals/<entry>.json              one sidecar per unit
data/adapted/<model>.json
```
- A **unit** is `evals/<entry>` (each of the 35 second-level entries: a family dir,
  `baselines`, `fast`, `hallucinationbench`) or `adapted/<model>` (16). 51 units; coarse units
  are fine because zips are not in git.
- A **sidecar** is `{"unit", "zip_sha256", "zip_bytes", "files": {"<repo-relative path>":
  "<sha256>"}}`, keys sorted, one file per line of the map so a bank PR shows which files
  changed. One sidecar per unit (no shared manifest to conflict on).
- **The pack set is the sidecars' file lists** (as git's index is today), not a sweep of the
  directory: `pack` updates files already listed; a new file is added only by `wsbench pack
  add=<path or glob>`; a removal only by `wsbench pack rm=<path or glob>`. Unlisted files in
  the working tree (work products such as `adapted/<m>/gate/`, `mc/`, caches) are never
  packed and are reported by `wsbench pack status` like untracked files. No exclude-pattern
  file exists.
- Never packed, tracked as today: `evals/**/*.md` and `adapted/README.md`.
  `adapted/<m>/**/*.md` (AUDIT files) ARE packed.

Outside git:
- Each unit's zip is the release asset `<zip_sha256>.zip` of release tag
  `data-<first two hex digits of the sha>` of `INDEX.json`'s `repo` (256 buckets: GitHub caps
  a release at 1,000 assets), URL
  `https://github.com/<repo>/releases/download/data-<xx>/<sha>.zip`. Content-addressed and
  never deleted, so every commit's sidecars stay resolvable.
- Encryption: AES-256 zip via `pyzipper` (a new core dependency in `pyproject.toml` /
  `uv.lock`), **imported lazily inside the pack / unpack functions only** (a dozen Modal
  images install hand-picked dependencies and mount `src/wsbench`; they have no `data/` and
  must keep importing `wsbench` without pyzipper). Each zip also holds `CANARY.txt`.
- Local cache of downloaded / built zips: `$WSB_CACHE/data/` or `~/.cache/wsbench/data/`
  (`<sha>.zip`, written to a temp name and `os.replace`d).

## 2. `src/wsbench/datapack.py`

State: `data/.unpacked.json` (git-ignored), `{unit: {"zip_sha256", "files": {path: sha256 as
unpacked}}}`, plus the sha256 of the concatenated sidecars it was written for, so the no-op
check is one small read and one hash of the sidecar files.

**`unpack(units=None, force=False)`** per unit whose sidecar differs from the stamp: get the
zip (cache, else download; its sha256 must equal the sidecar's, else a `DataError` naming the
unit and URL); then per file, three-way:
- working file absent, or its sha equals the STAMP's (unchanged since the last unpack):
  write the zip's bytes (temp name + `os.replace`, so readers never see a partial file);
- working file's sha equals the zip's: nothing;
- otherwise it is a local edit: **conflict**. Nothing of that unit is written; `unpack`
  raises `DataError` listing every conflicting path. `force=True` overwrites.
- a path in the stamp that the new sidecar no longer lists, and whose working file is
  unchanged since the stamp, is deleted (loaders glob these directories; a stale file changes
  results). An edited one is a conflict.
Then the stamp is rewritten for that unit. A pre-cutover checkout's first unpack has no
stamp: files whose sha equals the zip's are adopted into the stamp; differing ones are
conflicts.

**`pack(units=None)`**: refuses (a `DataError` saying "run `wsbench unpack` first") unless
the stamp shows EVERY unit unpacked from the current sidecars: packing a tree that is not
fully unpacked would read missing files as deletions. For each unit whose listed files'
working shas differ from the sidecar (a listed file missing from disk is an error naming it,
never a silent removal): build the zip into the cache, rewrite the sidecar, update the stamp.
`pack add=` / `rm=` edit the lists first. `pack` never touches the network.
- `pack status`: per unit, modified / missing listed files and unlisted files; exit 1 when
  any listed file differs from its sidecar (the "forgot to pack" check).
- `pack diff [ref=HEAD] [units=] [text=True]`: the files added / removed / changed between
  the sidecars at `ref` and the working tree; with `text=True`, a unified diff of each changed
  text file against the version in `ref`'s zip (fetched to the cache). This replaces `git
  diff adapted/<m>/...` for bank review.
- `publish`: uploads every zip named by a current sidecar that the release does not hold yet
  (`gh release upload`, creating the bucket release when missing; needs `gh` and push rights),
  then verifies each by an HTTP HEAD. A sidecar committed without its asset fails CI's unpack
  step (section 4), which is the guard against a forgotten publish.

**`ensure()`**: no-op when `data/INDEX.json` is absent (a wheel, a Modal container with only
`src/`, the skill's shadow checkouts) or `WSB_NO_UNPACK=1`, or the stamp is current;
otherwise `unpack()` under a lock file, with every file written atomically so correctness
never depends on the lock (flock on network volumes is unreliable). It raises `DataError`
(never `SystemExit`) and the CLI maps it to exit 2 with the message. In a read-only
checkout: when the plaintext is present and matches, continue without writing the stamp;
when it is absent, `DataError` saying so.
- Called once per process from `wsbench.registry` (import time, so every loader is covered)
  and explicitly from `tests/conftest.py` (session scope) and from the three Modal scripts
  that `add_local_dir("evals")` (`scripts/lucky_guess_modal.py`, the wsb-new-questions
  skill's `modal_gate.py` / `modal_script.py`) before they build their image. Scripts that
  read `REPO/"evals"` without importing `wsbench.registry` need `wsbench unpack` once; README
  says so.
- CLI: `wsbench unpack [units=] [force=True]`, `wsbench pack [units=] [add=] [rm=]`,
  `wsbench pack status`, `wsbench pack diff`, `wsbench pack publish`. `python -m
  wsbench.datapack` is not an entry point (one way in).

## 3. `.gitignore` (patterns verified in a scratch repo by the critic)

```
/evals/**
!/evals/**/
!/evals/**/*.md
/adapted/*
!/adapted/README.md
/data/.unpacked.json
```
The existing `adapted/*/...` work-product lines and the glm-5.3 negation are dead under
`/adapted/*` and are deleted.

## 4. CI and tests

- `.github/workflows/ci.yml`: `actions/cache` on `~/.cache/wsbench/data` keyed by
  `hashFiles('data/**')`; then `uv run wsbench unpack` (network; fails on a missing asset or
  a sha mismatch) and `uv run wsbench pack status` before `pytest`. Tests themselves stay
  offline: they read the unpacked tree.
- `tests/test_datapack.py` (temp trees, a local directory standing in for the release via a
  `file://`-style fetch seam; no network): round trip; the three-way rule (silent update of
  an unedited file, no-op, conflict, `force`); stale-file deletion; `pack` refusing a
  not-fully-unpacked tree; `add` / `rm`; a listed file missing; `status` exit codes; `diff`;
  a corrupt or wrong-sha zip; the stamp no-op; two processes unpacking at once leave a
  correct tree; `ensure()` with no `data/`, with `WSB_NO_UNPACK`, read-only; importing
  `wsbench` with pyzipper absent (blocked in `sys.modules`) still works; the tracked-files
  rule (`git ls-files evals adapted` = the Markdown keepers only; skipped outside a git
  checkout); the canary placement.
- **Three pre-existing tests whose guard would go silently blind once the plaintext is
  git-ignored are edited, exactly so, and listed in the PR:**
  `tests/test_r5_adapted_mode.py` (~l.61-65: "nothing under `evals/` changed" via `git status
  --porcelain evals/`) compares `datapack` file shas of `evals/` before and after instead;
  `tests/test_legacy_family_names.py` (~l.260-268) and `tests/test_adapt_refill.py`
  (~l.139-149) scan `git ls-files` ∪ the sidecars' paths. Any other red pre-existing test is
  a stop-and-report.
- The whole existing suite otherwise passes unedited.

## 5. The cutover (done by the main session, in this order)

1. On the implementation commit (plaintext still tracked): `wsbench pack add='evals/**'
   add='adapted/**'` restricted to tracked files minus the Markdown keepers and
   `.report.lock` (a one-off `scripts/datapack_cutover.py` drives it from `git ls-files` and
   is deleted in the same PR), producing sidecars and zips.
2. **One-time identity check:** wipe a scratch copy, unpack it from the zips, and compare
   `git hash-object` of every unpacked file with `git ls-tree -r <parent> evals adapted`
   blob ids: all 7,619 equal (7,626 minus the seven lock files), none extra. The command and
   its output go in the PR body.
3. `wsbench pack publish` (creates the `data-xx` releases, uploads 51 assets, ~130 MB).
4. `git rm --cached` the packed files, commit `data/` + `.gitignore`.
5. CI must pass from a clean checkout (it downloads the assets): the real end-to-end test.

## 6. Notice and canary

- `WORKSPACEBENCH CANARY GUID <uuid4>` in `data/README.md`, `data/INDEX.json`, `CANARY.txt`
  in every zip, README.md, NOTICE.md. NOT in any bank file (it would move every `bank_sha`).
  Stated limit: the unpacked banks and all pre-cutover history carry no canary, so it marks
  the release, it cannot by itself detect training on the items; a canary field in bank
  headers can join at each family's next version bump (not done here).
- README.md section "Do not train on this data" (above Quickstart), same request in
  NOTICE.md and `data/README.md`: evaluation data; please do not train on it or republish it
  unpacked; it ships in password-protected zips for that reason; the password; the canary.
  LICENSE stays MIT (a request, as nocot's is; a licence change is Camila's call).
- Docs: README §Quickstart and `README.md:154` (banks are unpacked beside `src/`),
  CLAUDE.md (a "Data packing" bullet + Workflow), AGENTS.md, `adapted/README.md`, the
  `adapt-bank` and `wsb-new-questions` skills: `wsbench pack` + `wsbench pack publish` before
  committing a bank change; review with `wsbench pack diff`; the adapt-bank push recipe
  becomes "copy the set's files into the clone, `pack add`, `pack`, `publish`"; never `git
  pull` the cutover into a tree with a running job (the pull deletes the then-untracked
  plaintext until the next unpack); a branch with bank edits merges main as: stash the
  edited files aside, merge, `wsbench unpack`, copy the edits back, `pack`.

## 7. Not covered (stated in the PR)

- Git history before the cutover, the tags `v1.0.0` / `v1.1.0` (GitHub serves their
  tarballs), other branches (`camila/judge-sweep`), open PR heads, forks and clones keep the
  plaintext: Camila chose no rewrite.
- `src/wsbench/adapt/data/*.json` (the agentic pools: source text of 716 of the 1,009
  adapted agentic items, shipped in the wheel), `tests/fixtures/`, `tests/golden/`,
  `examples/readouts/`, README example lines, `evals/multihop_mt/V1_AUDIT_2026-10-15.md`.
- The public viewer apps listed in `atlas/CB_EXPORT.md` serve items and readouts.
- A person, or a crawler that runs code, can unpack: this stops scraping, not access.

## Files

```
plans/0097-no-train-data.md
src/wsbench/datapack.py, src/wsbench/cli.py, src/wsbench/registry.py
data/README.md, data/INDEX.json, data/evals/*.json, data/adapted/*.json      new
evals/**, adapted/** (all but the Markdown keepers)                          out of the index
.gitignore, .github/workflows/ci.yml, pyproject.toml, uv.lock
README.md, NOTICE.md, CLAUDE.md, AGENTS.md, adapted/README.md, .claude/skills/*/SKILL.md
scripts/lucky_guess_modal.py, .claude/skills/wsb-new-questions/scripts/modal_{gate,script}.py
tests/test_datapack.py, tests/conftest.py, + the three sanctioned test edits
```
