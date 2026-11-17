# HallucinationBench index (vendored)

The `hallucination` family left WorkspaceBench on 2026-10-30 (plan
[`0040`](../../plans/0040-hallucination-to-hb.md)). It was replaced by
[HallucinationBench](https://github.com/camila-blank/hallucination-bench) (HB). WSB reports
HB's index instead, from a frozen copy in this directory.

**What the index is.** The weighted share of a lens's claims that are hallucinated: claims the
model's own computation contradicts. Lower is better. It is one number per (method, layer).

**Version.** `hindex-v8`, HB plan 0029 (frozen with `hbench final`; the `hbench index` default
since its Amendment 4; plan 0080 moved WSB from `hindex-v6`). The family weights are:

| family | weight |
|---|---:|
| main (chat) | 9 |
| am_claims (agentic misalignment claims) | 3 |
| am_situation (agentic misalignment situation) | 3 |
| sibling (user-model ladder) | 1 |
| story | 1 |
| overrefusal | 1 |
| benign_roleplay | 2 |

The units differ by family. Main and story count all claims (claim map `claimmap-v2.9`, with
excused literal continuations decided by the verifier `cverify-v1`). am_claims counts candidate
claims; am_situation counts cells whose readouts misrepresent the scenario as a whole. Sibling
counts committed, probe-testable units. Overrefusal counts refusal claims whose declined subject
is not in the prompt. benign_roleplay counts harmless role-play prompts with a claimed jailbreak
recognition. Main runs on HB plan 0028's 3x bank. HB's `docs/results/0029_index_v8.md` gives the
details and the numbers.

**Excluded cells.** HB plan 0029 Amendment 4 excludes cells that stay failed after the retries:
both judge models refused, or an invalid answer. They are counted and reported; the vendored
`excluded` keeps the per-entry counts for the vendored methods. Overrefusal is the most affected
family (O-lens up to 7% of items per layer).

**One number per row (decision Q6).**
- `full` needs every family, including a sibling score.
- `matched` is every family but sibling.
- WSB shows `full` when the method has it. Otherwise it shows `matched`, labelled
  `matched (no sibling)`.
- J-lens has no sibling score at any layer, so it always shows `matched (no sibling)`.

**benign_roleplay is an index family from v7 on.** This supersedes plan 0057's "benign_roleplay
is not an index family" reading of Q6. The rest of Q6 is retained: one number per (method,
layer), and the block never enters the WSB macro.

**Joint unit.** v8 also judges all 11 layers of a read site in one call (HB layer `all`). The
vendored file keeps these rows apart, under `joint` (`full_joint` per method). `full_joint` is
the six-family macro without sibling (Σ 19, the `matched` families), one sample per layer; it is
not comparable to a per-layer `full` / `matched` (and the joint am_situation is "any of 11").
They are not per-layer rows, and `wsbench report` does not show them.

## Files

- `index.json`: the vendored index.
  - Fields: `{hb_repo, hb_sha, version, weights, families, notes, rows, joint, excluded}`.
  - Each row is `{method, layer, full?, matched?, families: {name: {v, ci, n, n_items}}}`, with
    values as fractions and 95% cluster-bootstrap CIs.
  - Only the methods in `methods.json` are kept. HB's dev-box `sources` paths, the
    per-reader `safety` meta, the per-row `full_v7` / `matched_v7` / `full_j6` macros and the
    per-family `health` are dropped (the copied v8 note still names them).
  - Written only by `scripts/vendor_hb_index.py SRC=<HB outputs/index/<judge>/index.json>
    HB_SHA=<sha>`, where SRC is HB's `hbench final` index. The script refuses an index whose
    `git_sha` is not `HB_SHA`, or whose version is not `hindex-v8`.
  - The file is frozen. A refresh is a new commit with a new `hb_sha`.
  - Current copy: built at HB `2563704` (main, HB PR #65) with `hbench final --manifest
    data/index/v8_manifest.json` (strict, exit 0), judges `claude-opus-5-5` (AM maps and
    situation on `claude-sonnet-5-5`).
- `methods.json`: `{methods: {WSB arm: HB method}, not_measured: {WSB arm: reason}}`.
  - An arm is mapped only when its checkpoint is the HB method's checkpoint.
  - `s3d-rl600` maps to `s3d-rl600`, and `jlens` maps to `jlens`.
  - `final-olens-rl600-x4` is not measured on v8. HB plan 0029 §2.4 keeps it out of the v8
    reader allowlists; it is on hindex-v6 / v7 only. It reports "not measured on
    HallucinationBench" with that reason, and is never aliased to `s3d-rl600`.
  - `mlnla-rl2-2000` (`mlnla:/ckpts/rl2/iter_002000`) is not verified identical to any HB NLA
    method (`mlnla-rl600`, `mlnla64-rl2k-all11`, `mlnla-ck2000-all11`). It reports "not
    measured on HallucinationBench" and is never aliased.

## How `wsbench report` shows it

`wsbench report dir=D [hb_method=<name>]` resolves the HB method in this order:
1. the `hb_method=` argument;
2. `run.json["hb_method"]` (from `wsbench run … hb_method=<name>`);
3. `methods.json[<basename of run.json readouts_root>]`.

The layers are the union of the results' `config.layers_judged`. The block is appended after
the macro table in `summary.md`:

```
HallucinationBench index (hindex-v8 @ 2563704, lower is better): s3d-rl600 full 9.7% [9.0, 10.4] at L44
```

A multi-layer run lists every judged layer, then the unweighted mean of the per-layer values of
one headline kind (no CI). Layers HB does not have (e.g. agentic's L63) are listed as not
measured. `json=True` adds the same data under `"hallucinationbench"`. The HB index never
enters the WSB macro. Code: `src/wsbench/hbindex.py`; tests: `tests/test_hbindex.py`.
