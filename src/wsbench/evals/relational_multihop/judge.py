"""relational_multihop: two 11-way MCs (X outer, Y inner) per (item, layer) at the blank."""

import random
from collections import defaultdict

from wsbench import digest, reads
from wsbench.cache import Cache
from wsbench.family import render_bag
from wsbench.llm import Spend
from wsbench.mc import CANNOT, listing, seed_int, seeded_shuffle
from wsbench.mcjudge import (
    Call,
    Preflighter,
    base_config,
    base_counts,
    choice_of,
    item_scope,
    load_bank,
    quote_of,
    run_calls,
    with_readout_count,
)
from wsbench.readouts import Cell, LoadReport
from wsbench.registry import JudgeArgs
from wsbench.results import FamilyResult

from . import prompts, score
from .prompts import (
    BLANK_TOKEN,
    DIGEST_TASK_NOTE,
    JUDGE_SYSTEM,
    KIN_EXTRA,
    KINSHIP,
    MC_SCHEMA,
    N_MC_OPTIONS,
    N_PER_DOMAIN,
    PROF_EXTRA,
    PROMPT_VERSION,
    REL_SEED,
)

FAMILY = "relational_multihop"


def pools(items: list[dict]) -> tuple[list[str], list[str]]:
    """(professional, kinship) distractor pools, both **sorted**: the bank's relations + the
    near-miss extras (asserted disjoint)."""
    rels = {it["hop1"] for it in items} | {it["hop2"] for it in items}
    kin = sorted({r for r in rels if r in KINSHIP} | set(KIN_EXTRA))
    prof = sorted({r for r in rels if r not in KINSHIP} | set(PROF_EXTRA))
    if set(kin) & set(prof):
        raise ValueError(f"pools overlap: {sorted(set(kin) & set(prof))}")
    for name, pool in (("kinship", kin), ("professional", prof)):
        if len(pool) < N_PER_DOMAIN + 1:
            raise ValueError(f"{name} pool has {len(pool)} words < {N_PER_DOMAIN + 1}")
    return prof, kin


def build_mc(
    item_id: str, hop1: str, hop2: str, prof: list[str], kin: list[str]
) -> tuple[list[str], int, int]:
    """ONE pooled option list shared by both sub-questions: ``(shown incl. CANNOT, hop1 1-based
    position, hop2 1-based position)``. Kinship distractors are drawn first, then professional,
    from one rng; the pre-shuffle list is ``[hop1, hop2, *kin_dist, *prof_dist]``."""
    seed = f"{item_id}:pooled"
    rng = random.Random(seed_int(f"{REL_SEED}:dist:{seed}"))
    kin_dist = rng.sample([w for w in kin if w not in (hop1, hop2)], N_PER_DOMAIN)
    prof_dist = rng.sample([w for w in prof if w not in (hop1, hop2)], N_PER_DOMAIN)
    content = seeded_shuffle([hop1, hop2, *kin_dist, *prof_dist], f"{REL_SEED}:{seed}")
    assert len(set(content)) == len(content) == N_MC_OPTIONS - 1, (item_id, content)
    shown = [*content, CANNOT]
    return shown, shown.index(hop1) + 1, shown.index(hop2) + 1


def select_cells(cells: list[Cell]) -> list[Cell]:
    """Per (id, layer) the max-pos row is the blank; a row that carries a ``token`` must be the
    possessive ``'s`` (raises otherwise)."""
    groups: dict[tuple[str, int], list[Cell]] = defaultdict(list)
    for c in cells:
        groups[(c.id, c.layer)].append(c)
    out: list[Cell] = []
    for k in sorted(groups):
        last = max(groups[k], key=lambda c: c.pos)
        # a tokenizer that splits the possessive (Gemma: "'" + "s") reads its last piece
        if last.token is not None and not (last.token and BLANK_TOKEN.endswith(last.token)):
            raise ValueError(
                f"{last.id} L{last.layer}: last read position {last.pos} is {last.token!r}, not "
                f"the blank {BLANK_TOKEN!r} — these readouts were not captured at the "
                "relational read sites"
            )
        out.append(last)
    return out


