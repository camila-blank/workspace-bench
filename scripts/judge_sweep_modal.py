"""Judge sweep on Modal (plan 0059 §7): the OpenRouter side of ``scripts/judge_sweep.py``.

OpenRouter is only ever called from here (secret ``openrouter``), never from a dev box; so are
the roster's ``openai-wif`` models (OpenAI directly, secret ``openai-wif`` + the container's Modal
identity token; plan 0059 addendum A: the job runs with ``WSBENCH_OPENAI_ROUTE=wif``) and its
``gemini-vertex`` models (Vertex AI by workload identity federation on the container's Modal
identity token, no secret; addendum B: ``WSBENCH_GEMINI_ROUTE=vertex``; the identity pool trusts
only Modal environment ``camila-blank``). App
``wsb-judge-sweep``, volume ``wsb-judge-sweep`` (``/vol``):

    /vol/readouts/<name>/   readouts (+ .meta.json sidecars) of the swept method
    /vol/seeds/<name>/      the reference run's digest-path cache rows (layout as written)
    /vol/runs/<sweep>/<arm>/<model-slug>/<family>/   one ``wsbench run`` out dir per (arm, model)
    /vol/scratch/<sweep>/   preflight runs

Entry points (run with the project venv's ``modal`` and ``PYTHONPATH=src`` — the client imports
``judge_sweep.py``, which needs numpy and ``wsbench``; the driver loop runs on the client, in tmux):

    modal run scripts/judge_sweep_modal.py::upload --name s3d --readouts DIR --ref-dir R4 \\
        [--ref-dirs map.json] [--exclude-nocot]
    modal run scripts/judge_sweep_modal.py::preflight --sweep S --name s3d \\
        --ref-opts ref_opts.json [--models a,b|tier:cheap+mid] [--arms A,B]
    modal run scripts/judge_sweep_modal.py::sweep --sweep S --name s3d --ref-dir R4 \\
        --ref-opts ref_opts.json [--models ...] [--arms A,B] [--retry-errors]
    modal run scripts/judge_sweep_modal.py::pull --sweep S

``sweep`` drives the resume loop of ``judge_sweep.drive``: one container per (arm, model, job
group) per pass, each returning its pass record; the job state lives on the client under
``outputs/judge_sweep/<sweep>/jobs/``. ``pull`` copies the volume runs to
``outputs/judge_sweep/<sweep>/`` for ``judge_sweep.py analyze``.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import modal

HERE = Path(__file__).resolve().parent
LOCAL_REPO = HERE.parent
WSB = "/root/wsb"  # in the container: registry.REPO_ROOT = parents[2] of src/wsbench/registry.py
VOL = "/vol"
APP = "wsb-judge-sweep"
SCRIPTS = ("judge_sweep.py", "summary_mode_compare.py", "judge_swap.py")
DEPS = (
    "openai>=1.40",
    "anthropic>=1.0",
    "numpy>=1.26",
    "pydra-config>=0.0.17",
    "pronouncing>=0.2",
    "google-genai>=2.3",  # the gemini-vertex route (plan 0059 addendum B)
    "google-auth>=2.40",
)
COMMIT_EVERY = 60.0  # seconds between volume commits while a job runs

app = modal.App(APP)
vol = modal.Volume.from_name(APP, create_if_missing=True)
_ignore = ["**/__pycache__", "**/__pycache__/**"]
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(*DEPS)
    .env({"PYTHONPATH": f"{WSB}/src", "PYTHONUNBUFFERED": "1"})
    .add_local_dir(LOCAL_REPO / "src", f"{WSB}/src", ignore=_ignore)
    .add_local_dir(LOCAL_REPO / "evals", f"{WSB}/evals", ignore=_ignore)
)
for _s in SCRIPTS:
    image = image.add_local_file(HERE / _s, f"{WSB}/scripts/{_s}")
# both on every function: a job's route (its model's roster ``route``) picks which one it uses
# (gemini-vertex needs none: Modal's identity token is exchanged at Google STS)
SECRETS = [modal.Secret.from_name("openrouter"), modal.Secret.from_name("openai-wif")]


def _js() -> Any:
    """``scripts/judge_sweep.py`` (local checkout on the client, ``/root/wsb/scripts`` in the
    container)."""
    if "judge_sweep" in sys.modules:
        return sys.modules["judge_sweep"]
    path = HERE / "judge_sweep.py"
    if not path.exists():
        path = Path(WSB) / "scripts" / "judge_sweep.py"
    spec = importlib.util.spec_from_file_location("judge_sweep", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["judge_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- container side


def _committer(stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.wait(COMMIT_EVERY):
            try:
                vol.commit()
            except Exception as e:  # a failed periodic commit is retried next tick
                print(f"volume commit failed: {e!r}", flush=True)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def _container_env(name: str, model: str) -> dict[str, str]:
    """The container env plus the uploaded ``WSB_*`` path overrides (rewritten to volume paths
    at upload; ``WSB_NO_NOCOT`` is never set), with exactly ``model``'s route env
    (``WSBENCH_OPENAI_ROUTE=wif`` for ``openai-wif``, ``WSBENCH_GEMINI_ROUTE=vertex`` for
    ``gemini-vertex``; the roster baked into the image) and no other."""
    js = _js()
    env = dict(os.environ)
    f = Path(VOL) / "seeds" / name / "env.json"
    if f.exists():
        env.update(json.loads(f.read_text(encoding="utf-8")))
    return js.route_env(env, js.load_roster().get(model))


def _seed_job(js: Any, job: Any, out: Path, name: str, drop: list[str]) -> None:
    """Arm A (and A') dirs: seed each family's ``cells.jsonl`` from the uploaded seed if absent."""
    index = json.loads((Path(VOL) / "seeds" / name / "index.json").read_text(encoding="utf-8"))
    for f in job.families:
        rel = index.get(f)
        if rel is not None:
            js.seed_family_dir(out / f, Path(VOL) / "seeds" / name / rel / "cells.jsonl", drop)


@app.function(image=image, volumes={VOL: vol}, secrets=SECRETS, timeout=24 * 3600)
def run_job(job_json: dict, name: str, layers: list[int] | None, run_args: list[str]) -> dict:
    """One pass of one job group; returns its pass record (``judge_sweep.execute_pass``)."""
    js = _js()
    vol.reload()
    job = js.Job.from_json(job_json)
    if job.model.startswith("claude-"):
        raise SystemExit(f"{job.model}: Claude jobs run on the dev box (run --backend local)")
    out = Path(VOL) / "runs" / job.sweep / job.arm / job.slug
    if job.kind == "A":
        _seed_job(js, job, out, name, [])
    stop = threading.Event()
    _committer(stop)
    try:
        return dict(
            js.execute_pass(
                job,
                out_dir=out,
                readouts_root=Path(VOL) / "readouts" / name,
                env=_container_env(name, job.model),
                layers=layers,
                run_args=run_args,
                log=out / "logs" / f"{job.group_id}.log",
            )
        )
    finally:
        stop.set()
        vol.commit()


@app.function(image=image, volumes={VOL: vol}, secrets=SECRETS, timeout=6 * 3600)
def preflight_job(job_json: dict, name: str, layers: list[int] | None, run_args: list[str]) -> dict:
    """A preflight of ONE job group: the group's exact opts, effort and output cap, with
    ``items=`` = the group's probe items, into a scratch dir (Arm A: seeded from the same seed
    so ``summ_cache_only`` hits). Every family's schema and ``max_tokens`` is exercised."""
    js = _js()
    vol.reload()
    job = js.Job.from_json(job_json)
    out = Path(VOL) / "scratch" / job.sweep / job.arm / job.slug / job.group_id
    if job.kind == "A":
        _seed_job(js, job, out, name, [])
    try:
        rec = js.execute_pass(
            job,
            out_dir=out,
            readouts_root=Path(VOL) / "readouts" / name,
            env=_container_env(name, job.model),
            layers=layers,
            run_args=run_args,
            log=out / "preflight.log",
        )
    finally:
        vol.commit()
    out_rec = {"group": job.group_id, "arm": job.arm, "ok": js.pass_ok(rec), **rec}
    if not out_rec["ok"]:  # the reason, so preflight.json says why (not just "exit 3")
        text = (out / "preflight.log").read_text(encoding="utf-8", errors="replace")
        lines = text.rsplit("=== pass ", 1)[-1].splitlines()  # this attempt's section only
        out_rec["log_tail"] = [x[:400] for x in lines if "error" in x.lower()][-5:] or lines[-5:]
    return out_rec


