"""Multi-token basic families. The factory (``mt_family``, plan 0032 §2.16) takes one ``scorer``:
``regex`` — the regex contract in ``wsbench.multitoken.regex`` (a layer passes when every
required unit's form is found in a sample at that layer, an item at any layer; no call; the
``opts=judge=mc`` forced-choice judge stays a diagnostic, never pinned); ``mc`` — the forced-choice
judge as the pinned instrument; ``mc+regex`` — the MC roles AND the regex units
(``wsbench.multitoken.mcrx``, plan 0054: multilingual, multilingual_multihop).
Every read position of a layer is pooled (the one-position refusal is gone); token readouts are
matched as raw tokens (the J-lens interpretation step is deleted, plan 0032 §2.3); under summary
mode (the default) every method goes through ``digest.collapse`` only."""

import dataclasses
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from wsbench import digest, mc, reads
from wsbench.banks import load_bank, native_langs
from wsbench.basic.mcjudge_bank import pieces_hold
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
from wsbench.multitoken.options import LANGUAGE_ROLE, judged_roles, option_sets
from wsbench.multitoken.prompts import (
    BRMT_QUESTION,
    BRMT_SYSTEM,
    CANNOT,
    DIGEST_TASK_NOTES,
    LETTERS,
    ML_MC_PROMPT_VERSION,
    ML_SYSTEM,
    PROMPT_VERSION,
    SCHEMA,
    SYSTEM,
    render_user,
    render_user_summary,
)
from wsbench.multitoken.regex import SCORER_VERSION
from wsbench.nearquote import close_lines
from wsbench.readouts import Cell, bulleted
from wsbench.registry import REPO_ROOT, EvalSpec, JudgeArgs, bank_path
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
    "profile_name_gate",
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


# families whose instrument tag names their bank version (plan 0040: "+v2", or the v2 MC prompts
# of the mc+regex families): the tag follows the HEADER of the bank actually judged (its
# ``version``), never the file path, so a v1-shaped bank (an adapted port under
# ``WSB_BANK_ROOT``, or ``WSB_BANK_FILE_<F>=…/items.json``) never claims the v2 instrument and a
# v2 bank anywhere keeps it
_BANK_TAGS: dict[str, str] = {}
# mc+regex families: the scorer of record of a v1-shaped bank ("bank" = the basic bank judge,
# "regex" = the regex contract; review r2 item 1), with the stamp that scorer used before v2
V1Scorer = Literal["bank", "regex"]
_V1_SCORERS: dict[str, V1Scorer] = {}


def bank_version(name: str) -> int:
    """The ``version`` of the family's bank header as loaded now (``bank_path``); 1 if absent
    or unreadable. An int (the multilingual v2 banks) or a ``"v<N>"`` string (multihop_mt_v2)."""
    header, _items = load_bank(bank_path(name))
    raw = str(header.get("version", 1)).strip().lower().removeprefix("v")
    return int(raw) if raw.isdigit() else 1


# the opts=judge=regex diagnostic of an mc+regex family (plan 0054 B5, review r4 item 2): its own
# stamp, so floor / ATI matching (strip_reads(pv) + judge model) never mistakes it for the
# family's mc+regex instrument of record
REGEX_LEG_VERSION = f"{SCORER_VERSION}+leg-regex"


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


def profile_name_gate(
    kind: str,
    quote: str,
    gold_forms: Sequence[str],
    lines: Sequence[digest.Line],
    *,
    fuzzy: bool = False,
    avoid: Sequence[str] = (),
) -> str:
    """The deterministic name gate ON TOP of ``digest.quote_ok`` (never replacing it; the same
    signature and kinds as basic_readout_mt's, plan 0051 item 8, plus ``fuzzy`` / ``avoid``): a
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


def _regex_version(name: str, pinned: bool) -> str:
    """The regex result's stamp: :func:`_regex_pv` when pinned or for a profile family (its own
    regex version, plan 0041), else the bare ``SCORER_VERSION`` (an ``mc`` family's unpinned
    ``opts=judge=regex`` diagnostic, plan 0051)."""
    if pinned or name in MC_PROFILES:
        return _regex_pv(name)
    return SCORER_VERSION


def _spec_pv(name: str, default: str) -> str:
    """The family's registered prompt version (its scorer's own, plus any instrument tag); on a
    v1-shaped bank the tag is dropped (an mc+regex family falls back to its v1 regex stamp)."""
    from wsbench import registry

    spec = registry.FAMILIES.get(name)
    if spec is None:
        return default
    pv = spec.judge.prompt_version
    tag = _BANK_TAGS.get(name)
    if tag and bank_version(name) < 2:
        if _V1_SCORERS.get(name) == SCORER:
            return SCORER_VERSION
        pv = pv.removesuffix("+" + tag)
    return pv


def _spec_task_note(name: str) -> str:
    """The family's registered ``EvalSpec.task_note`` (``mt_family(task_note=)``), else its
    ``DIGEST_TASK_NOTES`` entry: what the digest's ``task`` variant is told."""
    from wsbench import registry

    spec = registry.FAMILIES.get(name)
    return spec.task_note if spec is not None else DIGEST_TASK_NOTES.get(name, "")


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
    v1_scorer: V1Scorer | None = None,
) -> EvalSpec:
    """One multi-token family (plan 0032 §2.16). ``scorer``: ``regex`` (``EvalSpec.scorer =
    "regex"``, no call; ``opts=judge=mc`` is a forced-unpinned diagnostic), ``mc`` / ``mc+regex``
    (``EvalSpec.scorer = ""``: an LLM judge, preflighted, the result pinned). ``bank`` defaults
    to ``evals/<name>/items.json``; ``prompt_version`` to the scorer's own; ``instrument_tag`` is
    appended once (``f"{pv}+{tag}"``) and stamped only on a bank whose header is ``version >= 2``.
    ``v1_scorer`` (``mc+regex`` only): how a v1-shaped bank of the family is scored — ``"bank"``
    (the basic bank judge) or ``"regex"`` (the regex contract), each under its own v1 stamp;
    without it a v1-shaped bank is a clear error."""
    if scorer not in SCORERS:
        raise ValueError(f"{name}: scorer={scorer!r}; expected one of {SCORERS}")
    pv = prompt_version or (SCORER_VERSION if scorer == "regex" else PROMPT_VERSION)
    if instrument_tag and not pv.endswith("+" + instrument_tag):
        pv = f"{pv}+{instrument_tag}"
    if v1_scorer is not None and scorer != "mc+regex":
        raise ValueError(f"{name}: v1_scorer applies to scorer='mc+regex' only")
    if instrument_tag:
        _BANK_TAGS[name] = instrument_tag
    if v1_scorer is not None:
        _V1_SCORERS[name] = v1_scorer
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
        if which == "mc":
            return run_mc(args, name=name, pinned=True)
        if which == SCORER:  # the old regex contract: a diagnostic, never pinned
            res = run_regex(args, name=name, pinned=False)
            return dataclasses.replace(res, pinned_instrument=False, complete=False)
        fail(f"{name}: opts=judge must be 'mc' (default) or 'regex', not {which!r}")
    if scorer == "mc+regex":
        # plan 0054 B5: the MC roles AND the regex units; opts=judge=mc / =regex keep each leg
        # alone as an unpinned diagnostic (never the number of record, never in the macro)
        from wsbench.multitoken.mcrx import run_mcrx

        if bank_version(name) < 2:
            return _run_v1(args, name=name)
        leg = args.extra.get("judge", "mcrx")
        if leg == "mc":
            return run_mc(args, name=name)
        if leg == SCORER:
            r = run_regex(args, name=name, version=REGEX_LEG_VERSION)
            return dataclasses.replace(r, pinned_instrument=False, complete=False)
        return run_mcrx(args, name=name)
    which = args.extra.get("judge", SCORER)
    if which == SCORER:
        return run_regex(args, name=name)
    if which == "mc":
        return run_mc(args, name=name)
    fail(f"{name}: opts=judge must be 'regex' (default) or 'mc', not {which!r}")


