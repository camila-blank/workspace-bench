"""Plan 0084 R4: re-derive ``finish`` in a ``subject_gen.py`` out file under the current
``LENGTH_SLACK`` (rows written with an older slack); prints how many samples changed.

    python refinish.py <X_out.jsonl> [max_tokens=1536]
"""

import json
import sys
from pathlib import Path

from subject_gen import finish_of

p = Path(sys.argv[1])
mt = int(sys.argv[2]) if len(sys.argv) > 2 else 1536
rows = [json.loads(x) for x in p.read_text().split("\n") if x.strip()]
n = 0
for r in rows:
    for s in r["samples"]:
        f = finish_of(len(s["token_ids"]), mt)
        n += f != s["finish"]
        s["finish"] = f
p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
print(f"{p}: {n} samples changed finish")