@app.function(image=image, secrets=SECRETS, timeout=1800)
def overcontext(model: str, context: int, max_out: int) -> dict:
    """One deliberate over-context call (a filler prompt of ~1.1 x context): what OpenRouter (or,
    for an ``openai-wif`` model, OpenAI through the WIF client; for a ``gemini-vertex`` model,
    Vertex AI) returns, and whether the change-4 classifier maps it to ``TOO_LONG_JSON``."""
    import asyncio

    from wsbench import llm

    js = _js()
    for var in (llm.OPENAI_ROUTE_ENV, llm.GEMINI_ROUTE_ENV):  # a warm container may hold an
        os.environ.pop(var, None)  # earlier model's
    os.environ.update(js.route_env({}, js.load_roster().get(model)))
    wif = llm.route(model) == "openai"
    vertex = llm.route(model) == "gemini"
    filler = " the" * int(1.1 * context)
    raw: dict[str, Any] = {}

    async def go_vertex() -> None:
        c: Any = None
        try:
            from google.genai import types

            c = llm._vertex_client()
            r = await c.aio.models.generate_content(
                model=llm.gemini_model(model),
                contents=filler,
                config=types.GenerateContentConfig(max_output_tokens=16),
            )
            raw["status"] = 200
            raw["body"] = str(r)[:500]
        except Exception as e:  # recorded verbatim: this is what the probe is for
            raw["status"] = getattr(e, "code", None)
            raw["message"] = str(e)[:1000]
        finally:
            aclose = getattr(getattr(c, "aio", None), "aclose", None)
            if aclose is not None:
                await aclose()

    async def go() -> None:
        from openai import AsyncOpenAI

        from wsbench.baselines import ati_remote

        clients = ati_remote.Clients(js.WIF_ROUTE) if wif else None
        c: Any = None
        try:
            c = (  # inside the try: a flaky token exchange is recorded, never a dropped model
                await clients.get()
                if clients is not None
                else AsyncOpenAI(
                    base_url="https://openrouter.ai/api/v1", api_key=llm.api_key(model)
                )
            )
            r = await c.chat.completions.create(
                model=ati_remote.route_model(model, js.WIF_ROUTE) if wif else model,
                messages=[{"role": "user", "content": filler}],
                **({"max_completion_tokens": 16} if wif else {"max_tokens": 16}),
            )
            raw["status"] = 200
            raw["body"] = str(r)[:500]
        except Exception as e:  # recorded verbatim: this is what the probe is for
            raw["status"] = getattr(e, "status_code", None)
            raw["message"] = str(e)[:1000]
        finally:
            if c is not None:
                await c.close()

    asyncio.run(go_vertex() if vertex else go())
    raw["route"] = js.WIF_ROUTE if wif else js.GEMINI_ROUTE if vertex else "openrouter"
    os.environ[llm.MAX_OUT_ENV] = str(max_out)
    got: dict[str, Any] = {}
    spend = llm.Spend()
    llm.stream_json(
        [("Reply with JSON.", filler)],
        schema=llm.schema_block("probe", {"ok": {"type": "string"}}, ["ok"]),
        model=model,
        on_result=lambda _i, r: got.update(result=r),
        mark_refusals=True,
        spend=spend,
        attempts=1,
    )
    too_long = got.get("result") == llm.TOO_LONG_JSON
    return {
        "model": model,
        "raw": raw,
        "too_long_json": too_long,
        "failed": dict(spend.failed),
        "errors": spend.errors,
        # a 404 drops the model from Arm B; an overflow the classifier does not map is reported
        # (the digest's halving fallback would not engage) for a fix, not silently dropped
        "drop_from_arm_b": raw.get("status") == 404,
        "classifier_warning": None if too_long else "over-context not mapped to TOO_LONG_JSON",
    }


