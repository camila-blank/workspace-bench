"""Multi-token basic families. The factory (``mt_family``, plan 0032 §2.16) takes one ``scorer``:
``regex`` — the regex contract in ``wsbench.multitoken.regex`` (a layer passes when every
required unit's form is found in a sample at that layer, an item at any layer; no call; the
``opts=judge=mc`` forced-choice judge stays a diagnostic, never pinned); ``mc`` — the forced-choice
judge as the pinned instrument; ``mc+regex`` — its semantics land with the multilingual family PR.
Every read position of a layer is pooled (the one-position refusal is gone); token readouts are
matched as raw tokens (the J-lens interpretation step is deleted, plan 0032 §2.3); under summary
mode (the default) every method goes through ``digest.collapse`` only."""

import dataclasses
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from wsbench import digest, mc, reads
from wsbench.banks import load_bank, native_langs
from wsbench.cache import Cache
from wsbench.family import (
    cell_text,
    fail,
    pass_rate_result,
    quote_in,
    rate,
    render_bag,
    require_cells,
    tri_state,
)
from wsbench.judge_config import JudgeConfig, ResolvedJudge
from wsbench.llm import Spend
from wsbench.mc import fold, letter_index  # fold is part of this module's public surface
from wsbench.mcjudge import (
    Call,
    Preflighter,
    item_scope,
    layer_pick_columns,
    run_calls,
    with_readout_count,
)
from wsbench.multitoken import regex
from wsbench.multitoken.options import judged_roles, option_sets
from wsbench.multitoken.prompts import (
    DIGEST_TASK_NOTES,
    LETTERS,
    PROMPT_VERSION,
    SCHEMA,
    SYSTEM,
    render_user,
    render_user_summary,
)
from wsbench.multitoken.regex import SCORER_VERSION
from wsbench.readouts import Cell
from wsbench.registry import EvalSpec, JudgeArgs, bank_path
from wsbench.results import FamilyResult
from wsbench.split import layer_protocols

__all__ = [
    "MC_PROFILES",
    "MCProfile",
    "fold",
    "letter_index",
    "mt_family",
    "name_credit",
    "name_gate",
    "run_family",
    "run_mc",
    "run_regex",
    "verdict",
]

GROUP = "basic_mt"
SCORER = "regex"
CHANCE_LABEL = (
    "no analytic floor for an any-layer conjunction of exact-form matches; the measured floor is "
    "the prompt-only baseline (the MC lucky-guessing floor does not apply to the regex scorer)"
)
MC_CHANCE_LABEL = (
    "no analytic floor for an any-layer conjunction (per call 1/6 per unit); the measured floors "
    "are the lucky-guessing and prompt-only baselines"
)
REGEX_JUDGE = ResolvedJudge(model=SCORER, reasoning=None, pinned=True, source="family")


SCORERS = ("regex", "mc", "mc+regex")


@dataclass(frozen=True)
class MCProfile:
    """A ``scorer="mc"`` family's own forced-choice judge (plan 0041; first user multihop_mt_v2):
    its SYSTEM prompt, its unit question (no role label), its cannot-tell line, and the name gate
    over the bank's ``name_forms``. ``regex_version`` stamps the regex contract when it runs as the
    diagnostic (``extras.regex``) or, unpinned, as ``opts=judge=regex``."""

    system: str
    question: str
    cannot: str
    regex_version: str = SCORER_VERSION


# family -> its judge; registered by the family package beside its SPEC (a family without an entry
# keeps the shared diagnostic judge of ``prompts.SYSTEM``)
MC_PROFILES: dict[str, MCProfile] = {}
CREDIT_KINDS = ("exact", "surname", "fuzzy", "name_miss", "none")


def _quote_candidates(quote: str) -> list[str]:
    """The texts a credited quote may name the gold in (each ``digest.fold_quote``-folded): each
    bullet of the quote on its own (split at the bullet mark, so a name is never assembled across
    two bullets), and for a token quote its " | " parts, plus the parts space-joined and
    concatenated in quote order (decision Q4: tokens of ONE list may spell one name; ``quote_ok``
    has already proved they are tokens of one list)."""
    out: list[str] = []
    for part in quote.split(mc.BULLET_MARK):
        if not part.strip():
            continue
        out.append(part)
        pieces = [x for x in part.split("|") if x.strip()]
        if len(pieces) >= 2:
            out += pieces
            out.append(" ".join(x.strip() for x in pieces))
            out.append("".join(x.strip() for x in pieces))
    return [digest.fold_quote(x) for x in out if digest.fold_quote(x)]


def name_credit(
    quote: str,
    *,
    full_forms: Sequence[str],
    name_forms: Sequence[str],
    fuzzy: bool = False,
    avoid: Sequence[str] = (),
) -> str:
    """How a verified quote names the gold: ``exact`` (one of its full names, the frozen bank's
    forms), ``surname`` (only a further ``name_forms`` entry: a gate alias, surname or distinctive
    name), ``fuzzy`` (only a misspelling ``regex.fuzzy_form_in`` accepts, never one also near an
    ``avoid`` option) or ``none``."""
    cands = _quote_candidates(quote)
    if any(mc.form_in(c, full_forms) for c in cands):
        return "exact"
    if any(mc.form_in(c, name_forms) for c in cands):
        return "surname"
    if fuzzy and any(regex.fuzzy_form_in(c, list(name_forms), avoid=avoid) for c in cands):
        return "fuzzy"
    return "none"


def name_gate(
    kind: str,
    quote: str,
    gold_forms: Sequence[str],
    lines: Sequence[digest.Line],
    *,
    fuzzy: bool = False,
    avoid: Sequence[str] = (),
) -> str:
    """The deterministic name gate ON TOP of ``digest.quote_ok`` (never replacing it; the same
    signature and kinds as basic_readout_mt's, plan 0037 item 8, plus ``fuzzy`` / ``avoid``): a
    gold pick (``kind == "correct"``) is ``unverified`` unless the quote verifies, ``name_miss``
    unless it names the gold by one of ``gold_forms`` (``mc.form_in`` over
    :func:`_quote_candidates`, then ``regex.fuzzy_form_in`` when ``fuzzy``), else ``correct``.
    Every other kind is returned unchanged."""
    if kind != "correct":
        return kind
    if not digest.quote_ok(quote, lines):
        return "unverified"
    credit = name_credit(
        quote, full_forms=gold_forms, name_forms=gold_forms, fuzzy=fuzzy, avoid=avoid
    )
    return "correct" if credit != "none" else "name_miss"


