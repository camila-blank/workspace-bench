"""conjunctive_association: one MC per item over every read cell (comp-v2, plan 0049).

Native mode judges the whole (layers x positions) blob in one call; summary mode (the default)
judges one digest of every cell. Either way a gold pick passes only when (C2.2) its quote
verifies inside the readouts (``digest.quote_ok``) AND inside ONE statement
(``digest.statement_lines``: one bullet of one free-text sample, or one token list), and (C2.3)
a quote-only re-judge (schema ``quote_mc``) picks the gold again from the quote alone. A pick of
the gold's role-swapped twin (bank v2) is ``inverted``, a fail.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from wsbench import digest, reads
from wsbench.cache import Cache
from wsbench.family import fail, render_bag
from wsbench.llm import Spend
from wsbench.mc import classify, options_with_cannot, seeded_shuffle
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
from .prompts import COMP_SEED, DIGEST_TASK_NOTE, PROMPT_VERSION, SCHEMA, SYSTEM, SYSTEM_SUMMARY

FAMILY = "conjunctive_association"
DEFAULT_CHAR_CAP = 200000  # the full-blob instrument of record (the source default 24000 is not)

# copied from the source repo's latent_eval/score_lens_readouts.py L26-32: applied to every
# sample, prose included (so AO text loses its underscores and BPE markers).
BPE_JUNK = re.compile(r"[ĠĊ▁]")  # tokens arrive decoded (no-op on them); prose keeps the rule

Opts = tuple[list[str], int, int | None]


def norm_token(t: str) -> str:
    t = BPE_JUNK.sub(" ", t)
    return t.replace("_", " ").strip()


def build_options(it: dict) -> Opts:
    """(shown incl. CANNOT, gold 1-based position, contrast 1-based position or None)."""
    opts = list(dict.fromkeys(it["mc_options"]))  # dedupe, preserve order
    if it["gold_label"] not in opts:
        raise ValueError(f"{it['id']}: gold_label not in mc_options")
    shown = options_with_cannot(seeded_shuffle(opts, f"{COMP_SEED}:{it['id']}"))
    contrast = it.get("contrast_label")
    contrast_pos = shown.index(contrast) + 1 if contrast in shown else None
    return shown, shown.index(it["gold_label"]) + 1, contrast_pos


def inverted_position(it: dict, shown: list[str]) -> int | None:
    """1-based position of the gold's role-swapped twin (``inverted_label``, bank v2) in
    ``shown``; None when the item has no twin (q06, q03's "himself" cells, every v1 item)."""
    inv = it.get("inverted_label")
    return shown.index(inv) + 1 if inv and inv in shown else None


def build_bags(cells: list[Cell]) -> dict[tuple[str, int], str]:
    """``{(id, layer): text}``: every row's samples (or tokens) ``norm_token``'d and joined with
    ``" | "`` in **file order** (as the source does), rows joined the same way."""
    bags: dict[tuple[str, int], str] = {}
    for c in cells:
        parts = c.samples if c.samples is not None else (c.tokens or ())
        text = " | ".join(norm_token(s) for s in parts)
        k = (c.id, c.layer)
        prev = bags.get(k, "")
        bags[k] = (prev + " | " + text) if prev else text
    return bags


def _printable(blob: str, char_cap: int) -> str:
    return "".join(ch for ch in blob[:char_cap] if ch.isprintable() or ch in "\n\t ")


def build_blob(texts: dict[int, str], layers: list[int], char_cap: int) -> str:
    blob = ""
    for layer in layers:
        blob += f"\n[L{layer}] " + texts[layer]
    return _printable(blob, char_cap)


def build_token_blob(cells: list[Cell], char_cap: int) -> str:
    """Token files (C4, plan 0032 §2.3): one ``[L<l> pos <p>]`` header per cell, each followed
    by its own raw ranked list (``family.render_bag``, best first, no scores), so every list
    stays a separate sample. Blank lists are left out."""
    out = ""
    for c in sorted(cells, key=lambda c: (c.layer, c.pos)):
        raw = list(c.tokens or ())
        keep = [i for i, t in enumerate(raw) if t.strip()]
        if not keep:
            continue
        toks = [raw[i].strip() for i in keep]
        sc = None
        if c.scores and len(c.scores) == len(raw):
            sc = [float(c.scores[i]) for i in keep]
        out += f"\n[L{c.layer} pos {c.pos}] " + render_bag(toks, sc)
    return _printable(out, char_cap)


def _excluded(bags: dict[tuple[str, int], str], layers: list[int], rep: LoadReport) -> list[str]:
    """Sidecar-less (report-only) files keep the family's denominator convention: an item missing
    a layer the file has is excluded and warned. A file with a sidecar defers to ``reads.load``
    (guard ii: exit 2, or a partial item judged on the cells it has under ``allow_missing``)."""
    if rep.reads_checked != "no_sidecar":
        return []
    by_item: dict[str, set[int]] = defaultdict(set)
    for iid, layer in bags:
        by_item[iid].add(layer)
    out = sorted(iid for iid, have in by_item.items() if any(L not in have for L in layers))
    if out:
        print(
            f"WARNING: {len(out)} items missing at least one layer are EXCLUDED from the "
            f"denominator (e.g. {out[:3]}) — arms with different coverage are not comparable."
        )
    return out


N_DRAWS = 3  # F9 (audit 2026-10-31): every judged item is asked N_DRAWS times; majority wins


def n_draws(args: JudgeArgs) -> int:
    """``opts=draws=N`` (an odd N >= 1; default :data:`N_DRAWS`)."""
    raw = args.extra.get("draws", str(N_DRAWS))
    try:
        n = int(raw)
    except ValueError:
        fail(f"opts=draws must be an odd integer >= 1 (got {raw!r})")
    if n < 1 or n % 2 == 0:
        fail(f"opts=draws must be an odd integer >= 1 (got {n})")
    return n


def draws_stamp(n: int) -> str:
    """The stamp suffix of a non-default draw count (``+draws1``): a different instrument,
    never matched against the default's floors or ATI entries."""
    return "" if n == N_DRAWS else f"+draws{n}"


def draw_key(key: str, k: int) -> str:
    """Draw ``k``'s cache key (draw 0 keeps the single-draw key)."""
    return key if k == 0 else f"{key}#d{k}"


def majority(picks: Sequence[str]) -> str:
    """The pick more than half of the draws share; ``api_fail`` (undecided) when none does and a
    draw failed, else ``split`` (a fail). A failed draw never votes, but counts in the half."""
    live = [p for p in picks if p != "api_fail"]
    if live:
        top, c = Counter(live).most_common(1)[0]
        if 2 * c > len(picks):
            return top
    return "api_fail" if len(live) < len(picks) else "split"


class _Gate:
    """C2.2 + C2.3 for one run: the quote condition, the statement condition and the quote-only
    re-judge, shared by native and summary mode, applied to every draw; then the majority over
    an item's draws (:func:`combine`, F9). Each draw is one full verdict: its own MC pick and,
    for a gold or inverted pick whose quote verifies, its own quote-only re-judge. Nothing is
    pieced across draws: a pass needs a majority of draws that each passed every gate."""

    def __init__(self, *, tokens: bool) -> None:
        self.tokens = tokens
        self.n_gold_raw = 0
        self.n_unverified_bullet = 0
        self.pending: list[Call] = []
        self.checks: dict[str, object] = {}
        self.checks_draws: dict[str, list[object]] = {}

    def verdict(
        self,
        it: dict,
        opts: Opts,
        choice: object,
        quote: str,
        lines: list[digest.Line],
        cells: list[Cell],
        *,
        system: str,
        draw: int = 0,
    ) -> dict:
        shown, gold_pos, contrast_pos = opts
        pick = classify(choice, gold_pos, len(shown), contrast_pos=contrast_pos)
        inv = inverted_position(it, shown)
        if pick not in ("invalid", "cannot_tell") and inv is not None and choice == inv:
            pick = "inverted"
        raw = pick
        in_lines = digest.quote_ok(quote, lines)
        in_statement = in_lines and digest.quote_ok(quote, digest.statement_lines(lines, cells))
        bullet = False
        if pick == "gold" and not in_statement:
            pick = "gold_unverified"
            bullet = in_lines
        if pick in ("gold", "inverted") and in_statement:
            q = prompts.render_question(shown)
            self.pending.append(
                Call(
                    draw_key(f"{it['id']}|quote", draw),
                    system,
                    prompts.render_quote_user(quote, q, tokens=self.tokens),
                    {"id": it["id"], "draw": draw, "gold_pos": gold_pos, "inverted_pos": inv},
                )
            )
        return {
            "choice": choice,
            "pick": pick,
            "raw_pick": raw,
            "unverified_bullet": bullet,
            "quote": quote,
            "quote_ok": in_statement,
        }

    def run(
        self, args: JudgeArgs, pv: str, cache: Cache, spend: Spend, pre: Preflighter
    ) -> dict[str, dict | None]:
        if not self.pending:
            return {}
        return run_calls(
            self.pending,
            schema=prompts.QUOTE_SCHEMA,
            judge=args.judge,
            prompt_version=pv,
            cache=cache,
            spend=spend,
            concurrency=args.concurrency,
            rpm=args.rpm,
            dry_run=args.dry_run,
            preflight=pre.for_judge(args.judge),
        )

    def apply(self, draws: dict[str, list[dict]], results: dict[str, dict | None]) -> None:
        """Fold the re-judge into each draw: a gold pick stays gold only when the quote alone
        re-judges to the gold; to the twin -> ``inverted``; anything else -> ``gold_partial``;
        a failed call -> ``api_fail`` (``failed_stage = "quote_check"``). Each re-judged draw
        records its answer as ``quote_check`` (None = a failed call); ``checks_draws[id]`` lists
        them in draw order."""
        for c in self.pending:
            iid, k = c.meta["id"], c.meta["draw"]
            d = draws[iid][k]
            r = results.get(c.key)
            if r is None:
                d["quote_check"] = None
                self.checks_draws.setdefault(iid, []).append(None)
                if d["pick"] == "gold":
                    d.update({"pick": "api_fail", "failed_stage": "quote_check"})
                continue
            ch = choice_of(r)
            d["quote_check"] = ch
            self.checks_draws.setdefault(iid, []).append(ch)
            if d["pick"] != "gold" or ch == c.meta["gold_pos"]:
                continue  # an inverted pick stays inverted whatever the quote re-judges to
            inv = c.meta["inverted_pos"]
            d["pick"] = "inverted" if inv is not None and ch == inv else "gold_partial"

    def combine(self, iid: str, ds: list[dict]) -> dict:
        """An item's row fields from its draws: ``pick`` = :func:`majority` of the draw picks,
        the shown choice / quote (and ``checks[iid]``, its quote re-judge) from the first draw
        with that pick, ``draw_picks`` in order.
        ``n_gold_raw`` / ``n_unverified_bullet`` count items (a majority of raw gold picks; a
        ``gold_unverified`` item whose quote lay in a line but crossed a bullet)."""
        pick = majority([d["pick"] for d in ds])
        rep = next((d for d in ds if d["pick"] == pick), ds[0])
        if "quote_check" in rep:
            self.checks[iid] = rep["quote_check"]
        self.n_gold_raw += majority([d.get("raw_pick", d["pick"]) for d in ds]) == "gold"
        self.n_unverified_bullet += pick == "gold_unverified" and bool(rep.get("unverified_bullet"))
        out = {
            "choice": rep.get("choice"),
            "pick": pick,
            "correct": pick == "gold",
            "quote": rep.get("quote", ""),
            "quote_ok": bool(rep.get("quote_ok")),
            "draw_picks": [d["pick"] for d in ds],
            "n_draws": len(ds),
        }
        if pick == "api_fail":
            stage = next((d["failed_stage"] for d in ds if d.get("failed_stage")), None)
            if stage is not None:
                out["failed_stage"] = stage
        return out

    def extras(self) -> dict:
        return {
            "n_gold_raw": self.n_gold_raw,
            "n_unverified_bullet": self.n_unverified_bullet,
            "quote_checks": self.checks,
            "quote_checks_draws": self.checks_draws,
        }


_FAILED_DRAW = {"choice": None, "pick": "api_fail", "quote": "", "quote_ok": False}


def _char_cap(args: JudgeArgs) -> int:
    try:
        char_cap = int(args.extra.get("char_cap", DEFAULT_CHAR_CAP))
    except ValueError:
        fail(f"opts=char_cap must be an integer (got {args.extra['char_cap']!r})")
    if char_cap < 1:
        fail(f"opts=char_cap must be >= 1 (got {char_cap})")
    return char_cap


def run(args: JudgeArgs) -> FamilyResult:
    char_cap = _char_cap(args)
    bank = load_bank(FAMILY)
    scope = item_scope(bank, args)
    cells, rep = reads.load(args, FAMILY, ids=[it["id"] for it in scope])
    bags = build_bags(cells)
    excluded = set(_excluded(bags, rep.layers, rep))
    items = [it for it in scope if it["id"] not in excluded]
    kept_bags = {k: v for k, v in bags.items() if k[0] not in excluded}
    by_cells: dict[str, list[Cell]] = defaultdict(list)
    for c in cells:
        by_cells[c.id].append(c)
    options = {it["id"]: build_options(it) for it in items}
    chance = score.mean_chance([o[0] for o in options.values()])
    if digest.summary_mode(args):
        return _run_summary(
            args,
            scope,
            items,
            cells,
            rep,
            by_cells=by_cells,
            options=options,
            kept_bags=kept_bags,
            n_excluded=len(excluded),
            chance=chance,
        )
    return _run_native(
        args,
        scope,
        items,
        cells,
        rep,
        by_cells=by_cells,
        options=options,
        kept_bags=kept_bags,
        n_excluded=len(excluded),
        chance=chance,
        char_cap=char_cap,
    )


def _run_native(
    args: JudgeArgs,
    scope: list[dict],
    items: list[dict],
    cells: list[Cell],
    rep: LoadReport,
    *,
    by_cells: dict[str, list[Cell]],
    options: dict[str, Opts],
    kept_bags: dict[tuple[str, int], str],
    n_excluded: int,
    chance: float,
    char_cap: int,
) -> FamilyResult:
    """Native mode: one call per item over the blob of every read cell. Prose: one ``[L<l>]``
    bag per layer (``build_bags``); tokens: one ``[L<l> pos <p>]`` raw list per cell, with the
    shared token-list rule (``mc.token_note`` of the spec) above the readouts."""
    tokens = rep.kind == "tokens"
    gate = _Gate(tokens=tokens)
    n = n_draws(args)
    spend = Spend()
    pre = Preflighter(args.dry_run)
    note = digest.token_note_of(FAMILY)
    layers_of: dict[str, list[int]] = defaultdict(list)
    for iid, layer in sorted(kept_bags):
        layers_of[iid].append(layer)
    with Cache(args.out / "cells.jsonl") as cache:
        calls = []
        for it in items:
            iid = it["id"]
            have = layers_of.get(iid, [])
            if not any(kept_bags[(iid, L)].strip() for L in have):
                continue  # no readouts (rowless fail) or every bag empty (n_empty_cells)
            if tokens:
                blob = build_token_blob(by_cells[iid], char_cap)
            else:
                blob = build_blob({L: kept_bags[(iid, L)] for L in have}, have, char_cap)
            q = prompts.render_question(options[iid][0])
            user = (
                prompts.render_user_tokens(note, blob, q)
                if tokens
                else prompts.render_user(blob, q)
            )
            calls += [
                Call(
                    draw_key(iid, k),
                    SYSTEM,
                    user,
                    {
                        "id": iid,
                        "draw": k,
                        "gold_pos": options[iid][1],
                        "contrast_pos": options[iid][2],
                        "n_shown": len(options[iid][0]),
                    },
                )
                for k in range(n)
            ]
        results = (
            run_calls(
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
            )
            if calls
            else {}
        )
        by_it = {it["id"]: it for it in items}
        draws: dict[str, list[dict]] = defaultdict(list)
        for c in calls:
            r = results.get(c.key)
            m = c.meta
            draws[m["id"]].append(
                dict(_FAILED_DRAW)
                if r is None
                else gate.verdict(
                    by_it[m["id"]],
                    options[m["id"]],
                    choice_of(r),
                    quote_of(r),
                    digest.native_lines(by_cells[m["id"]]),
                    by_cells[m["id"]],
                    system=SYSTEM,
                    draw=m["draw"],
                )
            )
        gate.apply(draws, gate.run(args, PROMPT_VERSION, cache, spend, pre))
        rows: list[dict] = [
            {
                "id": iid,
                "gold_pos": options[iid][1],
                "contrast_pos": options[iid][2],
                **gate.combine(iid, ds),
            }
            for iid, ds in draws.items()
        ]
    # a non-empty bag of an item without a verdict (API failure, a failed quote re-judge) is
    # unjudged; an item whose bags are all empty is counted only in n_empty
    judged = {v["id"] for v in rows if v["pick"] != "api_fail"}
    n_unjudged = sum(1 for (iid, _l), v in kept_bags.items() if v.strip() and iid not in judged)
    counts = base_counts(
        n_expected=len(kept_bags),
        n_unjudged=n_unjudged,
        n_empty=sum(1 for v in kept_bags.values() if not v.strip()),
        skipped_rows=sum(rep.skipped.values()),
        spend=spend,
    )
    return with_readout_count(
        score.score(
            args,
            items,
            rows,
            counts=counts,
            config=base_config(args, PROMPT_VERSION + draws_stamp(n), char_cap=char_cap, draws=n),
            n_items_excluded=n_excluded,
            chance=chance,
            gate=gate.extras(),
        ),
        scope,
        cells,
    )


def _run_summary(
    args: JudgeArgs,
    scope: list[dict],
    items: list[dict],
    cells: list[Cell],
    rep: LoadReport,
    *,
    by_cells: dict[str, list[Cell]],
    options: dict[str, Opts],
    kept_bags: dict[tuple[str, int], str],
    n_excluded: int,
    chance: float,
) -> FamilyResult:
    """Summary mode (plan 0020; the default): every cell of a kept item (every layer and read
    position) -> one digest -> the one MC, then the C2 gate on the digest's evidence lines. A
    sidecar-less file keeps the layer exclusion (native rule); an item with no readouts stays a
    rowless fail; an unsummarized item is a fail counted in ``n_unjudged_cells`` (items)."""
    pv = digest.prompt_version(PROMPT_VERSION, args, FAMILY)
    kept = [it["id"] for it in items if by_cells.get(it["id"])]
    by_it = {it["id"]: it for it in items}
    gate = _Gate(tokens=rep.kind == "tokens")
    n = n_draws(args)
    spend = Spend()
    pre = Preflighter(args.dry_run)
    with Cache(args.out / "cells.jsonl") as cache:
        col = digest.collapse(
            FAMILY,
            {iid: by_cells[iid] for iid in kept},
            args=args,
            cache=cache,
            spend=spend,
            preflight=pre.for_judge(digest.summarizer_judge(args, FAMILY)),
            task_note=DIGEST_TASK_NOTE,
            expect_all=[c for iid in kept for c in by_cells[iid]],
        )
        source_kind = digest.as_prose(rep)
        note = digest.summary_note(args, source_kind, family=FAMILY)
        judged = [iid for iid in kept if iid not in col.unsummarized and iid not in col.empty]
        calls = [
            Call(
                draw_key(f"{iid}|L{digest.SUMMARY_LAYER}", k),
                SYSTEM_SUMMARY,
                prompts.render_user_summary(
                    note,
                    (col.cells[iid].samples or ("",))[0],
                    prompts.render_question(options[iid][0]),
                ),
                {
                    "id": iid,
                    "draw": k,
                    "gold_pos": options[iid][1],
                    "contrast_pos": options[iid][2],
                    "n_shown": len(options[iid][0]),
                },
            )
            for iid in judged
            for k in range(n)
        ]
        results = (
            run_calls(
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
            )
            if calls
            else {}
        )
        draws: dict[str, list[dict]] = defaultdict(list)
        for c in calls:
            iid, r = c.meta["id"], results.get(c.key)
            draws[iid].append(
                dict(_FAILED_DRAW)
                if r is None
                else gate.verdict(
                    by_it[iid],
                    options[iid],
                    choice_of(r),
                    quote_of(r),
                    list(col.evidence.get(iid, ())),
                    by_cells[iid],
                    system=SYSTEM_SUMMARY,
                    draw=c.meta["draw"],
                )
            )
        gate.apply(draws, gate.run(args, pv, cache, spend, pre))
        rows: list[dict] = []
        n_api_failed = 0
        for iid in kept:
            _shown, gold_pos, contrast_pos = options[iid]
            row: dict = {
                "id": iid,
                "choice": None,
                "gold_pos": gold_pos,
                "contrast_pos": contrast_pos,
            }
            if iid in col.unsummarized or iid in col.empty:
                # not judged: a fail through the empty path, or an unjudged unit
                pick = "empty" if iid in col.empty else "unsummarized"
                row.update({"pick": pick, "correct": False, "quote": "", "quote_ok": False})
            else:
                row.update(gate.combine(iid, draws[iid]))
                n_api_failed += row["pick"] == "api_fail"
            row.update(col.row(iid))
            rows.append(row)
    counts = base_counts(
        n_expected=len(kept_bags),
        n_unjudged=len(col.unsummarized) + n_api_failed,
        n_empty=len(col.empty),
        skipped_rows=sum(rep.skipped.values()),
        spend=spend,
    )
    stamp = digest.prompt_version(PROMPT_VERSION + draws_stamp(n), args, FAMILY)
    config = base_config(
        args, stamp, char_cap=None, draws=n, **digest.config(args, source_kind, FAMILY)
    )
    return with_readout_count(
        score.score(
            args,
            items,
            rows,
            counts=counts,
            config=config,
            n_items_excluded=n_excluded,
            summary=col.extras(),
            pinned=digest.pinned(args, FAMILY),
            chance=chance,
            gate=gate.extras(),
        ),
        scope,
        cells,
    )