# ---------------------------------------------------------------- client side


def _plan(
    sweep: str,
    models: str,
    arms: str,
    ref_opts: str,
    exclude_nocot: bool,
    roster_path: str,
    families: str = "",
) -> tuple[Any, list[Any], list[str], dict[str, Any]]:
    js = _js()
    roster = js.load_roster(Path(roster_path))
    ids = (
        js._models_arg(models, roster, claude=False)
        if models
        else [m for m in roster if not js.is_claude(m)]
    )
    bad = [m for m in ids if js.is_claude(m)]
    if bad:
        raise SystemExit(
            f"Claude models run on the dev box (judge_sweep.py run --backend local): {bad}"
        )
    kc = js.kc_loaded_items()
    jobs, notes = js.plan_jobs(
        sweep=sweep,
        models=ids,
        draws=[],
        arms=[a for a in arms.split(",") if a],
        roster=roster,
        infos=js.registry_families(families),
        ref_opts=js._ref_opts(ref_opts),
        kc_items=js.nocot_free_ids(kc) if exclude_nocot else None,
    )
    return js, jobs, notes, roster


def _drop_ids(js: Any, exclude_nocot: bool) -> list[str]:
    return sorted(js.nocot_ids(js.kc_loaded_items())) if exclude_nocot else []