def bundle_text(c: Cell) -> str:
    if c.samples is not None:
        txt = " | ".join(s for s in c.samples if s.strip())
    else:
        txt = render_bag(c.tokens or (), c.scores)
    return prompts.render_bundle(c.token, c.pos, txt)


def run(args: JudgeArgs) -> FamilyResult:
    bank = load_bank(FAMILY)
    scope = item_scope(bank, args)
    prof, kin = pools(bank)
    mcs = {it["id"]: build_mc(it["id"], it["hop1"], it["hop2"], prof, kin) for it in scope}
    cells, rep = reads.load(args, FAMILY, ids=[it["id"] for it in scope])
    selected = select_cells(cells)
    if digest.summary_mode(args):
        return _run_summary(args, bank, scope, mcs, cells, rep, selected)
    nonempty = [c for c in selected if not c.empty]
    spend = Spend()
    pre = Preflighter(args.dry_run)
    n_interp_missing = 0
    with Cache(args.out / "cells.jsonl") as cache:
        bundles = {c.key: bundle_text(c) for c in nonempty}
        if rep.kind == "tokens":
            # plan 0032 §2.3: no interpretation step; the raw ranked tokens are judged
            summ: dict[str, str | None] = dict(bundles)
            texts = {k: v for k, v in summ.items() if v is not None}
            n_interp_missing = sum(1 for v in summ.values() if v is None)
        else:
            texts = bundles
        calls: list[Call] = []
        for c in nonempty:
            if c.key not in texts:
                continue
            shown, xg, yg = mcs[c.id]
            block = listing(shown)
            for which, gold in (("X", xg), ("Y", yg)):
                calls.append(
                    Call(
                        f"{c.key}:{which}",
                        JUDGE_SYSTEM,
                        prompts.render_question(texts[c.key], which, block),
                        {
                            "cell": c.key,
                            "id": c.id,
                            "layer": c.layer,
                            "pos": c.pos,
                            "gold_pos": gold,
                        },
                    )
                )
        results = (
            run_calls(
                calls,
                schema=MC_SCHEMA,
                judge=args.judge,
                prompt_version=PROMPT_VERSION,
                cache=cache,
                spend=spend,
                concurrency=args.concurrency,
                rpm=args.rpm,
                dry_run=args.dry_run,
                preflight=pre.for_judge(args.judge),
            )
            if calls
            else {}
        )
    rows: list[dict] = []
    n_api_failed = 0
    for c in nonempty:
        if c.key not in texts:
            continue
        xr, yr = results.get(f"{c.key}:X"), results.get(f"{c.key}:Y")
        if xr is None or yr is None:
            n_api_failed += 1  # the pair is dropped; rerun to retry
            continue
        _shown, xg, yg = mcs[c.id]
        x_ok = choice_of(xr) == xg
        y_ok = choice_of(yr) == yg
        rows.append(
            {
                "key": c.key,
                "id": c.id,
                "layer": c.layer,
                "pos": c.pos,
                "x_choice": choice_of(xr),
                "x_gold_pos": xg,
                "x_ok": x_ok,
                "x_quote": quote_of(xr),
                "y_choice": choice_of(yr),
                "y_gold_pos": yg,
                "y_ok": y_ok,
                "y_quote": quote_of(yr),
                "pass": bool(x_ok and y_ok),
            }
        )
    counts = base_counts(
        n_expected=len(selected),
        n_unjudged=len(nonempty) - len(rows),
        n_empty=len(selected) - len(nonempty),
        skipped_rows=sum(rep.skipped.values()),
        spend=spend,
    )
    return with_readout_count(
        score.score(
            args,
            bank,
            scope,
            rows,
            counts=counts,
            config=base_config(args, PROMPT_VERSION),
            n_layers=len(rep.layers),
            n_api_failed=n_api_failed,
            n_interp_missing=n_interp_missing,
            empty_cells=[(c.id, c.layer) for c in selected if c.empty],
        ),
        scope,
        cells,
    )


