# Plan 0096: an OpenRouter judge (GPT Luna) can run every family

Status: draft. Owner: Camila. Scope: `llm.py`, `digest.py`, four family judges, docs, tests.
No bank, prompt, call key, fingerprint or `prompt_version` moves. No judge call of a run on the
family pin changes by one byte (the invariant every step below is checked against).

## Problem

A run with `judge_model=openai/gpt-6-luna` (OpenRouter route) hits Claude-only assumptions:

1. **Free-text stage.** `llm.stream_text_async` raises `JudgeConfigError("free-text stage
   requires a claude-* model")` for any OpenRouter model and requires a `claude-*`
   `refusal_model`. agentic_misalignment (three free-text stages) therefore exits 3 under a Luna
   judge, with Luna or Opus digests alike. Plan 0005 wrote "raises ... for now": a port that was
   never done.
2. **Refusal fallbacks are route-locked.** `llm.stream_json_async` refuses a `refusal_model` on
   another route; `digest._fallback_opt` refuses a non-`claude-*` client-side refusal model.
3. **Refusal fallbacks are switched off by an override.** hinted_cutoff (`_Caller`),
   model_multihop (`_refusal_model`), agentic_misalignment (summary Stage C) and
   jailbreak_recognition (summary draws) enable their fallback only when `args.judge.pinned`. A
   Luna judge is never pinned, so a call Luna declines is simply lost.

Two items of the report are answered here, not changed:

- **The digest pins stay.** agentic_misalignment and jailbreak_recognition keep
  `DIGEST_MODEL = "claude-sonnet-5"` (`JudgeConfig.aux_models["digest"]`) under a judge
  override, exactly as every other family keeps the default Opus 5.5 digest under one:
  `judge_model=` moves the judge, `opts=summ_model=` moves the digest. Nothing couples them
  today and this plan adds no coupling. (A Luna-digest run of these two families is
  `opts=summ_model=openai/gpt-6-luna`; step 3 makes its `none+claude-opus-5` refusal re-run
  legal across routes.)
- **`pinned=False` under a non-default judge is the definition** (README §Judges:
  `pinned_instrument` is true only when the resolved model equals the family pin). It stays.
  What was wrong is that fallbacks keyed on it (item 3); after this plan nothing but the
  instrument-identity stamps reads `judge.pinned`.

## Design

### 1. `llm.py`: free-text primitive on the OpenRouter route

- New `_openrouter_text_once(client, user, *, model, thinking, max_tokens, timeout, spend,
  mark_refusals)`, the OpenRouter twin of `_anthropic_text_once`, same contract (returns the
  stripped text, `""` valid; `None` / `REFUSED` for a refusal):
  - `client.chat.completions.create(stream=True, stream_options={"include_usage": True},
    model=, max_tokens=budget, messages=[{"role": "user", "content": user}], timeout=,
    extra_body={"usage": {"include": True}, "provider": {"require_parameters": True},
    "reasoning": ...})`; user-only message, no system block, no `response_format` (as the
    Anthropic twin sends it).
  - Reasoning: `thinking=True` -> `{"enabled": True}` (the provider's default effort, the
    analogue of Anthropic `adaptive`); `thinking=False` -> `OPENROUTER_MIN_REASONING =
    {"effort": "minimal"}`, the value every structured OpenRouter judge call already runs at
    (`judge_config.DEFAULT_REASONING`, "Gemini cannot turn reasoning off"), so a no-thinking
    stage never 400s on a model that cannot disable reasoning. `judge_config.DEFAULT_REASONING`
    becomes an alias of the `llm` constant (one definition; `judge_config` already imports
    `llm`). The implementer confirms both request shapes against the OpenRouter reasoning-tokens
    documentation before coding and records the doc URL in the PR; if the documented shape
    differs, the documented one wins and the plan line is corrected.
  - Accumulate `choices[0].delta.content` over the stream; keep the last non-null
    `finish_reason`, the `usage` of the final chunk (cost -> `spend.usd` by the rule of
    `_openrouter_once`; prompt / completion tokens -> `spend.input_tokens` / `output_tokens`),
    and any `delta.refusal` text. `spend.calls += 1` once per completed stream.
  - A chunk carrying an `error` object, or a stream that ends with no chunk that has
    `choices`, raises `ValueError` (transient: retried by `_one_text`, as "response has no
    choices" is in `_openrouter_once`).
  - Refusal = `finish_reason == "content_filter"`, or a non-empty refusal with empty text:
    `spend.refusals += 1`, print `  llm refusal: <model>`, return `REFUSED` if
    `mark_refusals` else `None`.
  - Budget doubling, same loop as the Anthropic twin: empty text and `finish_reason ==
    "length"` and `budget < _TEXT_BUDGET_CEILING` -> `budget *= 2`, re-ask. The retained
    invariant, restated: the ceiling stays `_TEXT_BUDGET_CEILING = 64_000` for both routes, and
    `""` after the ceiling is still a valid (cached) result.
- `_one_text` gains a leading `route_` parameter and dispatches like `_one`; its backoff /
  fatal classification is unchanged, plus `_one`'s existing OpenRouter moderation rule (a 403
  with `flagged_input` is one refusal: `REFUSED` under `mark_refusals`, else `None`, never a
  `JudgeConfigError`).
