"""Multi-concept directed modulation: one multi-select judge call per in-sentence write cell;
an item passes when some cell names a dictated concept. Regions, capacity and false picks are
explained in the family README."""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from wsbench import digest
from wsbench.banks import load_bank
from wsbench.cache import Cache
from wsbench.family import (
    cell_text,
    fail,
    mean,
    pass_rate_result,
    quote_in,
    rate,
    require_cells,
    tri_state,
)
from wsbench.llm import Spend
from wsbench.mc import letter_index
from wsbench.mcjudge import Call, Preflighter, item_scope, run_calls, with_readout_count
from wsbench.readouts import Cell, LoadReport, load_readouts
from wsbench.registry import JudgeArgs, bank_path
from wsbench.results import FamilyResult
from wsbench.summarizer import SUMMARIZER_PROMPT_VERSION, aux_judge, render_bag, summarize

from .options import option_sets, partner_of
from .prompts import DIGEST_TASK_NOTE, PROMPT_VERSION, SCHEMA, SYSTEM, render_user
from .regions import Region, label_cells

NAME = "multi_concept_directed_modulation"
CHANCE_LABEL = (
    "no analytic floor (multi-select over a frozen candidate list); the measured floors are "
    "the lucky-guessing and prompt-only baselines, and the controls' pick rate"
)


def verdict(
    res: dict[str, Any] | None,
    options: list[str],
    gold: list[int],
    readout: str,
    gate: Sequence[str] | None = None,
) -> dict[str, Any]:
    """One judge answer. A selection counts only as a lone letter with a verbatim (folded) quote
    from the readout; ``own`` are the dictated concepts credited, ``false`` the other candidates
    the readout was said to name. ``kind``: ``hit`` / ``false_pick`` / ``none`` / ``invalid``
    (no ``picks`` list) / ``unavailable`` (no answer; unjudged). Summary mode passes ``gate``
    (``digest.gate_texts``): the quote must be inside ONE of those samples."""
    if res is None:
        return {"judged": False, "kind": "unavailable", "own": [], "false": []}
    picks = res.get("picks")
    if not isinstance(picks, list):
        return {"judged": True, "kind": "invalid", "own": [], "false": [], "n_invalid": 1}
    own: list[str] = []
    false: list[str] = []
    n_unverified = n_invalid = 0
    for p in picks:
        idx = letter_index(p.get("choice") if isinstance(p, dict) else None, options)
        if idx is None:
            n_invalid += 1
            continue
        quote = str(p.get("quote", ""))
        if not (
            quote_in(quote, readout) if gate is None else any(quote_in(quote, t) for t in gate)
        ):
            n_unverified += 1
            continue
        target = own if idx in gold else false
        if options[idx] not in target:
            target.append(options[idx])
    kind = "hit" if own else ("false_pick" if false else "none")
    return {
        "judged": True,
        "kind": kind,
        "own": own,
        "false": false,
        "n_unverified": n_unverified,
        "n_invalid": n_invalid,
        "picks": picks,
    }