def _run_v1(args: JudgeArgs, *, name: str) -> FamilyResult:
    """An mc+regex family on a v1-shaped bank (no ``mc_roles``, optional units: the adapted
    ports under ``WSB_BANK_ROOT`` until they are rebuilt, plan 0054 R6): the family's v1 scorer
    of record under its v1 stamp, as on main before plan 0040 (review r2 item 1)."""
    v1 = _V1_SCORERS.get(name)
    which = args.extra.get("judge")
    path = bank_path(name)
    if v1 is None:
        fail(
            f"{name}: {path} is a v1-shaped bank (header version < 2, no mc_roles); the mc+regex "
            "scorer needs a v2 bank — rebuild it (plan 0054 R6: wsbench.adapt emits v2)"
        )
    if v1 == "bank":
        if which not in (None, "mcrx"):
            fail(
                f"{name}: {path} is a v1 bank; it is scored by the bank judge only (no opts=judge)"
            )
        from wsbench.basic.family import run_family as run_bank

        return run_bank(args, name=name, bank=path)
    if which == "mc":
        return run_mc(args, name=name)
    if which not in (None, "mcrx", SCORER):
        fail(f"{name}: opts=judge must be 'regex' (default on a v1 bank) or 'mc', not {which!r}")
    return run_regex(args, name=name)


# ---------------------------------------------------------------------------------------------
# the regex contract (scorer of record)
# ---------------------------------------------------------------------------------------------
def _content_hit(res: Mapping[str, Any]) -> bool:
    """Some content unit (every unit but ``language``) hit at some layer: the ATI gate field of
    the multilingual families (plan 0054 item 10; the prompt names the language by itself)."""
    return any(hit for role, hit in res["unit_hit"].items() if role != "language")


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


def _split_words(cs: list[Cell], units: list[regex.ScoredUnit]) -> list[str]:
    """The single-word forms of ``units`` spelled by tokens of ADJACENT positions of this one
    layer (decision Q4; ``digest.split_word``, the verifier behind ``quote_ok``), each as its
    plain word. Prose samples and whole words of different positions never combine."""
    lines = digest.native_lines(cs)
    if not any(ln.pieces is not None for ln in lines):
        return []
    forms = {f for u in units for f in u.all_forms() if f.strip() and " " not in f.strip()}
    return sorted(f for f in forms if digest.split_word(f, lines) is not None)


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


def run_regex(
    args: JudgeArgs, *, name: str, pinned: bool = True, version: str | None = None
) -> FamilyResult:
    """The regex contract. ``pinned=False``: an ``mc`` family's ``opts=judge=regex``
    diagnostic (never pinned, so never complete and never in the macro). ``version`` overrides
    the stamp (the ``opts=judge=regex`` leg of an mc+regex family is stamped
    ``REGEX_LEG_VERSION``, never the family's mc+regex instrument); default: the family's
    registered prompt version (pinned) or ``SCORER_VERSION`` (unpinned)."""
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
            version=version,
            contract=contract,
            scope=scope,
            ids=ids,
            units_of=units_of,
            cells=cells,
            rep=rep,
            positions=positions,
            layers=layers,
            missing=missing,
            pinned=pinned,
        )
    previous_mc = _previous_mc(args.out)
    spend = Spend()
    if args.dry_run:
        _print_dry_run(name, contract, scope, units_of, positions, layers, missing)

    empty: set[tuple[str, int]] = set()
    hits_by: dict[str, dict[int, dict[str, list[str]]]] = defaultdict(dict)
    # the same hits without split words: a pass that needs one is a `split_word_hit` (plan 0033)
    plain_by: dict[str, dict[int, dict[str, list[str]]]] = defaultdict(dict)
    for (item_id, layer), cs in sorted(positions.items()):
        samples = _layer_samples(cs)
        if not samples:
            empty.add((item_id, layer))
        if args.dry_run:
            hits_by[item_id][layer] = {}
            continue
        units = units_of[item_id]
        plain_by[item_id][layer] = regex.layer_unit_hits(samples, units, contract)
        split = _split_words(cs, units)
        hits_by[item_id][layer] = (
            regex.layer_unit_hits(samples + split, units, contract)
            if split
            else plain_by[item_id][layer]
        )

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
            "split_word_hit": bool(
                res["pass"] and not regex.item_result(plain_by.get(item_id, {}), units)["pass"]
            ),
            "content_hit": _content_hit(res),
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
    # the contract alone (token readouts are matched as raw tokens)
    judge_used = REGEX_JUDGE if pinned else dataclasses.replace(REGEX_JUDGE, pinned=False)
    version = version or _regex_version(name, pinned)
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
            "n_split_word_hits": sum(bool(row["split_word_hit"]) for row in decided),
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
    pinned: bool = True,
    version: str | None = None,
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
        plain: dict[int, dict[str, list[str]]] = {}
        if cell is not None and item_id not in col.unsummarized and not args.dry_run:
            forms = [f for u in units for f in u.all_forms()]
            texts = digest.gate_texts(cell, col, joined=True, forms=forms)
            samples = [s for s in (regex.extract_phrase(t) for t in texts) if s]
            hits[digest.SUMMARY_LAYER] = regex.layer_unit_hits(samples, units, contract)
            # the same evidence without the quote_ok-verified split words (plan 0033 M3)
            ev = col.evidence.get(item_id, ())
            no_split = dataclasses.replace(
                col,
                evidence={item_id: tuple(x for x in ev if not digest.is_split_word_line(x))},
            )
            bare = digest.gate_texts(cell, no_split, joined=True)
            plain[digest.SUMMARY_LAYER] = regex.layer_unit_hits(
                [s for s in (regex.extract_phrase(t) for t in bare) if s], units, contract
            )
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
            "split_word_hit": bool(res["pass"] and not regex.item_result(plain, units)["pass"]),
            "content_hit": _content_hit(res),
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
    if not pinned:
        judge_used = dataclasses.replace(judge_used, pinned=False)
    version = digest.prompt_version(version or _regex_version(name, pinned), args, name)
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
            "n_split_word_hits": sum(bool(row["split_word_hit"]) for row in decided),
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
# the forced-choice judge: the instrument of record of an ``mc`` family (basic_readout_mt, plan
# 0033), and the regex families' ``opts=judge=mc`` diagnostic (never pinned)
# ---------------------------------------------------------------------------------------------
# basic_readout_mt's headline scores the readout unit only, in any language (plan 0051 item 1);
# the language unit is judged and reported beside it, never gated on
BRMT_HEADLINE_UNITS: tuple[str, ...] = ("readout",)


