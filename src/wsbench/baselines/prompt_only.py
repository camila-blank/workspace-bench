"""Prompt-only floor: stock Qwen3.6-27B given the prompt text (no activation) is asked what the
model is thinking; its summaries are judged by each family's own instrument (``wsbench judge``)
and ``freeze`` records the rates. See ``evals/baselines/README.md``."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from wsbench.registry import REPO_ROOT
from wsbench.results import read_results

FROZEN = REPO_ROOT / "evals" / "baselines" / "prompt_only.json"
# summary-mode floors (plan 0024): the same prompt-only readouts judged with aggregate=summary
FROZEN_SUMMARY = REPO_ROOT / "evals" / "baselines" / "prompt_only_summary.json"
# summarizer settings that do not change the instrument (throughput only)
_SUMMARIZER_IGNORED = ("concurrency", "timeout")
# Families where the prompt states the answer outright, so the stock model's summary names it
# every time (multi-concept DM scored 1.0): not a floor, never frozen or shown (Agam, 2026-09-16).
EXCLUDED: dict[str, str] = {
    "multi_concept_directed_modulation": "the prompt dictates the concepts; prompt-only is 1.0",
    "directed_modulation": "the instruction names the concept and, since plan 0028, narrating it "
    "counts: prompt-only is 1.0 (2026-10-29, native and summary)",
    "hallucination": "an invention rate, not a bar a lens clears",
    "jlens_concept_pr": "precision against the J-lens top-10, not a bar a lens clears",
}
SOURCE_KEYS = (
    "model",
    "base_model",
    "adapter",
    "prompt_kind",
    "prompt_template",
    "sampling",
    "layers",
)
# Families whose prompt text states or dictates the answer, so the rate is pinned high and
# bounds what the text gives away, not what a lens must clear. `report` marks these instead of
# drawing them as a bar; the comparison there is the family's own null (see the README).
SATURATED: dict[str, str] = {
    "role_bound_association": "the scene states who did what to whom",
    "relational_multihop": "the cloze states both hops",
    "brew_intermediates": "the rule table and the start colour are in the prompt",
    "typo": "the correction is recoverable from the misspelling",
}


def judge_models() -> dict[str, str]:
    from wsbench import registry

    registry.load_all()
    return {name: spec.judge.model for name, spec in registry.FAMILIES.items()}


def instrument_versions() -> dict[str, str]:
    from wsbench import registry

    registry.load_all()
    return {name: spec.judge.prompt_version for name, spec in registry.FAMILIES.items()}


def summarizer_key(config: dict[str, Any]) -> dict[str, Any] | None:
    """A result's digest settings minus the throughput-only keys: what a summary floor must share
    with the row it is drawn beside (plan 0024)."""
    summ = config.get("summarizer")
    if not isinstance(summ, dict):
        return None
    return {k: v for k, v in summ.items() if k not in _SUMMARIZER_IGNORED}


def default_dst(run_dir: Path) -> Path:
    """Where ``freeze`` writes ``run_dir`` by default: ``FROZEN_SUMMARY`` for a summary-mode run,
    else ``FROZEN``."""
    from wsbench import digest

    for p in sorted(run_dir.glob("*/results.json")):
        r = read_results(p.parent)
        if r.family in EXCLUDED or r.family in digest.NATIVE_ONLY:
            continue
        if r.config.get("aggregate") == "summary":
            return FROZEN_SUMMARY
    return FROZEN


def freeze(
    run_dir: Path,
    dst: Path | None = None,
    *,
    source: Path | None = None,
) -> dict[str, Any]:
    """Fold ``run_dir/<family>/results.json`` (each family's judge run on the prompt-only
    readouts) into ``dst`` as ``{family: entry}``, stamped with the judge model and prompt
    version the rate belongs to. ``source`` is the generation's ``run_config.json`` (model,
    template, sampling), copied under ``_source``. A subset run (``items`` / ``limit``) or a
    run with no value is refused (ValueError); families not in ``run_dir`` keep their entry.

    Plan 0024: the run's ``config.aggregate`` routes it. Native results go to ``FROZEN`` with
    the checks above; summary results go to ``FROZEN_SUMMARY`` (``dst`` overrides either), each
    entry also recording ``aggregate``, ``summarizer`` (:func:`summarizer_key`) and its
    ``instrument`` = the summary ``prompt_version`` as run. A ``run_dir`` mixing modes is
    refused."""
    versions = instrument_versions()
    frozen: dict[str, Any] = {}
    paths = sorted(run_dir.glob("*/results.json"))
    runs = {p: read_results(p.parent) for p in paths}
    # skip what is never frozen before the mode check: under the summary default a prompt-only
    # `run all=True` leaves the NATIVE_ONLY families native (plan 0024)
    runs = {p: r for p, r in runs.items() if r.family not in EXCLUDED}
    if any(r.config.get("aggregate") == "summary" for r in runs.values()):
        from wsbench import digest

        runs = {p: r for p, r in runs.items() if r.family not in digest.NATIVE_ONLY}
    paths = list(runs)
    modes = {r.config.get("aggregate") or "native" for r in runs.values()}
    if len(modes) > 1:
        raise ValueError(f"freeze: {run_dir} mixes aggregate modes {sorted(modes)}; freeze each")
    summary = modes == {"summary"}
    if dst is None:
        dst = default_dst(run_dir)
    # a summary run must not overwrite the native floors, nor a native run the summary ones
    other = FROZEN if summary else FROZEN_SUMMARY
    if Path(dst).resolve() == other.resolve():
        mode = "summary" if summary else "native"
        raise ValueError(f"freeze: {run_dir} is a {mode}-mode run; {dst} holds the other mode's")
    for path in paths:
        r = runs[path]
        if r.family in EXCLUDED:
            continue
        if r.config.get("items") or r.config.get("limit"):
            raise ValueError(f"freeze: {path} is a subset run, not a baseline")
        if r.value is None:
            raise ValueError(f"freeze: {path} has no value")
        if summary:
            # the instrument is the summary prompt_version as run; a summary row draws this
            # floor only when its own prompt_version and summarizer match (summary_floor_for)
            pv = str(r.config.get("prompt_version") or "")
            base = versions.get(r.family)
            if "+digest-" not in pv or (base is not None and not pv.startswith(f"{base}+")):
                raise ValueError(
                    f"freeze: {path} was judged with prompt_version {pv!r}; the family's "
                    f"summary instrument starts with {base!r}+…+digest-; re-judge first"
                )
        elif versions.get(r.family) != r.config.get("prompt_version"):
            raise ValueError(
                f"freeze: {path} was judged with prompt_version "
                f"{r.config.get('prompt_version')!r}, the family's instrument is "
                f"{versions.get(r.family)!r}; re-judge first"
            )
        frozen[r.family] = {
            "rate": r.value,
            "saturated": r.family in SATURATED,
            "ci95": list(r.ci95) if r.ci95 else None,
            "metric": r.metric,
            "higher_is_better": r.higher_is_better,
            "n_items": r.n_items,
            "n_items_decided": r.extras.get("n_items_decided"),
            "counts": {
                k: r.counts.get(k)
                for k in (
                    "n_expected_cells",
                    "n_missing_cells",
                    "n_unjudged_cells",
                    "n_empty_cells",
                )
            },
            "judge_model": r.config.get("judge_model"),
            "instrument": r.config.get("prompt_version"),
            "layers_judged": r.config.get("layers_judged") or r.config.get("layers"),
            "complete": r.complete,
            **({"aggregate": "summary", "summarizer": summarizer_key(r.config)} if summary else {}),
            "written": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    if not frozen:
        raise ValueError(f"freeze: no <family>/results.json under {run_dir}")
    merged = json.loads(dst.read_text(encoding="utf-8")) if dst.exists() else {}
    if source is not None:
        cfg = json.loads(Path(source).read_text(encoding="utf-8"))
        merged["_source"] = {k: cfg.get(k) for k in SOURCE_KEYS if k in cfg}
    merged.update(frozen)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(merged, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return merged


def floors(dst: Path = FROZEN) -> dict[str, dict[str, Any]]:
    """Frozen prompt-only entries whose ``instrument`` matches the family's current judge
    prompt version AND whose ``judge_model`` is the family's judge (a regex entry or one with
    no stamp passes); stale entries are dropped, so a judge change needs a re-measured floor."""
    if not dst.exists():
        return {}
    versions = instrument_versions()
    models = judge_models()
    frozen = json.loads(dst.read_text(encoding="utf-8"))
    return {
        fam: e
        for fam, e in frozen.items()
        if not fam.startswith("_")
        and fam not in EXCLUDED
        and versions.get(fam) is not None
        and e.get("instrument") == versions.get(fam)
        and e.get("judge_model") in (None, "regex", models.get(fam))
    }


def summary_floors(dst: Path = FROZEN_SUMMARY) -> dict[str, dict[str, Any]]:
    """The frozen summary-mode prompt-only entries (plan 0024). Which row an entry is drawn beside
    is decided per row by :func:`summary_floor_applies`, not here: a summary instrument is the
    prompt_version and digest settings the row actually ran with."""
    if not dst.exists():
        return {}
    frozen = json.loads(dst.read_text(encoding="utf-8"))
    return {
        fam: e
        for fam, e in frozen.items()
        if not fam.startswith("_") and fam not in EXCLUDED and isinstance(e, dict)
    }


def summary_floor_applies(entry: dict[str, Any], config: dict[str, Any]) -> bool:
    """A summary floor is drawn beside a summary row only when the row ran the floor's
    instrument: the same ``prompt_version``, the same digest settings (:func:`summarizer_key`)
    and the same judge model (plan 0024 Rev 1 #5)."""
    return (
        config.get("aggregate") == "summary"
        and entry.get("instrument") == config.get("prompt_version")
        and entry.get("summarizer") == summarizer_key(config)
        and entry.get("judge_model") == config.get("judge_model")
    )
