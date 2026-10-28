#!/usr/bin/env bash
# Judge every panel answerer's draws (plan 0019): for each answers/<answerer>/ under OUT and each
# draw, `wsbench run` over the readouts at layer 44 (allow_missing), the native-layer families
# without `layers=`. All LLM-judged families are judged by Opus 5.5 (the override replaces pins).
#   OUT=outputs/baselines/text_inversion FAMILIES="a b c" scripts/ati_panel_judge.sh
# Run with ANTHROPIC_API_KEY unset and ANTHROPIC_PROFILE=default (the ant profile). Resumable: each family's judge cache lives in
# its own judged/<answerer>/draw<d>/<family>/ directory.
set -euo pipefail
OUT=${OUT:-outputs/baselines/text_inversion}
DRAWS=${DRAWS:-3}
JUDGE=${JUDGE:-claude-opus-5-5}
NATIVE="arithmetic_intermediates buggy_code"
WSB=${WSB:-wsbench}

answerers=$(cd "$OUT/answers" && ls -d */ 2>/dev/null | tr -d / | grep -E '^(qwen|opus|sonnet|astra|argon)$' || true)
[ -n "$answerers" ] || { echo "no answers/<answerer>/ under $OUT" >&2; exit 2; }

for a in $answerers; do
  for ((d = 0; d < DRAWS; d++)); do
    root="$OUT/readouts/$a/draw$d"
    [ -d "$root" ] || { echo "skip $a draw$d: no readouts (run stage=readouts)"; continue; }
    fams=${FAMILIES:-$(ls "$root" | sed 's/\.jsonl$//' | tr '\n' ' ')}
    if [[ "$a" == opus || "$a" == sonnet ]] && [ -f "$OUT/claude_slot.json" ]; then
      # judge a Claude model only on the families whose slot it fills (default slot: opus)
      fams=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); a=sys.argv[2]
print(" ".join(f for f in sys.argv[3:] if s.get(f, {}).get("slot", "opus") == a))' \
        "$OUT/claude_slot.json" "$a" $fams)
    fi
    layered=() native=()
    for f in $fams; do
      [ -s "$root/$f.jsonl" ] || continue
      if [[ " $NATIVE " == *" $f "* ]]; then native+=("$f"); else layered+=("$f"); fi
    done
    echo "== $a draw$d: ${#layered[@]} families at L44, ${#native[@]} native"
    if ((${#layered[@]})); then
      $WSB run families="$(IFS=,; echo "${layered[*]}")" readouts_root="$root" \
        out="$OUT/judged/$a/draw$d" judge_model="$JUDGE" layers=44 allow_missing=True
    fi
    if ((${#native[@]})); then
      $WSB run families="$(IFS=,; echo "${native[*]}")" readouts_root="$root" \
        out="$OUT/judged/$a/draw$d" judge_model="$JUDGE" allow_missing=True
    fi
  done
done