def _regex_pv(name: str) -> str:
    """The regex contract's stamp: an ``mc`` family's own regex version, else the spec's."""
    profile = MC_PROFILES.get(name)
    return profile.regex_version if profile is not None else _spec_pv(name, SCORER_VERSION)


def _spec_task_note(name: str) -> str:
    from wsbench import registry

    spec = registry.FAMILIES.get(name)
    return spec.task_note if spec is not None else DIGEST_TASK_NOTES.get(name, "")


def _spec_pv(name: str, default: str) -> str:
    """The family's registered prompt version (its scorer's own, plus any instrument tag)."""
    from wsbench import registry

    spec = registry.FAMILIES.get(name)
    return spec.judge.prompt_version if spec is not None else default


def mt_family(
    name: str,
    title: str,
    *,
    calls_per_arm: str,
    superseded_by: str = "",
    scorer: Literal["regex", "mc", "mc+regex"] = "regex",
    bank: Path | None = None,
    prompt_version: str | None = None,
    instrument_tag: str = "",
    min_full_positions: int = 2,
    full_contiguous: bool = True,
    task_note: str = "",
    item_task_notes: Callable[[], Mapping[str, str]] | None = None,
    flat_types: tuple[str, ...] = (),
    group: str = GROUP,
) -> EvalSpec:
    """One multi-token family (plan 0032 §2.16). ``scorer``: ``regex`` (``EvalSpec.scorer =
    "regex"``, no call; ``opts=judge=mc`` is a forced-unpinned diagnostic), ``mc`` / ``mc+regex``
    (``EvalSpec.scorer = ""``: an LLM judge, preflighted, the result pinned). ``bank`` defaults
    to ``evals/<name>/items.json``; ``prompt_version`` to the scorer's own; ``instrument_tag`` is
    appended once (``f"{pv}+{tag}"``)."""
    if scorer not in SCORERS:
        raise ValueError(f"{name}: scorer={scorer!r}; expected one of {SCORERS}")
    pv = prompt_version or (SCORER_VERSION if scorer == "regex" else PROMPT_VERSION)
    if instrument_tag and not pv.endswith("+" + instrument_tag):
        pv = f"{pv}+{instrument_tag}"
    calls = f"0 (regex); {calls_per_arm} with opts=judge=mc" if scorer == "regex" else calls_per_arm
    return EvalSpec(
        name=name,
        title=title,
        group=group,
        bank=bank or Path("evals") / name / "items.json",
        # regex: the instrument is the contract; the JudgeConfig model is what opts=judge=mc uses
        judge=JudgeConfig(prompt_version=pv),
        metric="pass_rate",
        higher_is_better=True,
        run=lambda args: run_family(args, name=name, scorer=scorer),
        calls_per_arm=calls,
        sources="",
        scorer=SCORER if scorer == "regex" else "",
        superseded_by=superseded_by,
        min_full_positions=min_full_positions,
        full_contiguous=full_contiguous,
        task_note=task_note or DIGEST_TASK_NOTES.get(name, ""),
        item_task_notes=item_task_notes,
        flat_types=flat_types,
    )


def run_family(args: JudgeArgs, *, name: str, scorer: str = SCORER) -> FamilyResult:
    """The scorer of record, or (regex families) ``opts=judge=mc`` as an unpinned diagnostic;
    an ``mc`` family's ``opts=judge=regex`` scores the regex contract, forced unpinned."""
    if scorer == "mc":
        which = args.extra.get("judge", "mc")
        if which == SCORER:
            res = run_regex(args, name=name)
            return dataclasses.replace(res, pinned_instrument=False, complete=False)
        if which != "mc":
            fail(f"{name}: opts=judge must be 'mc' (default) or 'regex', not {which!r}")
        return run_mc(args, name=name, pinned=True)
    if scorer == "mc+regex":
        fail(f"{name}: scorer mc+regex: its semantics land with the multilingual family PR")
    which = args.extra.get("judge", SCORER)
    if which == SCORER:
        return run_regex(args, name=name)
    if which == "mc":
        return run_mc(args, name=name)
    fail(f"{name}: opts=judge must be 'regex' (default) or 'mc', not {which!r}")


# ---------------------------------------------------------------------------------------------
# the regex contract (scorer of record)
# ---------------------------------------------------------------------------------------------
def _cell_samples(cell: Cell) -> list[str]:
    """One sample per prose sample, or one per top-k token string (as written by the producer);
    scaffolding stripped; blanks dropped. Samples are never joined, so a form found in a single
    token hits, and nothing is assembled across them."""
    raw = cell.tokens if cell.tokens is not None else (cell.samples or ())
    out = [regex.extract_phrase(str(s)) for s in raw]
    return [s for s in out if s]


def _layer_samples(cs: list[Cell]) -> list[str]:
    """A layer's samples pooled over its read positions (plan 0032 §2.16), position order."""
    return [x for c in sorted(cs, key=lambda c: c.pos) for x in _cell_samples(c)]


def _previous_mc(out: Path) -> dict[str, Any] | None:
    """The MC judge's number when ``out/results.json`` still holds one (the regime before this
    scorer, or an ``opts=judge=mc`` run) — carried into ``extras["mc"]`` so the two instruments
    sit side by side without a judge call."""
    path = out / "results.json"
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cfg = d.get("config") or {}
    if not str(cfg.get("prompt_version", "")).startswith("mc-"):
        return None
    return {
        "value": (d.get("numbers") or {}).get("value"),
        "prompt_version": cfg.get("prompt_version"),
        "judge_model": cfg.get("judge_model"),
        "n_items_decided": (d.get("numbers") or {}).get("extras", {}).get("n_items_decided"),
    }