@dataclasses.dataclass(frozen=True)
class McFamily:
    """How one family runs the forced-choice judge. The default is the regex families' MC
    diagnostic, unchanged (the source's SYSTEM / CANNOT / unit question, every judged unit at one
    layer, the plain verbatim-quote check)."""

    system: str = SYSTEM
    unit_question: str | None = None  # None: the shared QUESTION_UNIT
    cannot: str = CANNOT
    headline_units: tuple[str, ...] | None = None  # None: every judged unit (the conjunction)
    name_gate: bool = False  # the deterministic name gate after the judge (plan 0051 item 8)


MC_FAMILIES: dict[str, McFamily] = {
    "basic_readout_mt": McFamily(
        system=BRMT_SYSTEM,
        unit_question=BRMT_QUESTION,
        cannot=mc.CANNOT,
        headline_units=BRMT_HEADLINE_UNITS,
        name_gate=True,
    ),
}


def mc_family(name: str) -> McFamily:
    return MC_FAMILIES.get(name, McFamily())


_CYR_LATIN = "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya i yi ye g"
_GRK_LATIN = "a v g d e z i th i k l m n x o p r s s t y f ch ps o"
_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ", _CYR_LATIN.split(" "), strict=True))
_GRK = dict(zip("αβγδεζηθικλμνξοπρσςτυφχψω", _GRK_LATIN.split(" "), strict=True))


def romanized(text: str) -> str:
    """A folded Cyrillic / Greek text in Latin letters (a fixed table, e.g. ``Тритон`` ->
    ``triton``), so a readout that writes the answer's own name in another alphabet reaches a
    Latin name form; any other character is kept. Not a translation: only the letters change."""
    t = mc.fold(text)
    out = []
    for c in t:
        r = _CYR.get(c) or _GRK.get(c)
        out.append("" if r == "_" else (r if r is not None else c))
    return "".join(out)


def name_candidates(quote: str) -> list[str]:
    """The texts a verified quote offers the name gate, all folded (``digest.fold_quote``): the
    whole quote; each of its " | " parts; and, for >= 2 parts, the parts space-joined and
    concatenated in quote order (decision Q4: tokens of ONE list may combine to read one name —
    ``digest.quote_ok`` has already proved every part is a whole token of one list); and each of
    those in Latin letters when it is Cyrillic or Greek (:func:`romanized`: ``Тритон`` ->
    ``triton``)."""
    whole = digest.fold_quote(quote)
    parts = [digest.fold_quote(p) for p in quote.split("|")]
    parts = [p for p in parts if p]
    out = [whole, *parts]
    if len(parts) >= 2:
        out += [" ".join(parts), "".join(parts)]
    out += [romanized(c) for c in out if romanized(c) != c]  # the same name in another alphabet
    return [c for c in dict.fromkeys(out) if c]


def name_langs(quote: str, forms_by_lang: Mapping[str, Sequence[str]]) -> list[str]:
    """The languages whose name forms a quote's candidate texts hold (``mc.form_in``)."""
    cands = name_candidates(quote)
    return [
        lang for lang, forms in forms_by_lang.items() if any(mc.form_in(c, forms) for c in cands)
    ]


def name_gate(
    kind: str, quote: str, gold_forms: Sequence[str], lines: Sequence[digest.Line]
) -> str:
    """The deterministic gate on a gold pick (plan 0051 item 8), on top of the shared verifier:
    ``unverified`` unless ``digest.quote_ok(quote, lines)``; ``name_miss`` unless one of the
    quote's candidate texts (:func:`name_candidates`) holds a full gold name form
    (``mc.form_in``); else ``correct``. Any other kind passes through unchanged."""
    if kind != "correct":
        return kind
    if not digest.quote_ok(quote, lines):
        return "unverified"
    if not any(mc.form_in(c, gold_forms) for c in name_candidates(quote)):
        return "name_miss"
    return "correct"


def _src_names(item: Mapping[str, Any]) -> list[str]:
    """An L2 item's source-language answer names (``target`` + ``target_alts``)."""
    if not item.get("lang"):
        return []
    return [str(x) for x in [item.get("target"), *(item.get("target_alts") or [])] if x]


def item_name_forms(item: Mapping[str, Any], role: str) -> dict[str, list[str]]:
    """lang -> the forms that credit ``role`` (the bank's ``name_forms``, else the unit's own
    ``forms``: the v1 bank has no ``name_forms``)."""
    got = (item.get("name_forms") or {}).get(role)
    if got:
        return {str(k): [str(f) for f in v] for k, v in got.items()}
    unit = next((u for u in item.get("units") or [] if u.get("role") == role), None)
    return {str(k): [str(f) for f in v] for k, v in ((unit or {}).get("forms") or {}).items()}


# decision Q4 held in code for token lines (add, never relax; the shared token note's flat-run
# rule): a gold quote whose every supporting line is a token list that also names another shown
# option, or FLAT_RUN_MIN other names of the gold's kind from the bank, is singled out by nothing
FLAT_RUN_MIN = 2


def merge_forms(entries: Sequence[Sequence[str]]) -> list[list[str]]:
    """``entries`` with every two that share a folded form merged into one (one name listed
    under two spellings, or as a unit and as a distractor, is ONE rival, never two)."""
    groups: list[tuple[set[str], list[str]]] = []
    for e in entries:
        forms = [str(f) for f in e if str(f).strip()]
        keys = {mc.fold(f).strip() for f in forms}
        if not keys:
            continue
        hit = [g for g in groups if g[0] & keys]
        merged_keys, merged = set(keys), []
        for g in hit:
            merged_keys |= g[0]
            merged += g[1]
            groups.remove(g)
        merged += [f for f in forms if f not in merged]
        groups.append((merged_keys, merged))
    return [g[1] for g in groups]


def bank_entities(items: Sequence[Mapping[str, Any]]) -> dict[str, list[list[str]]]:
    """kind -> the bank's names of that kind, one entry (its forms) per name: every name unit's
    full names (``units[].full_forms``, else its forms; never the ``language`` unit) and every
    ``mc`` gold and distractor, over the WHOLE bank (never the judged subset), entries sharing a
    form merged (:func:`merge_forms`). ``kind`` is the unit's ``kind`` (multihop_mt_v2: person /
    work / event), ``""`` for a bank without kinds (one pool)."""
    raw: dict[str, list[list[str]]] = defaultdict(list)
    for it in items:
        units = [u for u in it.get("units") or [] if str(u.get("role")) != LANGUAGE_ROLE]
        kinds = {str(u.get("role")): str(u.get("kind") or "") for u in units}
        for u in units:
            full = [str(f) for f in u.get("full_forms") or ()] or [
                str(f) for fs in (u.get("forms") or {}).values() for f in fs
            ]
            raw[kinds[str(u.get("role"))]].append(full)
        for role, blk in (it.get("mc") or {}).items():
            if not isinstance(blk, Mapping) or str(role) == LANGUAGE_ROLE:
                continue
            for o in [blk.get("gold"), *(blk.get("distractors") or [])]:
                if o:
                    raw[kinds.get(str(role), "")].append([str(o)])
    return {k: merge_forms(v) for k, v in raw.items()}