def _run_summary(
    args: JudgeArgs,
    bank: list[dict],
    scope: list[dict],
    mcs: dict[str, tuple[list[str], int, int]],
    cells: list[Cell],
    rep: LoadReport,
    selected: list[Cell],
) -> FamilyResult:
    """Summary mode (plan 0020): every layer's blank cell of an item -> one digest -> the X and
    Y MCs on it (X∧Y binding is summary-level). An item with no readouts stays a rowless fail;
    an unsummarized item, or one whose X or Y call failed, is a fail counted in
    ``n_unjudged_cells`` (items); an empty one is a fail counted in ``n_empty_cells`` (items)."""
    pv = digest.prompt_version(PROMPT_VERSION, args)
    by_item: dict[str, list[Cell]] = defaultdict(list)
    for c in selected:
        by_item[c.id].append(c)
    ids = [it["id"] for it in scope if by_item.get(it["id"])]
    spend = Spend()
    pre = Preflighter(args.dry_run)
    with Cache(args.out / "cells.jsonl") as cache:
        col = digest.collapse(
            FAMILY,
            {i: by_item[i] for i in ids},
            args=args,
            cache=cache,
            spend=spend,
            preflight=pre.for_judge(digest.summarizer_judge(args)),
            task_note=DIGEST_TASK_NOTE,
        )
        source_kind = digest.as_prose(rep)
        note = digest.summary_note(args, source_kind)
        judged = [i for i in ids if i not in col.unsummarized and i not in col.empty]
        calls: list[Call] = []
        for i in judged:
            c = col.cells[i]
            shown, xg, yg = mcs[i]
            readout = prompts.render_bundle_summary(note, (c.samples or ("",))[0])
            for which, gold in (("X", xg), ("Y", yg)):
                calls.append(
                    Call(
                        f"{_summary_key(i)}:{which}",
                        JUDGE_SYSTEM,
                        prompts.render_question(readout, which, listing(shown)),
                        {"cell": _summary_key(i), "id": i, "layer": c.layer, "gold_pos": gold},
                    )
                )
        results = (
            run_calls(
                calls,
                schema=MC_SCHEMA,
                judge=args.judge,
                prompt_version=pv,
                cache=cache,
                spend=spend,
                concurrency=args.concurrency,
                rpm=args.rpm,
                dry_run=args.dry_run,
                preflight=pre.for_judge(args.judge),
            )
            if calls
            else {}
        )
    rows: list[dict] = []
    n_api_failed = 0
    for i in ids:
        c = col.cells[i]
        _shown, xg, yg = mcs[i]
        xr = yr = None
        if i in judged:
            xr, yr = results.get(f"{_summary_key(i)}:X"), results.get(f"{_summary_key(i)}:Y")
            if xr is None or yr is None:
                n_api_failed += 1
        x_ok = xr is not None and yr is not None and choice_of(xr) == xg
        y_ok = xr is not None and yr is not None and choice_of(yr) == yg
        rows.append(
            {
                "key": _summary_key(i),
                "id": i,
                "layer": c.layer,
                "pos": c.pos,
                "judged": xr is not None and yr is not None,
                "x_choice": choice_of(xr),
                "x_gold_pos": xg,
                "x_ok": x_ok,
                "x_quote": quote_of(xr),
                "y_choice": choice_of(yr),
                "y_gold_pos": yg,
                "y_ok": y_ok,
                "y_quote": quote_of(yr),
                "pass": bool(x_ok and y_ok),
                **col.row(i),
            }
        )
    counts = base_counts(
        n_expected=len(selected),
        n_unjudged=len(col.unsummarized) + n_api_failed,
        n_empty=len(col.empty),
        skipped_rows=sum(rep.skipped.values()),
        spend=spend,
    )
    return with_readout_count(
        score.score(
            args,
            bank,
            scope,
            rows,
            counts=counts,
            config=base_config(args, pv, **digest.config(args, source_kind)),
            n_layers=len(rep.layers),
            n_api_failed=n_api_failed,
            n_interp_missing=0,
            summary=col.extras(),
            pinned=digest.pinned(args),
        ),
        scope,
        cells,
    )


def _summary_key(item_id: str) -> str:
    return f"{item_id}|L{digest.SUMMARY_LAYER}"