def run(args: JudgeArgs) -> FamilyResult:
    _header, items = load_bank(bank_path(NAME))
    scope = item_scope(items, args)
    ids = [it["id"] for it in scope]
    by_id = {it["id"]: it for it in scope}
    options = option_sets(items)  # over the whole bank, never the scored subset
    by_name = {it["name"]: it for it in items}
    partner_concepts = {
        it["id"]: {
            str(c)
            for c in (by_name.get(partner_of(str(it["name"])) or "") or {}).get("concepts") or []
        }
        - {str(c) for c in it.get("concepts") or []}
        for it in items
    }
    cells, rep = load_readouts(args.readouts, ids=ids, layers=args.layers)
    layers = args.layers if args.layers is not None else rep.layers

    # the write window per item, from the rows' tokens (any layer), in forward order
    window: dict[str, dict[int, str]] = defaultdict(dict)
    untokened = sorted({c.id for c in cells if c.token is None})
    if untokened:
        fail(
            f"{NAME}: {len(untokened)} items have readout rows without a `token` (first: "
            f"{untokened[0]}); this family labels write-cell regions from the window's tokens, "
            "so every row must carry the token read at its `pos` (convert-read-json writes it)"
        )
    for c in cells:
        window[c.id].setdefault(c.pos, c.token or "")
    regions: dict[str, dict[int, Region]] = {}
    compliance: dict[str, Any] = {}
    for item_id, toks in window.items():
        forward = sorted(toks)  # offsets -12 .. -1 read forward
        labels, comp = label_cells([toks[p] for p in forward])
        regions[item_id] = dict(zip(forward, labels, strict=True))
        compliance[item_id] = comp
    judged_pos = {
        i: sorted(p for p, r in regions.get(i, {}).items() if r is Region.IN_SENTENCE) for i in ids
    }
    have = {(c.id, c.layer, c.pos): c for c in cells}
    expected = [(i, layer, p) for i in ids for layer in layers for p in judged_pos[i]]
    missing = [k for k in expected if k not in have]
    require_cells(NAME, missing, len(expected), args)
    judged_cells = [have[k] for k in expected if k in have]
    if digest.summary_mode(args):
        return _run_summary(
            args,
            scope=scope,
            options=options,
            partner_concepts=partner_concepts,
            regions=regions,
            compliance=compliance,
            cells=cells,
            rep=rep,
            layers=layers,
            judged_cells=judged_cells,
            n_expected=len(expected),
            missing=missing,
        )

    spend = Spend()
    pre = Preflighter(args.dry_run)
    texts: dict[str, str] = {}
    empty: set[tuple[str, int, int]] = set()
    with Cache(args.out / "cells.jsonl") as cache:
        if rep.kind == "tokens":
            bundles = {
                c.key: render_bag(c.tokens or (), c.scores) for c in judged_cells if c.tokens
            }
            sjudge = aux_judge(args.judge, args.aux_models, "summarizer")
            summ = summarize(
                bundles,
                judge=sjudge,
                cache=cache,
                spend=spend,
                concurrency=args.concurrency,
                rpm=args.rpm,
                dry_run=args.dry_run,
                preflight=pre.for_judge(sjudge),
            )
            texts = {k: v for k, v in summ.items() if v is not None}
            empty = {(c.id, c.layer, c.pos) for c in judged_cells if not c.tokens}
        else:
            for c in judged_cells:
                text, _plain = cell_text(c)
                if text.strip():
                    texts[c.key] = text
                else:
                    empty.add((c.id, c.layer, c.pos))
        calls: list[Call] = []
        for c in judged_cells:
            readout = texts.get(c.key)
            if readout is None:
                continue
            opts, _gold = options[c.id]
            calls.append(
                Call(
                    key=f"{c.id}|L{c.layer:03d}|p{c.pos}",
                    system=SYSTEM,
                    user=render_user(readout, opts),
                    meta={"item": c.id, "layer": c.layer, "pos": c.pos},
                )
            )
        results = run_calls(
            calls,
            schema=SCHEMA,
            judge=args.judge,
            prompt_version=PROMPT_VERSION,
            cache=cache,
            spend=spend,
            concurrency=args.concurrency,
            rpm=args.rpm,
            dry_run=args.dry_run,
            preflight=pre.for_judge(args.judge),
            temperature=0.0,
        )

    verdicts: list[dict[str, Any]] = []
    for call in calls:
        c_id, layer, pos = call.meta["item"], call.meta["layer"], call.meta["pos"]
        opts, gold = options[c_id]
        v = verdict(results.get(call.key), opts, gold, texts[f"{c_id}__L{layer:03d}__p{pos}"])
        verdicts.append({"item": c_id, "layer": layer, "pos": pos, **v})
    unsummarized = {
        (c.id, c.layer, c.pos)
        for c in judged_cells
        if rep.kind == "tokens" and c.tokens and c.key not in texts
    }
    rows = [
        _item_row(
            by_id[i],
            options[i],
            partner_concepts[i],
            regions.get(i, {}),
            compliance.get(i),
            verdicts,
            missing,
            unsummarized,
            empty,
            layers,
        )
        for i in ids
    ]
    decided = [r for r in rows if r["pass"] is not None]
    unjudged_cells = {
        (v["item"], v["layer"], v["pos"]) for v in verdicts if not v["judged"]
    } | unsummarized
    kinds: dict[str, int] = defaultdict(int)
    for v in verdicts:
        kinds[v["kind"]] += 1
    judged = [v for v in verdicts if v["judged"]]
    controls = {r["id"] for r in rows if r["reason"] == "control"}
    result = pass_rate_result(
        name=NAME,
        args=args,
        prompt_version=PROMPT_VERSION,
        rows=rows,
        cells=cells,
        rep=rep,
        # a cell is one in-sentence (item, layer, write position) readout; one call per cell
        n_expected=len(expected),
        n_missing=len(missing),
        n_unjudged=len(unjudged_cells),
        n_empty=len(empty),
        spend=spend,
        chance_label=CHANCE_LABEL,
        config_extra={
            "layers_judged": layers,
            "summarizer": SUMMARIZER_PROMPT_VERSION if rep.kind == "tokens" else None,
        },
        extras={
            "n_calls": len(calls),
            "n_controls": len(controls),
            "n_off_task": sum(1 for r in rows if r["reason"] == "off_task"),
            "kinds": dict(kinds),
            "capacity": {
                k: mean(r["capacity"][k] for r in decided if r["capacity"])
                for k in (
                    "per_activation_max",
                    "per_cell_union",
                    "per_item_union",
                    "per_activation_mean",
                )
            },
            "false_pick_rate": rate(bool(v["false"]) for v in judged),
            "control_pick_rate": rate(
                bool(v["own"] or v["false"]) for v in judged if v["item"] in controls
            ),
            "partner_confusion_rate": rate(
                r["partner_picked"] for r in decided if r["partner_picked"] is not None
            ),
            "per_stratum": {
                s: rate(bool(r["pass"]) for r in decided if r["stratum"] == s)
                for s in sorted({r["stratum"] for r in decided})
            },
        },
    )
    return with_readout_count(result, scope, cells)