def list_gate(
    quote: str,
    lines: Sequence[digest.Line],
    *,
    gold_forms: Sequence[str],
    others: Sequence[str],
    rivals: Sequence[Sequence[str]],
    min_names: int = FLAT_RUN_MIN,
) -> dict[str, bool]:
    """``{multi_option, flat_run}`` of a verified gold quote: every line that supports it (the
    lines holding it, else the split word's lines) is a token list that also names another shown
    option (``others``), or names >= ``min_names`` of ``rivals`` (the bank's other names of the
    gold's kind, :func:`bank_entities`; a rival sharing a form with the gold is never counted). A
    prose line, or a quote no line supports, is never voided here."""
    support = [ln for ln in lines if digest.quote_ok(quote, [ln])]
    support = support or (digest.split_word(quote, lines) or [])
    if not support or any(ln.pieces is None for ln in support):
        return {"multi_option": False, "flat_run": False}
    gold = {mc.fold(f).strip() for f in gold_forms if str(f).strip()}
    rv = [e for e in rivals if not ({mc.fold(f).strip() for f in e} & gold)]
    multi = all(
        any(pieces_hold(ln.pieces or [], [o]) for o in others if mc.fold(o).strip() not in gold)
        for ln in support
    )
    flat = all(sum(1 for e in rv if pieces_hold(ln.pieces or [], e)) >= min_names for ln in support)
    return {"multi_option": multi, "flat_run": flat}


def _void(out: dict[str, Any], gate: dict[str, bool]) -> dict[str, Any]:
    """A credited verdict voided by :func:`list_gate` (kind ``flat_list``, never correct)."""
    out = {**out, **gate}
    if out.get("kind") == "correct" and (gate["multi_option"] or gate["flat_run"]):
        out.update({"kind": "flat_list", "correct": False})
        if "credit" in out:
            out["credit"] = "none"
    return out


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


def distinct_forms(
    forms_by_lang: Mapping[str, Sequence[str]], src: str | None, src_names: Sequence[str] = ()
) -> dict[str, list[str]]:
    """``forms_by_lang`` with every other language's copy of a source-language NAME removed
    (``src_names``: the bank's ``target`` / ``target_alts``; an authored "en" list may hold the
    source spelling "Ahvenanmaa"): what a quote must hold to count as named in that other
    language (``translated_pass_rate``). A name both languages write alike ("Åland") stays."""
    if not src or src not in forms_by_lang:
        return {k: list(v) for k, v in forms_by_lang.items()}
    own = {mc.fold(f).strip() for f in src_names}
    return {
        k: list(v) if k == src else [f for f in v if mc.fold(f).strip() not in own]
        for k, v in forms_by_lang.items()
    }


def gated_verdict(
    res: dict[str, Any] | None,
    gold_idx: int,
    options: list[str],
    lines: Sequence[digest.Line],
    forms_by_lang: Mapping[str, Sequence[str]],
    src: str | None = None,
    src_names: Sequence[str] = (),
    rivals: Sequence[Sequence[str]] = (),
    *,
    tolerant: bool = False,
) -> dict[str, Any]:
    """:func:`verdict` under the name gate: the quote is checked by ``digest.quote_ok`` against
    ``lines`` (``col.evidence[item]`` or ``digest.native_lines``), then :func:`name_gate`, then
    :func:`list_gate` (``rivals``: the bank's other names of the gold's kind).
    ``langs``: the languages whose gold forms the quote holds, a source-language spelling counted
    for the source language only (:func:`distinct_forms`; ``translated_pass_rate``).
    ``tolerant`` (basic_readout_mt since audit fix F4): :func:`tolerant_gate` instead."""
    v = verdict(res, gold_idx, options, "", samples=[])
    if v["kind"] not in ("correct", "distractor"):
        return v
    if tolerant:
        return tolerant_gate(v, gold_idx, options, lines, forms_by_lang, src, src_names, rivals)
    quote = str(v.get("quote", ""))
    ok = digest.quote_ok(quote, lines)
    gold_forms = [f for fs in forms_by_lang.values() for f in fs]
    kind = name_gate(v["kind"], quote, gold_forms, lines)
    out = {**v, "kind": kind, "quote_ok": ok, "correct": kind == "correct"}
    if kind == "correct":
        others = [o for i, o in enumerate(options[:-1]) if i != gold_idx]
        gold = [*gold_forms, options[gold_idx]]
        out = _void(out, list_gate(quote, lines, gold_forms=gold, others=others, rivals=rivals))
    if out["kind"] == "correct":
        out["langs"] = name_langs(quote, distinct_forms(forms_by_lang, src, src_names))
    return out


# ---------------------------------------------------------------------------------------------
# audit fix F4 (2026-10-31): the tolerant name gate of basic_readout_mt. The strict gate read
# only the judge's ONE quote with exact word boundaries, so 24 of 25 name_miss / unverified cells
# were correct picks: a quote in a third script (Persian, Cyrillic, Spanish) beside English lines
# naming the gold, a case ending (Ålands, Konstantinopolu), a Korean particle (일연입니다), an
# Arabic spelling without the article (طيب صالح), a quote one word off.
# ---------------------------------------------------------------------------------------------
# source languages whose forms may run on into a case ending / possessive / particle
INFLECTING = frozenset(
    {"fi", "hu", "et", "tr", "pl", "ru", "uk", "cs", "sk", "sl", "hr", "sr", "bg", "lt", "lv"}
)
SUFFIX_LEN = 5  # letters a form of an inflecting language may run on into (Ahvenanmaa+lla)
EN_ENDINGS = ("", "s")  # an English-list form may only take a plural / possessive -s


def _hit_ok(word: str, form: str, lang: str, src: str | None, heads: set[str]) -> bool:
    ext = word[len(form) :] if word.startswith(form) else ""
    if not ext:
        return True
    if lang == "en" and ext not in EN_ENDINGS:
        return False
    # a run-on that turns the form into another option's name (or its start) never credits
    return not any(h and (word == h or word.startswith(h) or h.startswith(word)) for h in heads)


