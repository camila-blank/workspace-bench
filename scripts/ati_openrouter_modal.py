"""ATI panel answers from an OpenRouter model (astra / argon), run on Modal only (plan 0019).

OpenRouter is never called from the dev box. The box builds the prompts
(``text_inversion.pending_requests``), Modal CPU containers call OpenRouter with the ``openrouter``
secret and write every row to the ``wsb-ati-answers`` volume as it completes (so a client
disconnect loses no paid answer), and the box then appends the rows to
``<out>/answers/<answerer>/draw{d}.jsonl``. Resumable: every launch first pulls the rows already
on the volume into the local answers files, so nothing answered is asked (or paid for) again.
Every row carries the ``prompt_sha`` of its question. Launch with ``--detach`` for long runs (a
client disconnect then does not stop the containers); rerun to collect.

    PYTHONPATH=src modal run scripts/ati_openrouter_modal.py --answerer astra \\
        [--out outputs/baselines/text_inversion] [--families a,b] [--shards 8]
"""

import json
from pathlib import Path
from typing import Any

import modal

app = modal.App("wsb-ati-openrouter")
answers_vol = modal.Volume.from_name("wsb-ati-answers", create_if_missing=True)
image = modal.Image.debian_slim(python_version="3.12").pip_install("openai>=1.60", "httpx")
VOL = "/answers"
CONCURRENCY = 16  # per container
MAX_TOKENS = 16000
ATTEMPTS = 8  # errors that count; 429s wait without spending one
RATE_LIMIT_WAITS = 200
# provider finish reasons that are a policy block, recorded once as a refusal (never re-asked)
SAFETY_FINISH = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})


class AbortError(Exception):
    """A key / credit / spend-limit error: stop the shard, never record it as a refusal."""


def classify_error(status: int | None, body: Any) -> str:
    """``refusal`` (final), ``abort`` (stop the run), ``rate`` (wait, no attempt spent) or
    ``retry``. A 403 is a refusal only when OpenRouter says it was moderation-flagged."""
    meta = body.get("error", {}).get("metadata", {}) if isinstance(body, dict) else {}
    if status == 403 and isinstance(meta, dict) and ("reasons" in meta or "flagged_input" in meta):
        return "refusal"
    if status in (401, 402, 403):
        return "abort"
    if status == 429:
        return "rate"
    return "retry"