def run_regex(args: JudgeArgs, *, name: str) -> FamilyResult:
    header, items = load_bank(bank_path(name))
    contract = regex.contract_for(header, str(header.get("family", name)))
    scope = item_scope(items, args)
    ids = [it["id"] for it in scope]
    try:
        units_of = {it["id"]: regex.scored_units(it, contract) for it in scope}
    except ValueError as e:
        fail(f"{name}: bank violates its own contract: {e}")
    cells, rep = reads.load(args, name, ids=ids)
    layers = args.layers if args.layers is not None else rep.layers
    positions: dict[tuple[str, int], list[Cell]] = defaultdict(list)
    for c in cells:
        positions[(c.id, c.layer)].append(c)
    missing = [(i, layer) for i in ids for layer in layers if (i, layer) not in positions]
    require_cells(name, missing, len(ids) * len(layers), args)
    if digest.summary_mode(args):
        return _run_regex_summary(
            args,
            name=name,
            contract=contract,
            scope=scope,
            ids=ids,
            units_of=units_of,
            cells=cells,
            rep=rep,
            positions=positions,
            layers=layers,
            missing=missing,
        )
    previous_mc = _previous_mc(args.out)
    spend = Spend()
    if args.dry_run:
        _print_dry_run(name, contract, scope, units_of, positions, layers, missing)

    empty: set[tuple[str, int]] = set()
    hits_by: dict[str, dict[int, dict[str, list[str]]]] = defaultdict(dict)
    for (item_id, layer), cs in sorted(positions.items()):
        samples = _layer_samples(cs)
        if not samples:
            empty.add((item_id, layer))
        if args.dry_run:
            hits_by[item_id][layer] = {}
            continue
        hits_by[item_id][layer] = regex.layer_unit_hits(samples, units_of[item_id], contract)

    rows: list[dict[str, Any]] = []
    incomplete_of: dict[str, bool] = {}
    for item_id in ids:
        units = units_of[item_id]
        res = regex.item_result(hits_by.get(item_id, {}), units)
        item_missing = [layer for i, layer in missing if i == item_id]
        incomplete = bool(item_missing) or not hits_by.get(item_id) or args.dry_run
        row = {
            "id": item_id,
            "roles": [u.role for u in units if u.required],
            "optional_roles": [u.role for u in units if not u.required],
            "pass": tri_state(res["pass"], incomplete),
            "earliest_layer": res["earliest_layer"],
            "passing_layers": res["passing_layers"],
            "unit_hit": res["unit_hit"],
            "unit_langs": res["unit_langs"],
            "first_lang": res["first_lang"],
            "any_hit": res["any_hit"],
            "missing_layers": item_missing,
            "layers": {
                str(layer): {"hits": h, "empty": (item_id, layer) in empty}
                for layer, h in sorted(hits_by.get(item_id, {}).items())
            },
        }
        rows.append(row)
        incomplete_of[item_id] = incomplete
    decided = [r for r in rows if r["pass"] is not None]
    roles = sorted({r for row in rows for r in (*row["roles"], *row["optional_roles"])})
    first_lang: dict[str, Counter[str]] = defaultdict(Counter)
    for row in decided:
        for role, lang in row["first_lang"].items():
            if lang is not None:
                first_lang[role][lang] += 1
    judge_used = REGEX_JUDGE  # the contract alone (token readouts are matched as raw tokens)
    version = _regex_pv(name)
    result = pass_rate_result(
        name=name,
        args=dataclasses.replace(args, judge=judge_used),
        prompt_version=version,
        rows=rows,
        cells=cells,
        rep=rep,
        n_expected=len(ids) * len(layers),
        n_missing=len(missing),
        n_unjudged=0,
        n_empty=len(empty),
        spend=spend,
        chance_label=CHANCE_LABEL,
        config_extra={
            "layers_judged": layers,
            "scorer": SCORER,
            "scorer_version": version,
            "contract": regex.contract_json(contract),
        },
        extras={
            "n_calls": spend.calls,
            "any_hit_rate": rate(row["any_hit"] for row in decided),
            "unit_any_layer": {
                r: rate(row["unit_hit"][r] for row in decided if r in row["unit_hit"])
                for r in roles
            },
            "language_of_readout": {r: dict(c) for r, c in sorted(first_lang.items())},
            **({"mc": previous_mc} if previous_mc else {}),
            # plan 0012 §5: report-only layer protocols (unsummarized layers are undecided)
            **(
                {
                    "layer_protocols": layer_protocols(
                        {
                            r["id"]: {
                                int(L): int(L) in set(r["passing_layers"]) for L in r["layers"]
                            }
                            for r in rows
                        },
                        family=name,
                        layers=layers,
                    )
                }
                if not args.dry_run
                else {}
            ),
        },
    )
    return with_readout_count(result, scope, cells)


def _collapse(
    name: str,
    args: JudgeArgs,
    ids: list[str],
    positions: dict[tuple[str, int], list[Cell]],
    *,
    cache: Cache,
    spend: Spend,
    pre: Preflighter,
) -> digest.Collapse:
    """Summary mode: every item's cells at every read layer -> one digest. Items with no cell
    are not passed (they keep the native missing path: undecided)."""
    by_item: dict[str, list[Cell]] = defaultdict(list)
    for (item_id, _layer), cs in sorted(positions.items()):
        by_item[item_id].extend(cs)
    passed = {i: by_item[i] for i in ids if by_item.get(i)}
    return digest.collapse(
        name,
        passed,
        args=args,
        cache=cache,
        spend=spend,
        preflight=pre.for_judge(digest.summarizer_judge(args, name)),
        task_note=_spec_task_note(name),
        expect_all=[c for cs in passed.values() for c in cs],
    )