def tolerant_names(
    text: str, forms: Sequence[str], lang: str, src: str | None, avoid: Sequence[str]
) -> bool:
    """``text`` names the gold by one of ``forms`` (the forms of language ``lang``) under the F4
    rules: ``mc.form_hits`` with Arabic clitics / letter variants folded, a run-on of up to
    :data:`SUFFIX_LEN` letters when ``lang`` or the item's source language ``src`` inflects (an
    English-list form: only ``-s``), never a run-on that spells another option (``avoid``);
    then the misspelling rule ``regex.fuzzy_form_in`` (Latin script, first letter kept, never
    near an ``avoid`` option; it also reads the romanized Cyrillic / Greek candidates)."""
    inflect = lang in INFLECTING or (src in INFLECTING and lang != "en")
    suffix = SUFFIX_LEN if inflect else (1 if lang == "en" else 0)
    heads = {mc.ar_fold(a).strip() for a in avoid}
    for f in forms:
        ff = mc.ar_fold(f).strip()
        for w in mc.form_hits(text, [f], suffix=suffix, clitics=True, hangul=True):
            if _hit_ok(w, ff, lang, src, heads):
                return True
    return regex.fuzzy_form_in(text, list(forms), avoid=list(avoid))


def _texts_of(ln: digest.Line) -> list[str]:
    """The texts of one line a name is read in: each bullet of a prose line on its own (a name
    is never assembled across bullets) and its romanized form; a token line as its tokens, the
    tokens space-joined and concatenated (decision Q4: one list may spell one name)."""
    if ln.pieces is not None:
        ps = [p for p in ln.pieces if p.strip()]
        out = [*ps, " ".join(ps), "".join(p.strip() for p in ps)]
    else:
        out = [b for b in ln.text.split(mc.BULLET_MARK) if b.strip()]
    out = [digest.fold_quote(x) for x in out if digest.fold_quote(x)]
    return list(dict.fromkeys([*out, *(romanized(x) for x in out)]))


def _named_langs(
    texts: Sequence[str],
    forms_by_lang: Mapping[str, Sequence[str]],
    src: str | None,
    avoid: Sequence[str],
) -> list[str]:
    return [
        lang
        for lang, forms in forms_by_lang.items()
        if forms and any(tolerant_names(t, forms, lang, src, avoid) for t in texts)
    ]


def _stem_hit(text: str, option: str, src: str | None) -> bool:
    """For an inflecting source language, another option also counts as named when a word of
    ``text`` starts with its stem (each word of the option cut by 2 letters, at least 5 kept:
    ``Gotland`` -> ``gotla…`` finds ``Gotlannissa``). Used only to BLOCK a line rescue, so a
    looser match here can only make the gate stricter."""
    if src not in INFLECTING:
        return False
    ws = [mc.fold(w) for w in option.split() if w.strip()]
    if not ws or any(len(w) < 6 or not w.isalpha() for w in ws):
        return False
    pat = r"\s+".join(rf"(?<!\w){re.escape(w[: max(5, len(w) - 2)])}\w*" for w in ws)
    return re.search(pat, mc.fold(text)) is not None


def _other_named(text: str, option: str, olang: str, src: str | None) -> bool:
    """Another shown option is named in ``text``: strictly, as tolerantly as the gold, or by its
    stem in an inflecting source language (review r2 of PR #195)."""
    return (
        mc.form_in(text, [option])
        or tolerant_names(text, [option], olang, src, [])
        or _stem_hit(text, option, src)
    )


def singles_out(
    ln: digest.Line,
    forms_by_lang: Mapping[str, Sequence[str]],
    others: Sequence[str],
    rivals: Sequence[Sequence[str]],
    src: str | None,
) -> list[str]:
    """The languages in which line ``ln`` SINGLES OUT the gold (F4: "credit the pick if ANY
    digest line containing the picked option holds a name form"): one of its texts (one bullet,
    or one token list) names the gold (:func:`tolerant_names`) and names no other shown option
    (``others``, strict ``mc.form_in``); a token list that also holds ``FLAT_RUN_MIN`` names of
    the gold's kind (``rivals``) singles out nothing (decision Q4). ``[]`` = it does not."""
    gold_keys = {mc.fold(f).strip() for fs in forms_by_lang.values() for f in fs}
    if ln.pieces is not None:
        rv = [e for e in rivals if not ({mc.fold(f).strip() for f in e} & gold_keys)]
        if sum(1 for e in rv if pieces_hold(ln.pieces, e)) >= FLAT_RUN_MIN:
            return []
        if any(pieces_hold(ln.pieces, [o]) for o in others):
            return []
        return _named_langs(_texts_of(ln), forms_by_lang, src, others)
    langs: list[str] = []
    olang = src if src in INFLECTING else "en"
    for text in _texts_of(ln):
        # another option is matched as tolerantly as the gold (case endings, clitics, fuzzy;
        # review r2 of PR #195: "Ahvenanmaalla vai Gotlannissa?" names Gotland too)
        if any(_other_named(text, o, olang, src) for o in others):
            continue
        langs += [x for x in _named_langs([text], forms_by_lang, src, others) if x not in langs]
    return langs


def tolerant_gate(
    v: dict[str, Any],
    gold_idx: int,
    options: list[str],
    lines: Sequence[digest.Line],
    forms_by_lang: Mapping[str, Sequence[str]],
    src: str | None,
    src_names: Sequence[str],
    rivals: Sequence[Sequence[str]],
) -> dict[str, Any]:
    """The F4 name gate on a gold pick, ON TOP of ``digest.quote_ok`` (adding a near-quote
    fallback, never replacing it):

    1. the quote verifies (``quote_ok``), or near-verbatim (``nearquote.close_lines``,
       ``quote_near``) — a near quote's own words then count for nothing, only the located
       lines' text does; neither -> ``unverified``;
    2. the gold is named (``credit_via``): by the verified quote, or by the near quote's line,
       under :func:`tolerant_names` (``quote``); else by ANY line that singles it out
       (:func:`singles_out`; ``line``); neither -> ``name_miss``;
    3. a quote-credited pick still goes through :func:`list_gate` (a token list naming another
       option or a flat run of the gold's kind is voided).

    ``langs`` = the languages named by the crediting texts and by every line that singles out
    the gold, a source-language spelling counted for the source language only."""
    quote = str(v.get("quote", ""))
    ok = digest.quote_ok(quote, lines)
    if v["kind"] != "correct":
        return {**v, "quote_ok": ok, "correct": False}
    others = [o for i, o in enumerate(options[:-1]) if i != gold_idx]
    near = [] if ok else close_lines(quote, lines)
    out: dict[str, Any] = {**v, "quote_ok": ok, "correct": False}
    if near:
        out["quote_near"] = True
    if not ok and not near:
        return {**out, "kind": "unverified"}
    forms = distinct_forms(forms_by_lang, src, src_names)
    if ok:
        texts = list(dict.fromkeys([*_quote_texts(quote)]))
    else:
        texts = [t for ln in near for t in _texts_of(ln)]
    q_langs = _named_langs(texts, forms_by_lang, src, others)
    single: dict[str, list[str]] = {}
    for ln in lines:
        got = singles_out(ln, forms_by_lang, others, rivals, src)
        if got:
            single[ln.text] = got
    if q_langs:
        out.update(kind="correct", correct=True, credit_via="quote")
        if ok:
            gold = [*(f for fs in forms_by_lang.values() for f in fs), options[gold_idx]]
            gate = list_gate(quote, lines, gold_forms=gold, others=others, rivals=rivals)
            out = _void(out, gate)
    elif single:
        out.update(kind="correct", correct=True, credit_via="line")
    else:
        return {**out, "kind": "name_miss"}
    if out["kind"] == "correct":
        dl = set(_named_langs(texts, forms, src, others)) if q_langs else set()
        for ln in lines:
            if ln.text in single:
                dl |= set(singles_out(ln, forms, others, rivals, src))
        out["langs"] = [lang for lang in forms if lang in dl]
    return out