async def _one(client: Any, sem: Any, model: str, req: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    import random

    import openai

    base = {k: req[k] for k in ("family", "id", "draw", "prompt_sha")}
    base |= {"answerer": req["answerer"]}
    err, waits, attempt = "", 0, 0
    async with sem:
        while attempt < ATTEMPTS:
            try:
                r = await client.chat.completions.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    messages=[{"role": "user", "content": req["content"]}],
                )
            except openai.APIStatusError as exc:
                body: Any
                try:
                    body = exc.response.json()
                except Exception:
                    body = {}
                kind = classify_error(exc.status_code, body)
                err = f"{exc.status_code}: {str(body)[:300]}"
                if kind == "refusal":
                    return base | {"answer": "", "refused": True, "error": "refusal"}
                if kind == "abort":
                    raise AbortError(err) from exc
                if kind == "rate" and waits < RATE_LIMIT_WAITS:
                    waits += 1
                    await asyncio.sleep(30 + 30 * random.random())
                    continue
                attempt += 1
                await asyncio.sleep(min(60, 2 * 2**attempt))
                continue
            except Exception as exc:  # network / timeout: retried
                err = f"{type(exc).__name__}: {exc}"[:300]
                attempt += 1
                await asyncio.sleep(min(60, 2 * 2**attempt))
                continue
            choice = r.choices[0] if r.choices else None
            text = ((choice.message.content if choice else "") or "").strip()
            finish = choice.finish_reason if choice else None
            native = str(getattr(choice, "native_finish_reason", "") or "").upper()
            refusal = (getattr(choice.message, "refusal", None) if choice else None) or ""
            if finish == "content_filter" or refusal or native in SAFETY_FINISH:
                why = refusal[:200] or native or finish
                return base | {"answer": "", "refused": True, "error": "refusal", "why": why}
            if not text:  # e.g. finish_reason=length: reasoning ate the budget; retryable
                err = f"no text (finish_reason={finish})"
                attempt += 1
                continue
            usage = r.usage
            return base | {
                "answer": text,
                "stop_reason": finish,
                "in_tokens": getattr(usage, "prompt_tokens", None),
                "out_tokens": getattr(usage, "completion_tokens", None),
                "model": r.model,
                "error": "",
            }
    return base | {"answer": "", "error": err}


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("openrouter")],
    volumes={VOL: answers_vol},
    timeout=24 * 3600,
)
def answer_shard(run: str, answerer: str, model: str, shard: int, reqs: list[dict]) -> int:
    """Answer one shard; rows are appended to ``/answers/<run>/<answerer>/shard{k}.jsonl`` as
    they complete. Keys already present there are skipped. Returns the number of new rows."""
    import asyncio
    import os

    from openai import AsyncOpenAI

    path = Path(VOL) / run / answerer / f"shard{shard}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:  # a partial last line from a killed container
                continue
            if not r["error"] or r.get("refused"):
                done.add((r["family"], r["id"], r["draw"], r.get("prompt_sha")))
    todo = [r for r in reqs if (r["family"], r["id"], r["draw"], r["prompt_sha"]) not in done]

    async def main() -> int:
        client = AsyncOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            max_retries=0,
            timeout=900,
        )
        sem = asyncio.Semaphore(CONCURRENCY)
        n = 0
        tasks = [asyncio.create_task(_one(client, sem, model, r)) for r in todo]
        with path.open("a", encoding="utf-8") as fh:
            try:
                for fut in asyncio.as_completed(tasks):
                    row = await fut
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    n += 1
                    if n % 200 == 0:
                        answers_vol.commit()
            finally:
                for t in tasks:
                    t.cancel()
                answers_vol.commit()
        return n

    return asyncio.run(main())


@app.function(image=image, secrets=[modal.Secret.from_name("openrouter")], timeout=600)
def preflight(model: str) -> str:
    """One neutral call outside the bank, so a dead key never looks like a refusal."""
    import os

    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1"
    )
    r = client.chat.completions.create(
        model=model, max_tokens=2000, messages=[{"role": "user", "content": "Say OK."}]
    )
    return f"{r.model}: {(r.choices[0].message.content or '')[:40]!r}"


@app.local_entrypoint()
def main(
    answerer: str,
    out: str = "outputs/baselines/text_inversion",
    families: str = "",
    shards: int = 8,
    draws: int = 3,
) -> None:
    from wsbench.baselines import text_inversion as ti

    if answerer not in ti.OPENROUTER_ANSWERERS:
        raise SystemExit(f"answerer must be one of {sorted(ti.OPENROUTER_ANSWERERS)}")
    model = ti.PANEL[answerer]
    out_p = Path(out)
    fams = {f for f in families.split(",") if f} or None
    run = ti.run_key(out_p)
    prefix = f"{run}/{answerer}"
    # collect first: rows already paid for on the volume are never asked again
    print(ti.append_answers(out_p, answerer, ti.download_volume_rows("wsb-ati-answers", prefix)))
    reqs = ti.pending_requests(out_p, answerer, draws, fams)
    print(f"{answerer} ({model}): {len(reqs)} requests pending, run key {run}", flush=True)
    if reqs:
        print("preflight:", preflight.remote(model), flush=True)
        k = max(1, min(shards, len(reqs)))
        parts = [reqs[i::k] for i in range(k)]
        results = list(
            answer_shard.starmap(
                [(run, answerer, model, i, p) for i, p in enumerate(parts)],
                return_exceptions=True,
            )
        )
        errs = [r for r in results if isinstance(r, BaseException)]
        n = sum(r for r in results if isinstance(r, int))
        print(f"{n} new rows written to the volume; {len(errs)} shard(s) failed", flush=True)
        for e in errs:
            print(f"  shard error: {e!r}"[:500], flush=True)
    rows = ti.download_volume_rows("wsb-ati-answers", prefix)
    print(ti.append_answers(out_p, answerer, rows), flush=True)