def _run_summary(
    args: JudgeArgs,
    *,
    scope: list[dict[str, Any]],
    options: dict[str, tuple[list[str], list[int]]],
    partner_concepts: dict[str, set[str]],
    regions: dict[str, dict[int, Region]],
    compliance: dict[str, Any],
    cells: list[Cell],
    rep: LoadReport,
    layers: list[int],
    judged_cells: list[Cell],
    n_expected: int,
    missing: list[tuple[str, int, int]],
) -> FamilyResult:
    """Summary mode (plan 0018): an item's in-sentence cells at every layer -> one digest ->
    ONE multi-select call per item, controls included (``control_pick_rate`` stays paired); the
    quote gate on the digest's evidence samples. Items with no in-sentence cell keep their
    native reason; a missing cell leaves a non-pass undecided; an unsummarized item is
    undecided and unjudged; ``capacity`` is omitted."""
    ids = [it["id"] for it in scope]
    by_id = {it["id"]: it for it in scope}
    by_item: dict[str, list[Cell]] = defaultdict(list)
    for c in judged_cells:
        by_item[c.id].append(c)
    spend = Spend()
    pre = Preflighter(args.dry_run)
    pv = digest.prompt_version(PROMPT_VERSION, args)
    with Cache(args.out / "cells.jsonl") as cache:
        col = digest.collapse(
            NAME,
            {i: by_item[i] for i in ids if by_item.get(i)},
            args=args,
            cache=cache,
            spend=spend,
            preflight=pre.for_judge(digest.summarizer_judge(args)),
            task_note=DIGEST_TASK_NOTE,
        )
        source_kind = digest.as_prose(rep)
        note = digest.summary_note(args, source_kind)
        calls = [
            Call(
                key=f"{i}|L{c.layer:03d}|p{c.pos}",
                system=SYSTEM,
                user=render_user((c.samples or ("",))[0], options[i][0], summary_note=note),
                meta={"item": i, "layer": c.layer, "pos": c.pos},
            )
            for i, c in col.cells.items()
            if i not in col.unsummarized and i not in col.empty
        ]
        results = run_calls(
            calls,
            schema=SCHEMA,
            judge=args.judge,
            prompt_version=pv,
            cache=cache,
            spend=spend,
            concurrency=args.concurrency,
            rpm=args.rpm,
            dry_run=args.dry_run,
            preflight=pre.for_judge(args.judge),
            temperature=0.0,
        )
    verdicts: list[dict[str, Any]] = []
    for call in calls:
        c_id = call.meta["item"]
        opts, gold = options[c_id]
        cell = col.cells[c_id]
        v = verdict(
            results.get(call.key),
            opts,
            gold,
            (cell.samples or ("",))[0],
            gate=digest.gate_texts(cell, col),
        )
        verdicts.append({"item": c_id, "layer": cell.layer, "pos": cell.pos, **v})
    synth = {i: (i, c.layer, c.pos) for i, c in col.cells.items()}
    unsummarized = {synth[i] for i in col.unsummarized}
    empty = {synth[i] for i in col.empty}
    rows = []
    for i in ids:
        row = _item_row(
            by_id[i],
            options[i],
            partner_concepts[i],
            regions.get(i, {}),
            compliance.get(i),
            verdicts,
            missing,
            unsummarized,
            empty,
            layers,
        )
        row["capacity"] = None  # per-activation capacity is meaningless on one digest
        if i in col.cells:
            row.update(col.row(i))
        rows.append(row)
    decided = [r for r in rows if r["pass"] is not None]
    n_unjudged = sum(1 for v in verdicts if not v["judged"]) + len(unsummarized)
    kinds: dict[str, int] = defaultdict(int)
    for v in verdicts:
        kinds[v["kind"]] += 1
    judged = [v for v in verdicts if v["judged"]]
    controls = {r["id"] for r in rows if r["reason"] == "control"}
    result = pass_rate_result(
        name=NAME,
        args=args,
        prompt_version=pv,
        rows=rows,
        cells=cells,
        rep=rep,
        # cells as native (in-sentence (item, layer, write position) readouts); unjudged /
        # empty count items (one digest, one call each)
        n_expected=n_expected,
        n_missing=len(missing),
        n_unjudged=n_unjudged,
        n_empty=len(empty),
        spend=spend,
        chance_label=CHANCE_LABEL,
        config_extra={"layers_judged": layers, **digest.config(args, source_kind)},
        extras={
            "n_calls": len(calls),
            "n_controls": len(controls),
            "n_off_task": sum(1 for r in rows if r["reason"] == "off_task"),
            "kinds": dict(kinds),
            "false_pick_rate": rate(bool(v["false"]) for v in judged),
            "control_pick_rate": rate(
                bool(v["own"] or v["false"]) for v in judged if v["item"] in controls
            ),
            "partner_confusion_rate": rate(
                r["partner_picked"] for r in decided if r["partner_picked"] is not None
            ),
            "per_stratum": {
                s: rate(bool(r["pass"]) for r in decided if r["stratum"] == s)
                for s in sorted({r["stratum"] for r in decided})
            },
            "summary": col.extras(),
        },
        n_items=len(ids),
        pinned=digest.pinned(args),
    )
    return with_readout_count(result, scope, cells)


