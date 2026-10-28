"""ATI panel answers from Qwen3.6-27B (thinking off), vLLM on Modal H200s (plan 0019).

The box builds every request as token ids (``text_inversion.qwen_prompt_ids``: the chat template
around a user span in which no added/special token can match, so a literal ``<|im_start|>`` or
``</think>`` inside the shown prompt stays plain text). Modal only generates: one request per
item with ``n=3`` draws and a per-item seed (``text_inversion.qwen_seed``). Rows are written per
shard to the ``wsb-ati-answers`` volume as each shard finishes and then appended locally to
``<out>/answers/qwen/draw{d}.jsonl``. Resumable per (family, id).

    PYTHONPATH=src modal run scripts/ati_qwen_modal.py [--out ...] [--families a,b] [--shards 8]
"""

import json
from pathlib import Path

import modal

app = modal.App("wsb-ati-qwen")
hf = modal.Volume.from_name("jlens-hf-cache", create_if_missing=True)
answers_vol = modal.Volume.from_name("wsb-ati-answers", create_if_missing=True)
VOL = "/answers"
MODEL = "Qwen/Qwen3.6-27B"
image = (  # adapt_modal.py's recipe: CUDA devel base (flashinfer JIT needs nvcc), vllm 0.31
    modal.Image.from_registry("nvidia/cuda:13.0.1-devel-ubuntu22.04", add_python="3.12")
    .pip_install("vllm==0.31.0", "transformers==5.17.0", "huggingface_hub")
    .env({"HF_HOME": "/hf", "VLLM_WORKER_MULTIPROC_METHOD": "spawn", "PYTHONUNBUFFERED": "1"})
)


@app.function(
    image=image,
    gpu="H200",
    volumes={"/hf": hf, VOL: answers_vol},
    timeout=6 * 3600,
    max_containers=8,
)
def answer_shard(
    run: str, shard: int, reqs: list[dict], max_model_len: int, max_tokens: int, draws: int
) -> int:
    """``reqs`` rows: {family, id, ids, seed}. Writes one row per (item, draw)."""
    from vllm import LLM, SamplingParams
    from vllm.inputs import TokensPrompt

    path = Path(VOL) / run / "qwen" / f"shard{shard}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if path.exists():
        done = {(r["family"], r["id"]) for r in map(json.loads, path.read_text().splitlines())}
    todo = [r for r in reqs if (r["family"], r["id"]) not in done]
    if not todo:
        return 0
    llm = LLM(MODEL, max_model_len=max_model_len, gpu_memory_utilization=0.90, seed=0)
    hf.commit()
    params = [
        SamplingParams(
            n=draws, temperature=1.0, top_p=1.0, top_k=0, max_tokens=max_tokens, seed=r["seed"]
        )
        for r in todo
    ]
    outs = llm.generate([TokensPrompt(prompt_token_ids=r["ids"]) for r in todo], params)
    with path.open("a", encoding="utf-8") as fh:
        for r, o in zip(todo, outs, strict=True):
            for d, c in enumerate(o.outputs):
                text = c.text.strip()
                row = {
                    "family": r["family"],
                    "id": r["id"],
                    "draw": d,
                    "answerer": "qwen",
                    "answer": text,
                    "stop_reason": c.finish_reason,
                    "in_tokens": len(r["ids"]),
                    "out_tokens": len(c.token_ids),
                    "seed": r["seed"],
                    "error": "" if text else f"empty (finish_reason={c.finish_reason})",
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    answers_vol.commit()
    return len(todo)


@app.local_entrypoint()
def main(
    out: str = "outputs/baselines/text_inversion",
    families: str = "",
    shards: int = 8,
    draws: int = 3,
    max_tokens: int = 1024,
) -> None:
    from wsbench.baselines import text_inversion as ti

    out_p = Path(out)
    fams = {f for f in families.split(",") if f} or None
    reqs = ti.qwen_requests(out_p, draws, fams)
    run = ti.run_key(out_p)
    print(f"qwen: {len(reqs)} items pending, run key {run}", flush=True)
    if reqs:
        mml = ti.qwen_max_model_len(reqs, max_tokens)
        k = max(1, min(shards, len(reqs)))
        parts = [reqs[i::k] for i in range(k)]
        args = [(run, i, p, mml, max_tokens, draws) for i, p in enumerate(parts)]
        print(f"{sum(answer_shard.starmap(args))} items generated (max_model_len {mml})")
    rows = ti.download_volume_rows("wsb-ati-answers", f"{run}/qwen")
    print(ti.append_answers(out_p, "qwen", rows), flush=True)