@app.local_entrypoint()
def upload(
    name: str,
    readouts: str,
    ref_dir: str,
    ref_dirs: str = "",
    exclude_nocot: bool = False,
) -> None:
    """Readouts (with sidecars) -> ``/vol/readouts/<name>/``; every ``cells.jsonl`` of the
    reference run dir -> ``/vol/seeds/<name>/`` (layout as written, incl. ``layers=each``
    ``<family>/<tag>/``), keeping every digest-path row in file order, failed rows included;
    plus ``index.json`` (family -> its seed dir) and ``env.json`` (``WSB_*`` path overrides
    rewritten to volume paths). ``--exclude-nocot`` strips the nocot rows from both."""
    js = _js()
    if os.environ.get("WSB_NO_NOCOT"):
        raise SystemExit("WSB_NO_NOCOT is set: unset it (it changes plan_sha vs the sidecars)")
    drop = _drop_ids(js, exclude_nocot)
    rd = js._ref_dirs(ref_dirs or None)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        for p in sorted(Path(readouts).iterdir()):
            if not p.is_file():
                continue
            dst = stage / "readouts" / p.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix == ".jsonl" and drop:
                lines = js.strip_readouts(js._read_lines(p), drop)
                dst.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
            else:
                dst.write_bytes(p.read_bytes())
        seeds = stage / "seeds"
        n_rows = 0
        for cells in sorted(Path(ref_dir).rglob("cells.jsonl")):
            rel = cells.relative_to(ref_dir)
            n_rows += js.write_seed(cells, seeds / rel, drop)
        index: dict[str, str] = {}
        for f in [i.name for i in js.registry_families()]:
            d = js.family_ref_dir(Path(ref_dir), f, rd)
            if not (d / "cells.jsonl").exists():
                continue
            try:
                index[f] = str(d.relative_to(ref_dir))
            except ValueError:  # a --ref-dirs dir outside the run dir: its own seed copy
                index[f] = f"_ref_dirs/{f}"
                n_rows += js.write_seed(d / "cells.jsonl", seeds / index[f] / "cells.jsonl", drop)
        (seeds / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
        env: dict[str, str] = {}
        for k, v in os.environ.items():
            if k.startswith("WSB_") and Path(v).is_absolute() and Path(v).is_file():
                dst = stage / "seeds" / "_env" / k / Path(v).name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(Path(v).read_bytes())
                env[k] = f"{VOL}/seeds/{name}/_env/{k}/{Path(v).name}"
            elif k.startswith("WSB_"):
                env[k] = v  # repo-relative values resolve against REPO_ROOT in the container
        (seeds / "env.json").write_text(json.dumps(env, indent=1), encoding="utf-8")
        with vol.batch_upload(force=True) as up:
            up.put_directory(str(stage / "readouts"), f"/readouts/{name}")
            up.put_directory(str(seeds), f"/seeds/{name}")
    print(f"uploaded readouts + {n_rows} seed rows ({len(index)} families) as {name!r}")


@app.local_entrypoint()
def preflight(
    sweep: str,
    name: str,
    ref_opts: str,
    models: str = "",
    arms: str = "A,B",
    readouts: str = "",
    layers: str = "",
    run_args: str = "",
    exclude_nocot: bool = False,
    roster: str = "",
    families: str = "",
) -> None:
    """Per OpenRouter model and arm: one preflight per JOB GROUP (same partition as
    ``run_job``) with ``items=`` = that group's first-item-with-readouts union, plus one
    over-context probe per model. Writes ``outputs/judge_sweep/<sweep>/preflight.json``
    (models that fail are dropped, reason kept; a 404 drops Arm B, an unmapped overflow only
    warns)."""
    js, jobs, notes, ros = _plan(
        sweep, models, arms, ref_opts, exclude_nocot, roster or str(_js().ROSTER_PATH), families
    )
    if not readouts:
        raise SystemExit("--readouts: the local readouts dir (probe items are picked from it)")
    drop = set(_drop_ids(js, exclude_nocot))
    probe = js.probe_items(Path(readouts), sorted({f for j in jobs for f in j.families}), drop)
    lay = [int(x) for x in layers.split(",")] if layers else None
    ra = js.check_run_args(run_args.split())
    pjobs = [js.preflight_job_of(j, probe) for j in jobs]
    recs = list(
        preflight_job.starmap([(j.to_json(), name, lay, ra) for j in pjobs], return_exceptions=True)
    )
    models_seen = sorted({j.model for j in jobs})
    oc = list(
        overcontext.starmap(
            [(m, int(ros[m]["context"]), js.output_cap(ros[m])) for m in models_seen],
            return_exceptions=True,
        )
    )
    report: dict[str, Any] = {"notes": notes, "models": {}}
    for j, r in zip(pjobs, recs, strict=True):
        d = report["models"].setdefault(j.model, {"groups": [], "dropped": None})
        if isinstance(r, BaseException):
            r = {"group": j.group_id, "arm": j.arm, "ok": False, "error": repr(r)[:500]}
        d["groups"].append(r)
        if not r.get("ok") and d["dropped"] is None:
            d["dropped"] = (
                f"preflight failed ({j.arm}/{j.group_id}): {r.get('outcomes') or r.get('error')}"
                f"{' — ' + ' | '.join(r['log_tail']) if r.get('log_tail') else ''}"
            )
    for m, r in zip(models_seen, oc, strict=True):
        d = report["models"][m]
        d["overcontext"] = r if not isinstance(r, BaseException) else {"error": repr(r)[:500]}
        if isinstance(r, BaseException) or r.get("drop_from_arm_b"):
            d["arm_b_exception"] = "over-context probe: 404 (no serving endpoint) or probe error"
        elif r.get("classifier_warning"):
            d["warning"] = r["classifier_warning"]
    out = js.OUT_ROOT / sweep / "preflight.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    for m, d in sorted(report["models"].items()):
        print(
            f"{m}: {'DROPPED ' + d['dropped'] if d['dropped'] else 'ok'}"
            f"{' | Arm B exception' if d.get('arm_b_exception') else ''}"
        )
    print(f"wrote {out}")


@app.local_entrypoint()
def sweep(
    sweep: str,
    name: str,
    ref_dir: str,
    ref_opts: str,
    models: str = "",
    arms: str = "A,B",
    ref_dirs: str = "",
    layers: str = "",
    run_args: str = "",
    retry_errors: bool = False,
    exclude_nocot: bool = False,
    roster: str = "",
    families: str = "",
) -> None:
    """The resume loop for the OpenRouter models (Arm A / Arm B), minus the models
    ``preflight.json`` dropped (and their Arm B jobs when it lists an Arm B exception)."""
    js, jobs, notes, _ros = _plan(
        sweep, models, arms, ref_opts, exclude_nocot, roster or str(_js().ROSTER_PATH), families
    )
    pf = js._read_json(js.OUT_ROOT / sweep / "preflight.json")
    if pf is None:
        raise SystemExit("run the preflight entrypoint first (preflight.json missing)")
    pm = pf.get("models", {})
    keep = []
    for j in jobs:
        d = pm.get(j.model)
        if d is None:
            raise SystemExit(f"{j.model}: not preflighted")
        if d.get("dropped") or (j.arm == "B" and d.get("arm_b_exception")):
            continue
        keep.append(j)
    for n in notes:
        print("note:", n)
    lay = [int(x) for x in layers.split(",")] if layers else None
    js.prelaunch_check(keep, Path(ref_dir), js._ref_dirs(ref_dirs or None), lay)
    ra = js.check_run_args(run_args.split())

    def execute(batch: list[Any]) -> list[Any]:
        return list(
            run_job.starmap([(j.to_json(), name, lay, ra) for j in batch], return_exceptions=True)
        )

    states = js.drive(
        js.split_rpm(keep),
        sweep_dir=js.OUT_ROOT / sweep,
        execute=execute,
        retry_errors=retry_errors,
    )
    for lab, (s, d) in sorted(states.items()):
        print(f"{s:9s} {lab}: {d}")


@app.local_entrypoint()
def pull(sweep: str, out: str = "") -> None:
    """``/vol/runs/<sweep>/`` -> ``outputs/judge_sweep/<sweep>/`` (results, caches, ledgers)."""
    js = _js()
    dest = Path(out) if out else js.OUT_ROOT / sweep
    n = 0
    for entry in vol.listdir(f"/runs/{sweep}", recursive=True):
        if entry.type != modal.volume.FileEntryType.FILE:
            continue
        rel = Path(entry.path.lstrip("/")).relative_to(f"runs/{sweep}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as fh:
            for chunk in vol.read_file(entry.path):
                fh.write(chunk)
        tmp.replace(target)
        n += 1
    print(f"pulled {n} files to {dest} ({time.strftime('%Y-%m-%d %H:%M')})")
