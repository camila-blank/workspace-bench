"""The ``[gen]`` step of the global-workspace ccut pipeline, served by an adapt subject's own Modal
app (plan 0084): ``X_in.jsonl`` rows -> ``X_out.jsonl`` rows, in the row format gw's
``gen_deployed.py`` writes, so every ccut stage reads them unchanged.

    python scripts/hinted_cutoff_adapt/subject_gen.py --model glm-5.3 \
        --inp <out_dir>/base_in.jsonl --out <out_dir>/base_out.jsonl

In rows: ``{key, user, n, temperature, top_p, max_tokens, seed}`` (ccut ``_sample_row``).
Out rows: ``{key, prompt_token_ids, samples: [{text, finish, token_ids}]}``.

The subject is rendered with its pinned thinking-off template (``run._runner``: the adapt
subject rule, ``template.check`` passed) and sampled through ``RemoteRunner.sample`` (the
deployed app; token ids sent). That method returns texts only, so:
- ``token_ids`` are the reply re-encoded with the subject's tokenizer;
- ``finish`` is ``"length"`` when that encoding is >= ``max_tokens - LENGTH_SLACK`` tokens (the
  engine stopped at the cap), else ``"stop"``.
Resumable: keys already in ``--out`` are skipped. Rows are sent in batches of ``--batch``.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

LENGTH_SLACK = 2  # re-encoding a capped reply can differ from the engine's ids by a token or two


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().split("\n") if x.strip()] if p.exists() else []


def finish_of(n_tokens: int, max_tokens: int) -> str:
    return "length" if n_tokens >= max_tokens - LENGTH_SLACK else "stop"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", type=int, default=1024, help="rows per remote round")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    from wsbench.adapt.gate import Ask
    from wsbench.adapt.run import _runner

    rows = _jsonl(Path(a.inp))
    out = Path(a.out)
    done = {r["key"] for r in _jsonl(out)}
    todo = [r for r in rows if r["key"] not in done]
    if a.limit:
        todo = todo[: a.limit]
    print(f"[gen] {len(rows)} rows, {len(done)} done, {len(todo)} to run", flush=True)
    if not todo:
        return
    runner = _runner(a.model)
    tok = runner.tok
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in todo:
        groups[(r["n"], r["temperature"], r["top_p"], r["max_tokens"])].append(r)
    with out.open("a") as f:
        for (n, temp, top_p, mt), rs in groups.items():
            for i in range(0, len(rs), a.batch):
                part = rs[i : i + a.batch]
                asks = [Ask(item_id=r["key"], leg="ccut", max_tokens=mt,
                            messages=({"role": "user", "content": r["user"]},)) for r in part]  # fmt: skip
                # one seed per batch: the runner's seed + the row offset (RemoteRunner.sample)
                runner.seed = int(part[0]["seed"]) % (2**31)
                t0 = time.time()
                res = runner.sample(asks, draws=n, temperature=temp, top_p=top_p)
                for r, ask, (_greedy, texts) in zip(part, asks, res, strict=True):
                    samples = []
                    for t in texts:
                        ids = list(tok(t, add_special_tokens=False)["input_ids"])
                        samples.append({"text": t, "finish": finish_of(len(ids), mt),
                                        "token_ids": ids})  # fmt: skip
                    f.write(json.dumps({"key": r["key"], "prompt_token_ids": runner.prompt_ids(ask),
                                        "samples": samples}, ensure_ascii=False) + "\n")  # fmt: skip
                f.flush()
                print(f"[gen] n={n} batch {i // a.batch + 1}: {len(part)} rows in "
                      f"{time.time() - t0:.0f}s", flush=True)  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