- `stream_text_async`: the `route(model) != "anthropic"` raise is deleted; the client is built
  for `route(model)`. Everything else in its contract is retained: no client when there is
  nothing to call, `""` valid, `on_served` before `on_result`.
- The section comment and docstrings say "both routes"; plan 0005 gets a one-line "superseded
  by plan 0096" note at the "for now" sentence (plans are history; nothing else there moves).

### 2. `llm.py`: a refusal model may be on the other route

Applies to `stream_json_async` and `stream_text_async` alike. The check becomes: `refusal_model
== model` raises (unchanged message shape: `refusal_model=... must be another model`); a
different route is legal.

- When `route(refusal_model) != route(model)`: `api_key(refusal_model)` is resolved **before any
  call** (a missing key for the fallback route is a `JudgeConfigError` up front, never a
  mid-run surprise), and a second client is built with `_make_client` and closed in the same
  `finally`. Same-route refusal models keep sharing the primary's client (today's behaviour,
  byte-identical).
- The re-run's request is re-shaped for its route by one new helper,
  `llm.reasoning_for(route_, reasoning) -> dict | None`: on `anthropic` keep only
  `ANTHROPIC_REASONING_KEYS` (None when empty); on `openrouter` keep every other key, and when
  none is left use `OPENROUTER_MIN_REASONING`. `ANTHROPIC_REASONING_KEYS` moves to `llm`
  (`judge_config` re-exports it), and `judge_config.resolve` calls the helper so the rule has
  one home; `resolve`'s outputs are unchanged for every existing input (a test pins the table).
  **The helper is applied only to a cross-route re-run.** A primary call, and a same-route
  re-run, send `reasoning` exactly as today.
