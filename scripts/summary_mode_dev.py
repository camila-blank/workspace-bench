"""Plan 0018 D2: the summary-mode dev slice. ``ids`` writes the slice (items 0-19 in bank order of
every hooked family with >= 60 items in its readouts); ``audit`` scores the fidelity checks of one
or more summary-mode out dirs (no score is a selection input).

    python scripts/summary_mode_dev.py ids roots=DIR1,DIR2 out=dev_ids.json
    python scripts/summary_mode_dev.py audit runs=LABEL:OUTDIR,... roots=DIR1,DIR2 \
        [passivity=True] [out=PREFIX]

``roots`` are readouts dirs searched in order for ``<family>.jsonl`` (legacy names too)."""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from wsbench import digest, llm, registry
from wsbench.banks import load_bank
from wsbench.family import span_in
from wsbench.readouts import load_readouts

MIN_ITEMS = 60
N_DEV = 20
STOP = frozenset(
    [
        "the",
        "and",
        "that",
        "this",
        "with",
        "from",
        "have",
        "for",
        "are",
        "was",
        "were",
        "not",
        "but",
        "you",
        "your",
        "they",
        "them",
        "their",
        "there",
        "what",
        "which",
        "who",
        "whom",
        "will",
        "would",
        "could",
        "should",
        "about",
        "into",
        "over",
        "than",
        "then",
        "also",
        "just",
        "only",
        "very",
        "been",
        "being",
        "has",
        "had",
        "its",
        "it's",
        "can",
        "may",
        "might",
        "must",
        "our",
        "out",
        "all",
        "any",
        "some",
        "more",
        "most",
        "such",
        "each",
        "other",
        "these",
        "those",
        "when",
        "where",
        "while",
        "because",
        "answer",
        "model",
        "text",
        "word",
        "words",
    ]
)
AUDIT_MODEL = "claude-opus-5-5"
AUDIT_SYSTEM = """You audit a note-taker's digest of interpretability-lens readouts. You see each \
digest item (a statement and the verbatim evidence span it cites) and the readout sample that \
span comes from. The note-taker was told to be a passive recorder. Flag an item if its statement \
does any of: interprets beyond what the readouts say; infers what the passage or task is; answers \
a question; translates; generalises a specific term into a category; sharpens a category into a \
specific member; combines separate readouts into one claim; changes who wants/believes/does \
what, or whether a claim is asserted, denied or merely mentioned. Otherwise it is faithful."""
AUDIT_SCHEMA = llm.schema_block(
    "passivity",
    {
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "kind"],
                "properties": {"index": {"type": "integer"}, "kind": {"type": "string"}},
            },
        }
    },
    ["violations"],
)


def hooked() -> list[str]:
    registry.load_all()
    return sorted(f for f in registry.FAMILIES if f not in digest.NATIVE_ONLY)


def readouts_path(fam: str, roots: list[Path]) -> Path | None:
    for root in roots:
        for name in (fam, *registry.legacy_names(fam)):
            p = root / f"{name}.jsonl"
            if p.exists():
                return p
    return None


def _bank_ids(fam: str) -> list[str]:
    spec = registry.FAMILIES[fam]
    path = registry.REPO_ROOT / spec.bank
    try:
        _h, items = load_bank(path)
    except Exception:
        items = json.loads(path.read_text())
        items = items["items"] if isinstance(items, dict) else items
    return [str(it.get("id", it.get("name"))) for it in items]