def _quote_texts(quote: str) -> list[str]:
    """A verified quote's candidate texts (:func:`name_candidates`), split at bullet marks too."""
    out: list[str] = []
    for part in quote.split(mc.BULLET_MARK):
        if part.strip():
            out += name_candidates(part)
    return out


def quoted_verdict(
    res: dict[str, Any] | None, gold_idx: int, options: list[str], lines: Sequence[digest.Line]
) -> dict[str, Any]:
    """:func:`verdict` with the quote checked by ``digest.quote_ok`` and no name gate: a
    name-gate family's ``language`` unit, whose evidence may be the passage's own words in its
    script rather than a name (``QUESTION_LANGUAGE``)."""
    v = verdict(res, gold_idx, options, "", samples=[])
    if v["kind"] not in ("correct", "distractor"):
        return v
    ok = digest.quote_ok(str(v.get("quote", "")), lines)
    return {**v, "quote_ok": ok, "correct": v["kind"] == "correct" and ok}


def _gate(
    cfg: McFamily,
    role: str,
    res: dict[str, Any] | None,
    gold_idx: int,
    options: list[str],
    lines: Sequence[digest.Line],
    item: Mapping[str, Any],
    entities: Mapping[str, list[list[str]]] | None = None,
) -> dict[str, Any]:
    """A name-gate family's verdict for one unit: the name gate (and :func:`list_gate` over
    ``entities``, :func:`bank_entities`) on a name unit, the plain verified quote on the
    ``language`` unit."""
    if role == LANGUAGE_ROLE:
        return quoted_verdict(res, gold_idx, options, lines)
    return gated_verdict(
        res, gold_idx, options, lines, gate_name_forms(item, role), item.get("lang"),
        _src_names(item), rivals=_rivals(entities, item, role), tolerant=True,
    )  # fmt: skip


EXONYMS_FILE = REPO_ROOT / "evals/basic_readout_mt/exonyms.json"


def _exonyms() -> dict[str, dict[str, dict[str, list[str]]]]:
    """item id -> role -> lang -> extra gold forms (audit fix F4: the ru / es / fr / de / fa
    exonyms of each gold, ``evals/basic_readout_mt/exonyms.json``; a frozen gate table beside
    the bank, which stays byte-identical). Missing file = no extra forms."""
    if not EXONYMS_FILE.exists():
        return {}
    data = json.loads(EXONYMS_FILE.read_text(encoding="utf-8"))
    return {str(k): v for k, v in (data.get("items") or {}).items()}


_EXONYM_CACHE: dict[str, dict[str, dict[str, dict[str, list[str]]]]] = {}


def gate_name_forms(item: Mapping[str, Any], role: str) -> dict[str, list[str]]:
    """:func:`item_name_forms` plus the item's frozen exonyms (:func:`_exonyms`), each new
    language after the bank's own (a form the bank already lists is not repeated)."""
    if "x" not in _EXONYM_CACHE:
        _EXONYM_CACHE["x"] = _exonyms()
    out = item_name_forms(item, role)
    extra = (_EXONYM_CACHE["x"].get(str(item.get("id") or item.get("name"))) or {}).get(role)
    have = {mc.fold(f).strip() for fs in out.values() for f in fs}
    for lang, fs in (extra or {}).items():
        add = [str(f) for f in fs if mc.fold(str(f)).strip() not in have]
        if add:
            out.setdefault(str(lang), [])
            out[str(lang)] += add
            have |= {mc.fold(f).strip() for f in add}
    return out


def _rivals(
    entities: Mapping[str, list[list[str]]] | None, item: Mapping[str, Any], role: str
) -> list[list[str]]:
    """The bank names of ``role``'s kind (the unit's ``kind``, else the bank's one pool)."""
    if not entities:
        return []
    unit = next((u for u in item.get("units") or [] if str(u.get("role")) == role), None)
    return list(entities.get(str((unit or {}).get("kind") or ""), []))


def _native_block(c: Cell, cfg: McFamily) -> str:
    """One read position's text: a token list as its raw ranked tokens (no scores, no
    interpretation step, plan 0032 §2.3); a prose cell its samples (a family of record joins
    several samples of one activation as bullets, ``readouts.bulleted``)."""
    if c.tokens is not None:
        return render_bag(c.tokens)
    if cfg.name_gate and len([x for x in c.samples or () if x.strip()]) > 1:
        return bulleted(c.samples or ())
    return cell_text(c)[0]


def _regex_column(
    name: str,
    header: Mapping[str, Any],
    scope: list[dict[str, Any]],
    positions: Mapping[tuple[str, int], list[Cell]],
    col: digest.Collapse | None,
) -> dict[str, Any]:
    """The old regex contract on the same evidence, no call (``extras.regex``; plan 0051 item 1):
    a report column for continuity with the instrument history, never a gate."""
    contract = regex.contract_for(header, str(header.get("family", name)))
    rows = []
    for it in scope:
        try:
            units = regex.scored_units(it, contract)
        except ValueError:
            continue
        hits: dict[int, dict[str, list[str]]] = {}
        if col is not None:
            cell = col.cells.get(it["id"])
            if cell is None or it["id"] in col.unsummarized:
                continue
            forms = [f for u in units for f in u.all_forms()]
            texts = digest.gate_texts(cell, col, joined=True, forms=forms)
            samples = [x for x in (regex.extract_phrase(t) for t in texts) if x]
            hits[digest.SUMMARY_LAYER] = regex.layer_unit_hits(samples, units)
        else:
            for (i, layer), cs in positions.items():
                if i == it["id"]:
                    hits[layer] = regex.layer_unit_hits(_layer_samples(cs), units)
        rows.append(regex.item_result(hits, units))
    return {
        "value": rate(bool(r["pass"]) for r in rows),
        "any_hit_rate": rate(bool(r["any_hit"]) for r in rows),
        "n_items": len(rows),
        "scorer_version": SCORER_VERSION,
    }