def _run_regex_summary(
    args: JudgeArgs,
    *,
    name: str,
    contract: regex.BankContract,
    scope: list[dict[str, Any]],
    ids: list[str],
    units_of: dict[str, list[regex.ScoredUnit]],
    cells: list[Cell],
    rep: Any,
    positions: dict[tuple[str, int], list[Cell]],
    layers: list[int],
    missing: list[tuple[str, int]],
) -> FamilyResult:
    """Summary mode (plan 0020): the contract runs once per item on its digest's evidence
    samples (``gate_texts(joined=True)``) as the samples of one layer, so the same-layer
    conjunction becomes "every required unit among the evidence samples". The instrument is
    the digest. No cell: undecided (native); a missing layer and no pass, or an unsummarized
    item: undecided; an empty item (blank cells or a zero-item digest): a fail."""
    spend = Spend()
    with Cache(args.out / "cells.jsonl") as cache:
        col = _collapse(
            name, args, ids, positions, cache=cache, spend=spend, pre=Preflighter(args.dry_run)
        )
    source_kind = digest.as_prose(rep)
    if args.dry_run:
        _print_dry_run(name, contract, scope, units_of, positions, layers, missing)
    partial = {i for i, _layer in missing}
    rows: list[dict[str, Any]] = []
    for item_id in ids:
        units = units_of[item_id]
        cell = col.cells.get(item_id)
        hits: dict[int, dict[str, list[str]]] = {}
        if cell is not None and item_id not in col.unsummarized and not args.dry_run:
            forms = [f for u in units for f in u.all_forms()]
            texts = digest.gate_texts(cell, col, joined=True, forms=forms)
            samples = [s for s in (regex.extract_phrase(t) for t in texts) if s]
            hits[digest.SUMMARY_LAYER] = regex.layer_unit_hits(samples, units, contract)
        res = regex.item_result(hits, units)
        incomplete = item_id in partial or not hits or args.dry_run
        row: dict[str, Any] = {
            "id": item_id,
            "roles": [u.role for u in units if u.required],
            "optional_roles": [u.role for u in units if not u.required],
            "pass": tri_state(res["pass"], incomplete),
            "unit_hit": res["unit_hit"],
            "unit_langs": res["unit_langs"],
            "first_lang": res["first_lang"],
            "any_hit": res["any_hit"],
            "missing_layers": [layer for i, layer in missing if i == item_id],
            "layers": {
                str(layer): {"hits": h, "empty": item_id in col.empty} for layer, h in hits.items()
            },
        }
        if cell is not None:
            row.update(col.row(item_id))
        rows.append(row)
    decided = [r for r in rows if r["pass"] is not None]
    roles = sorted({r for row in rows for r in (*row["roles"], *row["optional_roles"])})
    first_lang: dict[str, Counter[str]] = defaultdict(Counter)
    for row in decided:
        for role, lang in row["first_lang"].items():
            if lang is not None:
                first_lang[role][lang] += 1
    # the instrument is the digest (plan 0020 A6): judge_model = the digest model, pinned
    # follows the digest, version = the regex contract + the digest suffix
    judge_used = digest.summarizer_judge(args, name)
    version = digest.prompt_version(_regex_pv(name), args, name)
    result = pass_rate_result(
        name=name,
        args=dataclasses.replace(args, judge=judge_used),
        prompt_version=version,
        rows=rows,
        cells=cells,
        rep=rep,
        n_expected=len(ids) * len(layers),
        n_missing=len(missing),
        n_unjudged=len(col.unsummarized),
        n_empty=len(col.empty),
        spend=spend,
        chance_label=CHANCE_LABEL,
        config_extra={
            **digest.config(args, source_kind, name),
            "layers_judged": layers,
            "scorer": SCORER,
            "scorer_version": version,
            "contract": regex.contract_json(contract),
        },
        extras={
            "n_calls": spend.calls,
            "any_hit_rate": rate(row["any_hit"] for row in decided),
            "unit_any_layer": {
                r: rate(row["unit_hit"][r] for row in decided if r in row["unit_hit"])
                for r in roles
            },
            "language_of_readout": {r: dict(c) for r, c in sorted(first_lang.items())},
            "summary": col.extras(),
        },
        n_items=len(ids),
        pinned=judge_used.pinned,
    )
    return with_readout_count(result, scope, cells)


def _print_dry_run(
    name: str,
    contract: regex.BankContract,
    scope: list[dict[str, Any]],
    units_of: dict[str, list[regex.ScoredUnit]],
    positions: dict[tuple[str, int], list[Cell]],
    layers: list[int],
    missing: list[tuple[str, int]],
) -> None:
    print(
        f"[regex] {name}: scorer {SCORER_VERSION}, contract {regex.contract_json(contract)}, "
        f"{len(scope)} items x {len(layers)} layers, {len(missing)} missing cells"
    )
    if scope:
        first = scope[0]["id"]
        print(f"[regex] units of {first}:")
        for u in units_of[first]:
            print(f"  {u.role} (required={u.required}): {json.dumps(u.forms, ensure_ascii=False)}")
        key = next((k for k in sorted(positions) if k[0] == first), None)
        if key is not None:
            samples = _cell_samples(positions[key][0])
            print(f"[regex] first cell {key}: {len(samples)} samples, scored -> ", end="")
            print(json.dumps(regex.layer_unit_hits(samples, units_of[first]), ensure_ascii=False))


# ---------------------------------------------------------------------------------------------
# the forced-choice judge (opts=judge=mc): the instrument from 2026-09-16 to 2026-09-23
# ---------------------------------------------------------------------------------------------
def verdict(
    res: dict[str, Any] | None,
    gold_idx: int,
    options: list[str],
    readout: str,
    samples: list[str] | None = None,
) -> dict[str, Any]:
    """One judge answer: ``correct`` / ``distractor`` / ``cannot`` / ``invalid``; a correct pick
    counts only with a verbatim (folded) quote from the readout, or from ONE of ``samples`` when
    given (summary mode: the digest's evidence samples). ``invalid`` (no lone letter, or a
    letter past the option list) is judged and negative: the instrument answered, wrongly."""
    if res is None:
        return {"judged": False, "kind": "unavailable", "correct": False}
    idx = letter_index(res.get("choice"), options)
    if idx is None:
        return {
            "judged": True,
            "pick": str(res.get("choice", "")).strip() or None,
            "kind": "invalid",
            "correct": False,
            "quote_ok": False,
        }
    letter = LETTERS[idx]
    if idx == len(options) - 1:
        return {
            "judged": True,
            "pick": letter,
            "kind": "cannot",
            "correct": False,
            "quote_ok": False,
        }
    quote = str(res.get("quote", ""))
    texts = [readout] if samples is None else samples
    ok = any(quote_in(quote, s) for s in texts)
    kind = "correct" if idx == gold_idx else "distractor"
    return {
        "judged": True,
        "pick": letter,
        "kind": kind,
        "correct": idx == gold_idx and ok,
        "quote": quote,
        "quote_ok": ok,
    }