def dev_ids(roots: list[Path]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for fam in hooked():
        p = readouts_path(fam, roots)
        if p is None:
            continue
        cells, _rep = load_readouts(p)
        present = {c.id for c in cells}
        order = [i for i in _bank_ids(fam) if i in present]
        if len(order) >= MIN_ITEMS:
            out[fam] = order[:N_DEV]
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{3,}", text.casefold()) if w not in STOP}


def coverage(cells: list, evidence: list[str]) -> float | None:
    """Share of recurring word types (>= 3 samples spanning >= 2 layers) present in the
    evidence samples."""
    n_samples: Counter[str] = Counter()
    layers: dict[str, set[int]] = defaultdict(set)
    for c in cells:
        samples = c.samples if c.samples is not None else [" ".join(c.tokens or ())]
        for s in samples:
            text = digest.decode_bpe(s) if c.tokens is not None else s
            for w in _words(text):
                n_samples[w] += 1
                layers[w].add(c.layer)
    recurring = {w for w, n in n_samples.items() if n >= 3 and len(layers[w]) >= 2}
    if not recurring:
        return None
    ev = set().union(*(_words(e) for e in evidence)) if evidence else set()
    return len(recurring & ev) / len(recurring)


def _targets(row: dict) -> list[str]:
    t = row.get("targets")
    return [str(x) for x in t] if isinstance(t, list) else []


def selection_bias(cells: list, row: dict) -> float | None:
    """(gold-containing share of evidence samples) / (gold-containing share of bundle samples),
    for rows that carry ``targets`` (the bank families); None otherwise."""
    targets = _targets(row)
    evidence = row.get("summary_evidence") or []
    if not targets or not evidence:
        return None
    samples: list[str] = []
    for c in cells:
        if c.samples is not None:
            samples.extend(s for s in c.samples if s.strip())
        else:
            samples.append(" | ".join(digest.decode_bpe(t) for t in c.tokens or ()))

    def has(s: str) -> bool:
        return any(span_in(t, s) for t in targets)

    base = sum(map(has, samples)) / len(samples) if samples else 0.0
    if base == 0:
        return None
    return (sum(map(has, evidence)) / len(evidence)) / base


def passivity(rows: list[dict]) -> tuple[int, int]:
    """(violating items, audited items) over the rows' digest statements."""
    prompts: list[tuple[str, str]] = []
    sizes: list[int] = []
    for r in rows:
        st = r.get("summary_statements") or []
        if not st:
            continue
        body = "\n".join(
            f"[{k}] statement: {s['statement']}\n    evidence: {s['evidence']}"
            for k, s in enumerate(st)
        )
        prompts.append((AUDIT_SYSTEM, "DIGEST ITEMS:\n" + body))
        sizes.append(len(st))
    bad = [0]

    def on_result(i: int, r: dict | None) -> None:
        if isinstance(r, dict):
            idx = {v.get("index") for v in r.get("violations", []) if isinstance(v, dict)}
            bad[0] += len([k for k in idx if isinstance(k, int) and 0 <= k < sizes[i]])

    if prompts:
        llm.stream_json(
            prompts,
            schema=AUDIT_SCHEMA,
            model=AUDIT_MODEL,
            reasoning={"anthropic_effort": "default"},
            on_result=on_result,
            concurrency=16,
            rpm=600,
        )
    return bad[0], sum(sizes)


def audit(runs: list[tuple[str, Path]], roots: list[Path], do_passivity: bool) -> list[dict]:
    out: list[dict[str, Any]] = []
    for label, root in runs:
        for res in sorted(root.glob("*/results.json")):
            fam = res.parent.name
            d = json.loads(res.read_text())
            if d["config"].get("aggregate") != "summary":
                continue
            rows = d.get("rows", [])
            ex = d["numbers"].get("extras", {}).get("summary", {}) or {}
            p = readouts_path(fam, roots)
            by_item: dict[str, list] = defaultdict(list)
            if p is not None:
                layers = d["config"].get("layers")
                cells, _rep = load_readouts(p, layers=layers)
                for c in cells:
                    by_item[c.id].append(c)
            covs = [
                cv
                for r in rows
                if (
                    cv := coverage(
                        by_item.get(r.get("id", ""), []), r.get("summary_evidence") or []
                    )
                )
                is not None
            ]
            bias = [
                b
                for r in rows
                if (b := selection_bias(by_item.get(r.get("id", ""), []), r)) is not None
            ]
            n_items = max(1, len(rows))
            kept, dropped = ex.get("n_evidence_kept", 0), ex.get("n_evidence_dropped", 0)
            rec: dict[str, Any] = {
                "run": label,
                "family": fam,
                "n": len(rows),
                "unsummarized_rate": ex.get("n_items_unsummarized", 0) / n_items,
                "zero_digest_rate": ex.get("n_digest_zero_nonempty", 0) / n_items,
                "evidence_drop_rate": dropped / max(1, kept + dropped),
                "truncated": ex.get("n_truncated_items", 0),
                "coverage": sum(covs) / len(covs) if covs else None,
                "selection_bias": sum(bias) / len(bias) if bias else None,
                "value": d["numbers"].get("value"),
            }
            if do_passivity:
                bad, total = passivity(rows)
                rec["passivity_violation_rate"] = bad / total if total else None
            out.append(rec)
    return out


def main(argv: list[str]) -> int:
    cmd, kw = argv[0], dict(a.split("=", 1) for a in argv[1:])
    roots = [Path(r) for r in kw["roots"].split(",")]
    if cmd == "ids":
        ids = dev_ids(roots)
        Path(kw["out"]).write_text(json.dumps(ids, indent=1))
        print({f: len(v) for f, v in ids.items()})
        return 0
    if cmd == "audit":
        runs = [(r.split(":", 1)[0], Path(r.split(":", 1)[1])) for r in kw["runs"].split(",")]
        recs = audit(runs, roots, kw.get("passivity", "False") == "True")
        for r in recs:
            print(json.dumps(r))
        if kw.get("out"):
            Path(kw["out"] + ".json").write_text(json.dumps(recs, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