def _family_extras(
    rows: list[dict[str, Any]], by_id: Mapping[str, dict[str, Any]], native: tuple[str, ...]
) -> dict[str, Any]:
    """basic_readout_mt's report columns beside the headline (plan 0051 items 1, 3): the
    translated variant (an L2 item's answer named in a native language), the pre-2026-10-30
    conjunction (answer AND question language), the language unit alone (L2 items), per
    subfamily."""
    decided = [r for r in rows if r["pass"] is not None]
    l2 = [r for r in decided if by_id[r["id"]].get("lang")]

    def cols(rs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(rs),
            "pass_rate": rate(bool(r["pass"]) for r in rs),
            "translated_pass_rate": rate(bool(r["translated_pass"]) for r in rs),
        }

    subs = sorted({str(by_id[r["id"]].get("subfamily", "")) for r in decided})
    return {
        "translated_pass_rate": rate(bool(r["translated_pass"]) for r in decided),
        "conjunctive_pass_rate": rate(bool(r["conjunctive_pass"]) for r in decided),
        "language_unit_rate": rate(bool(r["unit_correct"].get("language")) for r in l2),
        "n_l2_items_decided": len(l2),
        "n_l2_pass_not_conjunctive": sum(1 for r in l2 if r["pass"] and not r["conjunctive_pass"]),
        "by_subfamily": {
            s: cols([r for r in decided if by_id[r["id"]].get("subfamily") == s]) for s in subs
        },
        "native_langs": list(native),
    }


def is_v2_multilingual(header: Mapping[str, Any], items: list[dict[str, Any]]) -> bool:
    """A multilingual v2 bank (plan 0054): its MC calls use the v2 prompts (``ML_SYSTEM``, the
    names-only language question) under ``ML_MC_PROMPT_VERSION``."""
    return any(it.get("mc_roles") for it in items)