def run_mc(args: JudgeArgs, *, name: str, pinned: bool = False) -> FamilyResult:
    """The forced-choice judge, one call per (item, layer, unit). For a regex family it is not
    the instrument of record: the result is never pinned, so it is never ``complete`` and never
    enters the macro. ``pinned=True``: the family's ``scorer="mc"`` (the judge's own pin)."""
    _header, items = load_bank(bank_path(name))
    scope = item_scope(items, args)
    ids = [it["id"] for it in scope]
    by_id = {it["id"]: it for it in scope}
    profile = MC_PROFILES.get(name) if pinned else None
    cells, rep = reads.load(args, name, ids=ids)
    layers = args.layers if args.layers is not None else rep.layers
    positions: dict[tuple[str, int], list[Cell]] = defaultdict(list)
    for c in cells:
        positions[(c.id, c.layer)].append(c)
    missing = [(i, layer) for i in ids for layer in layers if (i, layer) not in positions]
    require_cells(name, missing, len(ids) * len(layers), args)
    if profile is not None:
        return _run_mc_profile(
            args,
            name=name,
            profile=profile,
            header=_header,
            scope=scope,
            ids=ids,
            by_id=by_id,
            cells=cells,
            rep=rep,
            positions=positions,
            layers=layers,
            missing=missing,
        )
    options = option_sets(items)  # over the whole bank, never the scored subset
    if digest.summary_mode(args):
        return _run_mc_summary(
            args,
            name=name,
            pinned=pinned,
            scope=scope,
            ids=ids,
            by_id=by_id,
            options=options,
            cells=cells,
            rep=rep,
            positions=positions,
            layers=layers,
            missing=missing,
        )

    spend = Spend()
    pre = Preflighter(args.dry_run)
    texts: dict[str, str] = {}
    empty: set[tuple[str, int]] = set()  # (item, layer) cells with nothing to judge: negatives
    with Cache(args.out / "cells.jsonl") as cache:
        # one readout per (item, layer): its read positions in order, each its own block (a
        # token list as its raw ranked tokens, plan 0032 §2.3.2; no interpretation step)
        for (item_id, layer), cs in sorted(positions.items()):
            blocks = []
            for c in sorted(cs, key=lambda c: c.pos):
                text = render_bag(c.tokens) if c.tokens is not None else cell_text(c)[0]
                if text.strip():
                    blocks.append(text if len(cs) == 1 else f"[pos {c.pos}] {text}")
            if blocks:
                texts[f"{item_id}|{layer}"] = "\n".join(blocks)
            else:
                empty.add((item_id, layer))
        calls: list[Call] = []
        meta: dict[
            str, tuple[str, int, str, int, list[str], str]
        ] = {}  # key -> item, layer, role, gold, options, readout
        for item_id, layer in sorted(positions):
            readout = texts.get(f"{item_id}|{layer}")
            if readout is None:
                continue
            for role, (opts, gold_idx) in options[item_id].items():
                key = f"{item_id}|L{layer:03d}|{role}"
                meta[key] = (item_id, layer, role, gold_idx, opts, readout)
                calls.append(
                    Call(
                        key=key,
                        system=SYSTEM,
                        user=render_user(readout, role, opts, native_langs(_header)),
                        meta={"item": item_id, "layer": layer, "role": role},
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
    for key, (item_id, layer, role, gold_idx, opts, readout) in meta.items():
        v = verdict(results.get(key), gold_idx, opts, readout)
        verdicts.append(
            {
                "item": item_id,
                "layer": layer,
                "role": role,
                "gold": options[item_id][role][0][gold_idx],
                **v,
            }
        )
    unsummarized: set[tuple[str, int]] = set()
    rows = [
        _item_row(i, judged_roles(by_id[i]), verdicts, missing, unsummarized, empty) for i in ids
    ]
    decided = [r for r in rows if r["pass"] is not None]
    unjudged_cells = {(v["item"], v["layer"]) for v in verdicts if not v["judged"]} | unsummarized
    kinds: dict[str, int] = defaultdict(int)
    for v in verdicts:
        kinds[v["kind"]] += 1
    roles = sorted({r for i in ids for r in judged_roles(by_id[i])})
    judge = args.judge if pinned else dataclasses.replace(args.judge, pinned=False)
    result = pass_rate_result(
        name=name,
        # a regex family's diagnostic: never pinned, so never complete and never in the macro
        args=dataclasses.replace(args, judge=judge),
        prompt_version=_spec_pv(name, PROMPT_VERSION) if pinned else PROMPT_VERSION,
        rows=rows,
        cells=cells,
        rep=rep,
        # a cell is one (item, layer) readout at the single read position; a call is one unit
        n_expected=len(ids) * len(layers),
        n_missing=len(missing),
        n_unjudged=len(unjudged_cells),
        n_empty=len(empty),
        spend=spend,
        chance_label=MC_CHANCE_LABEL,
        config_extra={
            "layers_judged": layers,
            "instrument_of_record": _spec_pv(name, PROMPT_VERSION) if pinned else SCORER_VERSION,
        },
        extras={
            "n_calls": len(calls),
            "n_unjudged_units": sum(1 for v in verdicts if not v["judged"]),
            "unit_any_layer": {
                r: rate(row["unit_correct"][r] for row in decided if r in row["unit_correct"])
                for r in roles
            },
            "kinds": dict(kinds),
            "abstain_rate": rate(v["kind"] == "cannot" for v in verdicts if v["judged"]),
        },
    )
    return with_readout_count(result, scope, cells)


def _run_mc_summary(
    args: JudgeArgs,
    *,
    name: str,
    pinned: bool = False,
    scope: list[dict[str, Any]],
    ids: list[str],
    by_id: dict[str, dict[str, Any]],
    options: dict[str, dict[str, tuple[list[str], int]]],
    cells: list[Cell],
    rep: Any,
    positions: dict[tuple[str, int], list[Cell]],
    layers: list[int],
    missing: list[tuple[str, int]],
) -> FamilyResult:
    """Summary mode (plan 0020) on the forced-choice judge: one call per (item, unit) on the
    item's digest, the quote gated by ``gate_texts``; the item passes when every judged unit is
    correct on the one digest. Item states as in ``_run_regex_summary``; never pinned."""
    spend = Spend()
    pre = Preflighter(args.dry_run)
    native = native_langs(load_bank(bank_path(name))[0])
    pv = digest.prompt_version(
        _spec_pv(name, PROMPT_VERSION) if pinned else PROMPT_VERSION, args, name
    )
    with Cache(args.out / "cells.jsonl") as cache:
        col = _collapse(name, args, ids, positions, cache=cache, spend=spend, pre=pre)
        source_kind = digest.as_prose(rep)
        note = digest.summary_note(args, source_kind, name)
        calls: list[Call] = []
        meta: dict[str, tuple[str, str, int, list[str], str, list[str]]] = {}
        layer = digest.SUMMARY_LAYER
        for item_id in ids:
            cell = col.cells.get(item_id)
            if cell is None or item_id in col.unsummarized or item_id in col.empty:
                continue
            readout = (cell.samples or ("",))[0]
            gate = digest.gate_texts(cell, col)
            for role, (opts, gold_idx) in options[item_id].items():
                key = f"{item_id}|L{layer:03d}|{role}"
                meta[key] = (item_id, role, gold_idx, opts, readout, gate)
                calls.append(
                    Call(
                        key=key,
                        system=SYSTEM,
                        user=render_user_summary(readout, note, role, opts, native),
                        meta={"item": item_id, "layer": layer, "role": role},
                    )
                )
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
    for key, (item_id, role, gold_idx, opts, readout, gate) in meta.items():
        v = verdict(results.get(key), gold_idx, opts, readout, samples=gate)
        verdicts.append(
            {
                "item": item_id,
                "layer": layer,
                "role": role,
                "gold": options[item_id][role][0][gold_idx],
                **v,
            }
        )
    unsummarized = {(i, layer) for i in col.unsummarized}
    empty = {(i, layer) for i in col.empty}
    rows = []
    for i in ids:
        row = _item_row(i, judged_roles(by_id[i]), verdicts, missing, unsummarized, empty)
        row.pop("earliest_layer")
        row.pop("passing_layers")
        if i in col.cells:
            row.update(col.row(i))
        rows.append(row)
    decided = [r for r in rows if r["pass"] is not None]
    kinds: dict[str, int] = defaultdict(int)
    for v in verdicts:
        kinds[v["kind"]] += 1
    roles = sorted({r for i in ids for r in judged_roles(by_id[i])})
    unjudged_items = {v["item"] for v in verdicts if not v["judged"]} | col.unsummarized
    judge = args.judge if pinned else dataclasses.replace(args.judge, pinned=False)
    summ_pinned = pinned and digest.pinned(args, name)
    result = pass_rate_result(
        name=name,
        # a regex family's diagnostic: never pinned, so never complete and never in the macro
        args=dataclasses.replace(args, judge=dataclasses.replace(judge, pinned=summ_pinned)),
        prompt_version=pv,
        rows=rows,
        cells=cells,
        rep=rep,
        n_expected=len(ids) * len(layers),
        n_missing=len(missing),
        n_unjudged=len(unjudged_items),
        n_empty=len(col.empty),
        spend=spend,
        chance_label=MC_CHANCE_LABEL,
        config_extra={
            **digest.config(args, source_kind, name),
            "layers_judged": layers,
            "instrument_of_record": _spec_pv(name, PROMPT_VERSION) if pinned else SCORER_VERSION,
        },
        extras={
            "n_calls": len(calls),
            "n_unjudged_units": sum(1 for v in verdicts if not v["judged"]),
            "unit_any_layer": {
                r: rate(row["unit_correct"][r] for row in decided if r in row["unit_correct"])
                for r in roles
            },
            "kinds": dict(kinds),
            "abstain_rate": rate(v["kind"] == "cannot" for v in verdicts if v["judged"]),
            "summary": col.extras(),
        },
        n_items=len(ids),
        pinned=summ_pinned,
    )
    return with_readout_count(result, scope, cells)


def _item_row(
    item_id: str,
    roles: list[str],
    verdicts: list[dict[str, Any]],
    missing: list[tuple[str, int]],
    unsummarized: set[tuple[str, int]],
    empty: set[tuple[str, int]],
) -> dict[str, Any]:
    """``pass`` = some layer where every judged unit is correct; None (undecided) when no layer
    passes and a unit verdict is unjudged, a summary failed or a layer is missing. An empty cell
    is a judged negative at that layer (nothing was read), never a reason to be undecided."""
    mine = [v for v in verdicts if v["item"] == item_id]
    by_layer: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for v in mine:
        by_layer[v["layer"]][v["role"]] = v
    for i, layer in sorted(empty):
        if i == item_id:
            by_layer[layer] = {
                r: {"judged": True, "kind": "empty", "correct": False, "quote_ok": False}
                for r in roles
            }
    passing = sorted(
        layer for layer, vs in by_layer.items() if all(vs.get(r, {}).get("correct") for r in roles)
    )
    incomplete = (
        any(not v["judged"] for v in mine)
        or any(i == item_id for i, _l in unsummarized)
        or any(i == item_id for i, _l in missing)
        or not by_layer
    )
    return {
        "id": item_id,
        "roles": roles,
        "pass": tri_state(bool(passing), incomplete),
        "earliest_layer": passing[0] if passing else None,
        "passing_layers": passing,
        "unit_correct": {
            r: any(vs.get(r, {}).get("correct", False) for vs in by_layer.values()) for r in roles
        },
        "layers": {
            str(layer): {
                r: {k: v for k, v in vs[r].items() if k not in ("item", "layer", "role")}
                for r in vs
            }
            for layer, vs in sorted(by_layer.items())
        },
    }


# ---------------------------------------------------------------------------------------------
# a family's own pinned forced-choice judge (MC_PROFILES; plan 0041, multihop_mt_v2)
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _UnitBank:
    """What the name gate of one (item, unit) needs from the bank."""

    full_forms: list[str]
    name_forms: list[str]


def _unit_banks(items: list[dict[str, Any]]) -> dict[str, dict[str, _UnitBank]]:
    """item id -> role -> its full names (``units[].full_forms``, else the unit's forms) and its
    ``name_forms`` (``name_forms[role]`` over every language, else the full names)."""
    out: dict[str, dict[str, _UnitBank]] = {}
    for it in items:
        per: dict[str, _UnitBank] = {}
        nf = it.get("name_forms") or {}
        for u in it.get("units") or []:
            role = str(u["role"])
            full = [str(f) for f in u.get("full_forms") or ()] or [
                str(f) for fs in (u.get("forms") or {}).values() for f in fs
            ]
            names = [str(f) for fs in (nf.get(role) or {}).values() for f in fs] or full
            per[role] = _UnitBank(full, names)
        out[it["id"]] = per
    return out


def _profile_verdict(
    res: dict[str, Any] | None,
    gold_idx: int,
    options: list[str],
    lines: Sequence[digest.Line],
    unit: _UnitBank | None,
    *,
    fuzzy: bool,
) -> dict[str, Any]:
    """One answer of a profile judge: ``verdict``'s kinds, the quote checked by ``digest.quote_ok``
    against ``lines`` (summary: the digest's evidence lines; native: ``digest.native_lines``),
    then the name gate; ``credit`` in :data:`CREDIT_KINDS` (``none`` unless the gold was picked)."""
    if res is None:
        return {"judged": False, "kind": "unavailable", "correct": False, "credit": "none"}
    idx = letter_index(res.get("choice"), options)
    if idx is None:
        pick = str(res.get("choice", "")).strip() or None
        return {
            "judged": True,
            "pick": pick,
            "kind": "invalid",
            "correct": False,
            "quote_ok": False,
            "credit": "none",
        }
    letter = LETTERS[idx]
    if idx == len(options) - 1:
        return {
            "judged": True,
            "pick": letter,
            "kind": "cannot",
            "correct": False,
            "quote_ok": False,
            "credit": "none",
        }
    quote = str(res.get("quote", ""))
    ok = digest.quote_ok(quote, lines)
    kind = "correct" if idx == gold_idx else "distractor"
    credit = "none"
    if kind == "correct" and not ok:
        kind = "unverified"
    if kind == "correct" and unit is not None:
        avoid = [o for i, o in enumerate(options[:-1]) if i != gold_idx]
        credit = name_credit(
            quote,
            full_forms=unit.full_forms,
            name_forms=unit.name_forms,
            fuzzy=fuzzy,
            avoid=avoid,
        )
        if credit == "none":
            kind, credit = "name_miss", "name_miss"
    return {
        "judged": True,
        "pick": letter,
        "kind": kind,
        "correct": kind == "correct" and ok,
        "quote": quote,
        "quote_ok": ok,
        "credit": credit,
    }


def _regex_columns(
    name: str,
    scope: list[dict[str, Any]],
    samples: Mapping[str, Mapping[int, list[str]]],
) -> dict[str, Any]:
    """The regex contract on the SAME evidence as the judge (no calls): ``value`` / ``any_hit_rate``
    over the items with evidence, and ``regex_any_length`` = the same contract over the bank's
    ``name_forms`` with no token-length filter (what the multi-token filter costs)."""
    header, _items = load_bank(bank_path(name))
    contract = regex.contract_for(header, str(header.get("family", name)))
    loose = dataclasses.replace(contract, multi_token=False)
    out: dict[str, Any] = {"version": _regex_pv(name), "contract": regex.contract_json(contract)}
    for key, use_names in (("regex", False), ("regex_any_length", True)):
        passes: list[bool] = []
        anys: list[bool] = []
        for it in scope:
            by_layer = samples.get(it["id"])
            if not by_layer:
                continue
            units = regex.scored_units(it, contract)
            if use_names:
                nf = it.get("name_forms") or {}
                units = [
                    dataclasses.replace(
                        u, forms={"en": [f for fs in nf[u.role].values() for f in fs]}
                    )
                    if u.role in nf
                    else u
                    for u in regex.scored_units(it, loose)
                ]
            hits = {L: regex.layer_unit_hits(s, units, contract) for L, s in by_layer.items()}
            res = regex.item_result(hits, units)
            passes.append(bool(res["pass"]))
            anys.append(bool(res["any_hit"]))
        out[key if key == "regex" else "regex_any_length"] = {
            "value": rate(passes),
            "any_hit_rate": rate(anys),
            "n_items": len(passes),
        }
    return {
        "regex": {**out["regex"], "version": out["version"], "contract": out["contract"]},
        "regex_any_length": out["regex_any_length"],
    }


def _name_misses(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v.get(k) for k in ("item", "layer", "role", "gold", "quote")}
        for v in verdicts
        if v.get("kind") == "name_miss"
    ]


def _credit_counts(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter(str(v.get("credit", "none")) for v in verdicts if v.get("judged"))
    return {k: c.get(k, 0) for k in CREDIT_KINDS}


def _run_mc_profile(
    args: JudgeArgs,
    *,
    name: str,
    profile: MCProfile,
    header: dict[str, Any],
    scope: list[dict[str, Any]],
    ids: list[str],
    by_id: dict[str, dict[str, Any]],
    cells: list[Cell],
    rep: Any,
    positions: dict[tuple[str, int], list[Cell]],
    layers: list[int],
    missing: list[tuple[str, int]],
) -> FamilyResult:
    """The pinned instrument of a profile family. Summary mode (the default): one call per
    (item, unit) on the item's digest of every loaded cell (every read position x every requested
    layer), quote checked against ``col.evidence``. Native (``opts=aggregate=native``): one call
    per (item, layer, unit), all positions of the layer as ``[pos k]`` blocks, any-layer rule and
    ``mcjudge.layer_pick_columns``. Both: the name gate, ``credit`` per unit row, the regex
    contract on the same evidence in ``extras.regex``."""
    _h, items = load_bank(bank_path(name))
    options = option_sets(items, cannot=profile.cannot)  # over the whole bank
    ubank = _unit_banks(items)
    contract = regex.contract_for(header, str(header.get("family", name)))
    fuzzy = contract.fuzzy_names
    native = native_langs(header)
    spend = Spend()
    pre = Preflighter(args.dry_run)
    summary = digest.summary_mode(args)
    pv = (
        digest.prompt_version(_spec_pv(name, PROMPT_VERSION), args, name)
        if summary
        else _spec_pv(name, PROMPT_VERSION)
    )
    calls: list[Call] = []
    meta: dict[str, tuple[str, int, str, int, list[str], Sequence[digest.Line]]] = {}
    regex_samples: dict[str, dict[int, list[str]]] = defaultdict(dict)
    empty: set[tuple[str, int]] = set()
    col: digest.Collapse | None = None
    source_kind = digest.as_prose(rep) if summary else None
    with Cache(args.out / "cells.jsonl") as cache:
        if summary:
            col = _collapse(name, args, ids, positions, cache=cache, spend=spend, pre=pre)
            note = digest.summary_note(args, source_kind, name)
            layer = digest.SUMMARY_LAYER
            for item_id in ids:
                cell = col.cells.get(item_id)
                if cell is None or item_id in col.unsummarized or item_id in col.empty:
                    continue
                readout = (cell.samples or ("",))[0]
                lines = col.evidence.get(item_id, ())
                forms = [f for u in ubank[item_id].values() for f in u.name_forms]
                texts = digest.gate_texts(cell, col, joined=True, forms=forms)
                regex_samples[item_id][layer] = [
                    s for s in (regex.extract_phrase(t) for t in texts) if s
                ]
                for role, (opts, gold_idx) in options[item_id].items():
                    key = f"{item_id}|L{layer:03d}|{role}"
                    meta[key] = (item_id, layer, role, gold_idx, opts, lines)
                    calls.append(
                        Call(
                            key=key,
                            system=profile.system,
                            user=render_user_summary(
                                readout, note, role, opts, native, question=profile.question
                            ),
                            meta={"item": item_id, "layer": layer, "role": role},
                        )
                    )
        else:
            for (item_id, layer), cs in sorted(positions.items()):
                blocks = []
                for c in sorted(cs, key=lambda c: c.pos):
                    text = render_bag(c.tokens) if c.tokens is not None else cell_text(c)[0]
                    if text.strip():
                        blocks.append(text if len(cs) == 1 else f"[pos {c.pos}] {text}")
                regex_samples[item_id][layer] = _layer_samples(cs)
                if not blocks:
                    empty.add((item_id, layer))
                    continue
                readout = "\n".join(blocks)
                lines = digest.native_lines(cs)
                for role, (opts, gold_idx) in options[item_id].items():
                    key = f"{item_id}|L{layer:03d}|{role}"
                    meta[key] = (item_id, layer, role, gold_idx, opts, lines)
                    calls.append(
                        Call(
                            key=key,
                            system=profile.system,
                            user=render_user(
                                readout, role, opts, native, question=profile.question
                            ),
                            meta={"item": item_id, "layer": layer, "role": role},
                        )
                    )
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
    for key, (item_id, layer, role, gold_idx, opts, lines) in meta.items():
        v = _profile_verdict(
            results.get(key), gold_idx, opts, lines, ubank[item_id].get(role), fuzzy=fuzzy
        )
        verdicts.append(
            {"item": item_id, "layer": layer, "role": role, "gold": opts[gold_idx], **v}
        )
    if col is not None:
        unsummarized = {(i, digest.SUMMARY_LAYER) for i in col.unsummarized}
        empty = {(i, digest.SUMMARY_LAYER) for i in col.empty}
    else:
        unsummarized = set()
    rows = []
    for i in ids:
        row = _item_row(i, judged_roles(by_id[i]), verdicts, missing, unsummarized, empty)
        row["credit"] = {
            v["role"]: v["credit"] for v in verdicts if v["item"] == i and v.get("correct")
        }
        if col is not None:
            row.pop("earliest_layer")
            row.pop("passing_layers")
            if i in col.cells:
                row.update(col.row(i))
        rows.append(row)
    decided = [r for r in rows if r["pass"] is not None]
    kinds: dict[str, int] = defaultdict(int)
    for v in verdicts:
        kinds[v["kind"]] += 1
    roles = sorted({r for i in ids for r in judged_roles(by_id[i])})
    if col is not None:
        unjudged = {v["item"] for v in verdicts if not v["judged"]} | col.unsummarized
        pinned = bool(args.judge.pinned) and digest.pinned(args, name)
        n_unjudged, n_empty = len(unjudged), len(col.empty)
    else:
        unjudged_cells = {(v["item"], v["layer"]) for v in verdicts if not v["judged"]}
        pinned = bool(args.judge.pinned)
        n_unjudged, n_empty = len(unjudged_cells), len(empty)
    extras: dict[str, Any] = {
        "n_calls": len(calls),
        "n_unjudged_units": sum(1 for v in verdicts if not v["judged"]),
        "unit_any_layer": {
            r: rate(row["unit_correct"][r] for row in decided if r in row["unit_correct"])
            for r in roles
        },
        "kinds": dict(kinds),
        "credit": _credit_counts(verdicts),
        "name_misses": _name_misses(verdicts),
        "abstain_rate": rate(v["kind"] == "cannot" for v in verdicts if v["judged"]),
        **(_regex_columns(name, scope, regex_samples) if not args.dry_run else {}),
    }
    if col is not None:
        extras["summary"] = col.extras()
    else:
        gold_of = {}
        for v in verdicts:
            label = (
                "gold"
                if v["kind"] in ("correct", "name_miss", "unverified")
                else "distractor"
                if v["kind"] == "distractor"
                else v["kind"]
            )
            gold_of.setdefault(v["item"], []).append(label)
        extras.update(layer_pick_columns([{"picks": gold_of.get(i, [])} for i in ids]))
    result = pass_rate_result(
        name=name,
        args=dataclasses.replace(args, judge=dataclasses.replace(args.judge, pinned=pinned)),
        prompt_version=pv,
        rows=rows,
        cells=cells,
        rep=rep,
        n_expected=len(ids) * len(layers),
        n_missing=len(missing),
        n_unjudged=n_unjudged,
        n_empty=n_empty,
        spend=spend,
        chance_label=MC_CHANCE_LABEL,
        config_extra={
            **(digest.config(args, source_kind, name) if col is not None else {}),
            "layers_judged": layers,
            "instrument_of_record": _spec_pv(name, PROMPT_VERSION),
            "regex_version": profile.regex_version,
        },
        extras=extras,
        n_items=len(ids) if col is not None else None,
        pinned=pinned,
    )
    return with_readout_count(result, scope, cells)