def _item_row(
    item: dict[str, Any],
    opts: tuple[list[str], list[int]],
    partner_concepts: set[str],
    regions: dict[int, Region],
    comp: Any,
    verdicts: list[dict[str, Any]],
    missing: list[tuple[str, int, int]],
    unsummarized: set[tuple[str, int, int]],
    empty: set[tuple[str, int, int]],
    layers: list[int],
) -> dict[str, Any]:
    """``pass`` = some in-sentence (cell, layer) readout names a dictated concept; None with a
    ``reason`` for controls (nothing dictated), off-task items (no in-sentence cell), items
    without any readout row, and items with no hit whose cells are unjudged or missing."""
    item_id = item["id"]
    concepts = [str(c) for c in item.get("concepts") or []]
    partner = partner_of(str(item["name"]))
    mine = [v for v in verdicts if v["item"] == item_id]
    in_sentence = sorted(p for p, r in regions.items() if r is Region.IN_SENTENCE)
    region_counts = {r.value: sum(1 for x in regions.values() if x is r) for r in Region}
    reason: str | None = None
    if not concepts:
        reason = "control"
    elif not regions:
        reason = "no_readouts"
    elif comp is not None and not comp.complied:
        reason = "off_task"
    elif not in_sentence:
        reason = "no_in_sentence_cell"
    hits = [v for v in mine if v["own"]]
    incomplete = (
        any(not v["judged"] for v in mine)
        or any(i == item_id for i, _l, _p in unsummarized)
        or any(i == item_id for i, _l, _p in missing)
    )
    passed = None if reason is not None else tri_state(bool(hits), incomplete)
    if reason is None and passed is None:
        reason = "unjudged"
    # capacity over judged in-sentence activations (empty cells decode nothing)
    decoded = [len(v["own"]) for v in mine if v["judged"]] + [
        0 for i, _l, _p in empty if i == item_id
    ]
    per_cell: dict[int, set[str]] = defaultdict(set)
    for v in mine:
        per_cell[v["pos"]].update(v["own"])
    capacity = (
        {
            "per_activation_max": max(decoded),
            "per_cell_union": max((len(s) for s in per_cell.values()), default=0),
            "per_item_union": len({c for v in mine for c in v["own"]}),
            "per_activation_mean": sum(decoded) / len(decoded),
            "n_activations": len(decoded),
        }
        if decoded and passed is not None
        else None
    )
    partner_picked = (
        any(f in partner_concepts for v in mine for f in v["false"])
        if partner_concepts and any(v["judged"] for v in mine)
        else None
    )
    return {
        "id": item_id,
        "stratum": item.get("stratum"),
        "concepts": concepts,
        "partner": partner,
        "pass": passed,
        "reason": reason,
        "complied": None if comp is None else comp.complied,
        "matched_run": None if comp is None else comp.matched,
        "regions": region_counts,
        "in_sentence_cells": in_sentence,
        "layers": layers,
        "capacity": capacity,
        "concepts_surfaced": sorted({c for v in mine for c in v["own"]}),
        "false_picks": sorted({c for v in mine for c in v["false"]}),
        "partner_picked": partner_picked,
        "cells": [
            {k: v for k, v in x.items() if k != "item"}
            for x in sorted(mine, key=lambda x: (x["layer"], x["pos"]))
        ],
    }