def run_mc(args: JudgeArgs, *, name: str, pinned: bool = False) -> FamilyResult:
    """The forced-choice judge, one call per (item, layer, unit). For a regex family it is not
    the instrument of record: the result is never pinned, so it is never ``complete`` and never
    enters the macro. ``pinned=True``: the family's ``scorer="mc"`` (the judge's own pin). On a
    multilingual v2 bank the calls use the v2 prompts (``is_v2_multilingual``)."""
    cfg = mc_family(name)
    header, items = load_bank(bank_path(name))
    native = native_langs(header)
    v2 = is_v2_multilingual(header, items)
    base_system = ML_SYSTEM if v2 else cfg.system
    diag_pv = ML_MC_PROMPT_VERSION if v2 else PROMPT_VERSION
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
            header=header,
            scope=scope,
            ids=ids,
            by_id=by_id,
            cells=cells,
            rep=rep,
            positions=positions,
            layers=layers,
            missing=missing,
        )
    options = option_sets(items, cannot=cfg.cannot)  # over the whole bank, never the subset
    entities = bank_entities(items)  # list_gate's rivals, over the whole bank too
    if digest.summary_mode(args):
        return _run_mc_summary(
            args,
            name=name,
            pinned=pinned,
            header=header,
            scope=scope,
            ids=ids,
            by_id=by_id,
            options=options,
            cells=cells,
            rep=rep,
            positions=positions,
            layers=layers,
            missing=missing,
            v2=v2,
            entities=entities,
        )

    pv = _spec_pv(name, PROMPT_VERSION) if pinned else diag_pv
    spend = Spend()
    pre = Preflighter(args.dry_run)
    token_note = digest.token_note_of(name) if v2 else ""
    texts: dict[str, str] = {}
    lines_of: dict[tuple[str, int], list[digest.Line]] = {}
    tokens_at: set[tuple[str, int]] = set()
    empty: set[tuple[str, int]] = set()  # (item, layer) cells with nothing to judge: negatives
    with Cache(args.out / "cells.jsonl") as cache:
        # one readout per (item, layer): its read positions in order, each its own block headed
        # by its offset only (no token text; plan 0051 item 2)
        for (item_id, layer), cs in sorted(positions.items()):
            blocks = []
            for c in sorted(cs, key=lambda c: c.pos):
                text = _native_block(c, cfg)
                if c.tokens is not None:
                    tokens_at.add((item_id, layer))
                if text.strip():
                    blocks.append(text if len(cs) == 1 else f"[pos {c.pos}] {text}")
            if blocks:
                texts[f"{item_id}|{layer}"] = "\n".join(blocks)
                lines_of[(item_id, layer)] = digest.native_lines(cs)
            else:
                empty.add((item_id, layer))
        token_system = cfg.system + "\n\n" + digest.token_note_of(name)
        calls: list[Call] = []
        meta: dict[
            str, tuple[str, int, str, int, list[str], str]
        ] = {}  # key -> item, layer, role, gold, options, readout
        for item_id, layer in sorted(positions):
            readout = texts.get(f"{item_id}|{layer}")
            if readout is None:
                continue
            tok = v2 and any(c.tokens is not None for c in positions[(item_id, layer)])
            for role, (opts, gold_idx) in options[item_id].items():
                key = f"{item_id}|L{layer:03d}|{role}"
                meta[key] = (item_id, layer, role, gold_idx, opts, readout)
                system = (
                    token_system if cfg.name_gate and (item_id, layer) in tokens_at else base_system
                )
                calls.append(
                    Call(
                        key=key,
                        system=system,
                        user=render_user(
                            readout,
                            role,
                            opts,
                            native,
                            cfg.unit_question,
                            v2=v2,
                            token_note=token_note if tok else "",
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
    for key, (item_id, layer, role, gold_idx, opts, readout) in meta.items():
        if cfg.name_gate:
            v = _gate(
                cfg,
                role,
                results.get(key),
                gold_idx,
                opts,
                lines_of.get((item_id, layer), []),
                by_id[item_id],
                entities,
            )
        else:
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
        _item_row(
            i, judged_roles(by_id[i]), verdicts, missing, unsummarized, empty, cfg, by_id[i], native
        )
        for i in ids
    ]
    decided = [r for r in rows if r["pass"] is not None]
    unjudged_cells = {(v["item"], v["layer"]) for v in verdicts if not v["judged"]} | unsummarized
    kinds: dict[str, int] = defaultdict(int)
    for v in verdicts:
        kinds[v["kind"]] += 1
    roles = sorted({r for i in ids for r in judged_roles(by_id[i])})
    judge = args.judge if pinned else dataclasses.replace(args.judge, pinned=False)
    extras: dict[str, Any] = {
        "n_calls": len(calls),
        "n_unjudged_units": sum(1 for v in verdicts if not v["judged"]),
        "unit_any_layer": {
            r: rate(row["unit_correct"][r] for row in decided if r in row["unit_correct"])
            for r in roles
        },
        "kinds": dict(kinds),
        "abstain_rate": rate(v["kind"] == "cannot" for v in verdicts if v["judged"]),
    }
    if cfg.name_gate:
        extras.update(_family_extras(rows, by_id, native))
        extras["name_misses"] = _name_misses(verdicts)
        extras["n_gold_pick_flat_list"] = sum(v.get("kind") == "flat_list" for v in verdicts)
        extras["regex"] = _regex_column(name, header, scope, positions, None)
        extras.update(layer_pick_columns(rows))
    result = pass_rate_result(
        name=name,
        # a regex family's diagnostic: never pinned, so never complete and never in the macro
        args=dataclasses.replace(args, judge=judge),
        prompt_version=pv,
        rows=rows,
        cells=cells,
        rep=rep,
        # a cell is one (item, layer) readout (every read position of the layer); a call is one
        # unit
        n_expected=len(ids) * len(layers),
        n_missing=len(missing),
        n_unjudged=len(unjudged_cells),
        n_empty=len(empty),
        spend=spend,
        chance_label=MC_CHANCE_LABEL,
        config_extra={
            "layers_judged": layers,
            "instrument_of_record": pv if pinned else SCORER_VERSION,
        },
        extras=extras,
    )
    return with_readout_count(result, scope, cells)


def _name_misses(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"item": v["item"], "layer": v["layer"], "role": v["role"], "quote": v.get("quote", "")}
        for v in verdicts
        if v["kind"] == "name_miss"
    ]


def _run_mc_summary(
    args: JudgeArgs,
    *,
    name: str,
    pinned: bool = False,
    header: Mapping[str, Any],
    scope: list[dict[str, Any]],
    ids: list[str],
    by_id: dict[str, dict[str, Any]],
    options: dict[str, dict[str, tuple[list[str], int]]],
    cells: list[Cell],
    rep: Any,
    positions: dict[tuple[str, int], list[Cell]],
    layers: list[int],
    missing: list[tuple[str, int]],
    v2: bool = False,
    entities: Mapping[str, list[list[str]]] | None = None,
) -> FamilyResult:
    """Summary mode (plan 0020) on the forced-choice judge: one call per (item, unit) on the
    item's digest, the quote gated by ``gate_texts`` (a name-gate family: ``digest.quote_ok`` on
    the digest's evidence lines, then :func:`name_gate`); the item passes when every headline
    unit is correct on the one digest. Item states as in ``_run_regex_summary``."""
    cfg = mc_family(name)
    spend = Spend()
    pre = Preflighter(args.dry_run)
    native = native_langs(dict(header))
    diag_pv = ML_MC_PROMPT_VERSION if v2 else PROMPT_VERSION
    pv = digest.prompt_version(_spec_pv(name, PROMPT_VERSION) if pinned else diag_pv, args, name)
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
                        system=ML_SYSTEM if v2 else cfg.system,
                        user=render_user_summary(
                            readout, note, role, opts, native, cfg.unit_question, v2=v2
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
    for key, (item_id, role, gold_idx, opts, readout, gate) in meta.items():
        if cfg.name_gate:
            v = _gate(
                cfg,
                role,
                results.get(key),
                gold_idx,
                opts,
                list(col.evidence.get(item_id, ())),
                by_id[item_id],
                entities,
            )
        else:
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
        row = _item_row(
            i, judged_roles(by_id[i]), verdicts, missing, unsummarized, empty, cfg, by_id[i], native
        )
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
    extras: dict[str, Any] = {
        "n_calls": len(calls),
        "n_unjudged_units": sum(1 for v in verdicts if not v["judged"]),
        "unit_any_layer": {
            r: rate(row["unit_correct"][r] for row in decided if r in row["unit_correct"])
            for r in roles
        },
        "kinds": dict(kinds),
        "abstain_rate": rate(v["kind"] == "cannot" for v in verdicts if v["judged"]),
        "summary": col.extras(),
    }
    if cfg.name_gate:
        extras.update(_family_extras(rows, by_id, native))
        extras["name_misses"] = _name_misses(verdicts)
        extras["n_gold_pick_flat_list"] = sum(v.get("kind") == "flat_list" for v in verdicts)
        extras["regex"] = _regex_column(name, header, scope, positions, col)
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
        extras=extras,
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
    cfg: McFamily | None = None,
    item: Mapping[str, Any] | None = None,
    native: tuple[str, ...] = (),
) -> dict[str, Any]:
    """``pass`` = some layer where every headline unit is correct (``cfg.headline_units``; by
    default every judged unit); None (undecided) when no layer passes and a unit verdict is
    unjudged, a summary failed or a layer is missing. An empty cell is a judged negative at that
    layer (nothing was read), never a reason to be undecided. A name-gate family's row also
    carries ``conjunctive_pass`` (every judged unit at one layer), ``translated_pass`` (the
    headline with an L2 item's answer named in a native language), ``any_hit`` and per-layer
    ``picks`` (``mcjudge.layer_pick_columns``)."""
    cfg = cfg or McFamily()
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
    head = [r for r in roles if cfg.headline_units is None or r in cfg.headline_units]
    passing = sorted(
        layer for layer, vs in by_layer.items() if all(vs.get(r, {}).get("correct") for r in head)
    )
    incomplete = (
        any(not v["judged"] for v in mine)
        or any(i == item_id for i, _l in unsummarized)
        or any(i == item_id for i, _l in missing)
        or not by_layer
    )
    row: dict[str, Any] = {
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
    if cfg.headline_units is not None:
        src = bool((item or {}).get("lang"))

        def translated(vs: Mapping[str, dict[str, Any]]) -> bool:
            if not all(vs.get(r, {}).get("correct") for r in head):
                return False
            return not src or all(set(vs[r].get("langs") or ()) & set(native) for r in head)

        row["headline_units"] = head
        row["any_hit"] = bool(passing)
        row["conjunctive_pass"] = any(
            all(vs.get(r, {}).get("correct") for r in roles) for vs in by_layer.values()
        )
        row["translated_pass"] = any(translated(vs) for vs in by_layer.values())
        row["subfamily"] = (item or {}).get("subfamily")
        unit = head[0] if head else None
        row["picks"] = [
            "gold"
            if vs.get(unit, {}).get("correct")
            else "distractor"
            if vs.get(unit, {}).get("kind") == "distractor"
            else vs.get(unit, {}).get("kind", "none")
            for _layer, vs in sorted(by_layer.items())
        ]
    return row


# ---------------------------------------------------------------------------------------------
# a family's own pinned forced-choice judge (MC_PROFILES; plan 0041, multihop_mt_v2)
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _UnitBank:
    """What the name gate of one (item, unit) needs from the bank."""

    full_forms: list[str]
    name_forms: list[str]
    kind: str = ""


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
            per[role] = _UnitBank(full, names, str(u.get("kind") or ""))
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
    entities: Mapping[str, list[list[str]]] | None = None,
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
    out = {
        "judged": True,
        "pick": letter,
        "kind": kind,
        "correct": kind == "correct" and ok,
        "quote": quote,
        "quote_ok": ok,
        "credit": credit,
    }
    if kind == "correct" and unit is not None:
        others = [o for i, o in enumerate(options[:-1]) if i != gold_idx]
        gold = [*unit.full_forms, *unit.name_forms, options[gold_idx]]
        rivals = (entities or {}).get(unit.kind, [])
        out = _void(out, list_gate(quote, lines, gold_forms=gold, others=others, rivals=rivals))
    return out


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
    entities = bank_entities(items)  # list_gate's rivals, over the whole bank
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
            results.get(key),
            gold_idx,
            opts,
            lines,
            ubank[item_id].get(role),
            fuzzy=fuzzy,
            entities=entities,
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
        "n_gold_pick_flat_list": sum(v.get("kind") == "flat_list" for v in verdicts),
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
                if v["kind"] in ("correct", "name_miss", "unverified", "flat_list")
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