- Cross-route re-run, the other per-route knobs (`stream_json_async` only): `max_tokens` the
  caller left `None` resolves to the refusal route's own `_DEFAULT_MAX_TOKENS`; `temperature`
  is not sent to an Anthropic refusal model (the existing drop rule) and is sent to an
  OpenRouter one; `overflow_max_tokens` state carries over only between Anthropic calls (an
  OpenRouter re-run uses the plain cap); `fallbacks=` is never passed to the re-run (today's
  rule). Retained, restated: only refusals move (a `max_tokens` stop, an error or an
  unparsable body stays the primary's outcome), the re-run happens ONCE, a served JSON result
  carries `SERVED_BY`, a served text fires `on_served`.

### 3. `digest.py`: `summ_fallback` accepts any client-side refusal model

`_fallback_opt` accepts `none+<model>` and `default+<model>` for any non-empty model id (the
`claude-` test is dropped; the error text lists `<model>`). The three client-side re-run sites
(`_digest_pass`'s `on_retry` call, the `run(again, s.refusal_model, True)` site, the levels
re-run near `n_refusal_model_calls`) pass `reasoning=llm.reasoning_for(llm.route(model),
s.reasoning)` **iff** `llm.route(model) != llm.route(s.model)`, else `s.reasoning` as today.
Retained, restated at the point of change: the digest fingerprint and the fallback salt are
untouched; a fallback-served row still carries `SERVED_BY` + `SERVED_VIA = "client"` and
`_stale_fallback` still makes a row another client-side model served a cache miss.

Not in scope (recorded so the reviewer does not chase it): what a non-Claude **primary** digest
model sends (`reasoning={"anthropic_effort": ...}` reaches OpenRouter as-is today; server-side
`fallbacks=` is ignored on that route; `count_tokens` is Anthropic-only). The report did not
flag the Luna-digest setups as failing; changing their request would move existing runs.

### 4. Families: the refusal fallback no longer needs a pinned judge

One rule for all four: **the family's refusal fallback is on whenever the fallback model
differs from the resolved judge model**, whatever `judge.pinned` says, and off under
`opts=fallback=none`.

| family | site | today | after |
|---|---|---|---|
| hinted_cutoff | `_Caller.__init__` | `on and fb and fb != judge.model and judge.pinned` | `on and fb and fb != judge.model` |
| model_multihop | `_refusal_model` | `fb and fb != judge.model and judge.pinned` | `fallback on and fb and fb != judge.model` |
| agentic_misalignment | summary Stage C `refusal_model=` | `REFUSAL_MODEL if judge.pinned else None` | `REFUSAL_MODEL if fallback on and REFUSAL_MODEL != judge.model else None` |
| jailbreak_recognition | summary draws `refusal_model=` | same | same |

- hinted_cutoff's fallback `ResolvedJudge` takes `reasoning=llm.reasoning_for(route(fb),
  args.judge.reasoning)` when the routes differ (it is a separate `run_calls`, so it shapes its
  own request), else `args.judge.reasoning` as today. Its key suffix `|fb`, its "any failed
  call" trigger and `n_fallback_calls` are retained.
- `opts=fallback=on|none` (hinted_cutoff has it; default `on`) is read the same way by the
  other three, through one shared helper in `wsbench/family.py` (`fallback_on(args, name)`;
  any other value exits 2 with hinted_cutoff's message form). It is how a judge-comparison run
  gets a pure single-judge row. It keys no fingerprint and no stamp.
- Instrument identity is untouched: `summary_pinned` (agentic, jailbreak) keeps
  `bool(args.judge.pinned) and ...`, `digest.pinned` likewise, so a Luna row still reports
  `pinned_instrument = false` and is `complete = false` by the results contract, with or
  without a served fallback. On the pin, `REFUSAL_MODEL != judge.model` and `fb != judge.model`
  are always true, so the pinned path is byte-identical.
- Rows already record who served (`judge_served_by` / `judge_model` / `n_fallback_calls` /
  model_multihop's config `refusal_model`); no new result field.

### 5. Docs

README §Judges (the override bullets: fallbacks follow an override judge, `opts=fallback=none`,
the digest pin answer above, free-text stages on both routes), the agentic / jailbreak /
hinted_cutoff / model_multihop family READMEs where they say "when pinned", `CLAUDE.md` judge
layer (`stream_text` is no longer Anthropic-only; cross-route `refusal_model`;
`reasoning_for`), `docs/summarizer.md` where `summ_fallback` is specified.

## Tests (offline; `tests/conftest.py` fakes)

- `test_llm_text.py`: OpenRouter free text through a fake streaming client: plain text;
  `thinking` True / False request bodies; empty + `length` doubles to the ceiling then returns
  `""`; `content_filter` and a moderation 403 -> `REFUSED` / `None`; an `error` chunk is
  retried; spend tallies. The existing Anthropic tests pass unedited.
- `test_llm_refusal_rerun.py`: cross-route re-run both directions for JSON and text (second
  client built, closed, right reasoning / max_tokens / temperature per route; missing fallback
  key raises before the first call; same-route path builds one client as before);
  `refusal_model == model` still raises.
- `reasoning_for` / `judge_config.resolve` table test.
- digest: `summ_fallback=none+openai/gpt-6-luna` parses; a cross-route re-run sends OpenRouter
  reasoning; a same-route one sends `s.reasoning`.
- families: for each of the four, a non-pinned judge with a declined call is re-run on the
  fallback and recorded; `opts=fallback=none` leaves it lost; a judge equal to the fallback
  model gets none; a bad `fallback=` value exits 2. agentic: the three native stages run on an
  OpenRouter judge end to end (fake).
- **No pre-existing test is edited** except assertions that pin the three deleted behaviours
  by name: "free-text stage requires a claude-* model", "must be another <route> model" for a
  cross-route pair, and `summ_fallback` rejecting a non-Claude model, plus any test asserting
  that an unpinned judge of the four families gets no fallback. Each such edit is listed in the
  PR body with its old and new assertion. Any other red pre-existing test is a stop-and-report,
  never an edit.

## Live smoke (after CI, before merge)

OpenRouter is only called from Modal (secret `openrouter`), never from the dev box. From Modal:
`llm.stream_text` on `openai/gpt-6-luna`, one prompt each for `thinking=True` / `False` and one
with `max_tokens=16` (budget doubling), then `wsbench judge family=agentic_misalignment
judge_model=openai/gpt-6-luna limit=1` on existing readouts, native and summary. The PR records
the outcome; a request-shape error found here is fixed in the PR, not after it.

## Files

```
plans/0096-luna-judge.md                          new
plans/0005-agentic.md                             one-line superseded note
src/wsbench/llm.py                                steps 1-2
src/wsbench/judge_config.py                       reasoning_for / constants re-export
src/wsbench/digest.py                             step 3
src/wsbench/family.py                             fallback_on
src/wsbench/evals/{hinted_cutoff,model_multihop,agentic_misalignment,jailbreak_recognition}/judge.py   step 4
README.md, CLAUDE.md, docs/summarizer.md, evals/<4 families>/README.md   step 5
tests/test_llm_text.py, test_llm_refusal_rerun.py, + family / digest tests   new cases
```
