# Plan 0096: an OpenRouter judge (GPT Luna) can run every family

Status: draft (critique round 1 applied). Owner: Camila. Scope: `llm.py`, `judge_config.py`,
`mcjudge.py`, `digest.py`, `family.py`, four family judges, docs, tests.
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
  - Reasoning: `thinking=True` -> `{"enabled": True}` (OpenRouter documents this as medium
    effort; the analogue of Anthropic `adaptive`); `thinking=False` -> `OPENROUTER_MIN_REASONING =
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
  - Mid-stream errors. The OpenAI SDK never yields an error chunk: it raises
    `openai.APIError` (no `status_code`) from the stream iterator for SSE data with an `error`
    key. On the OpenRouter text route `_one_text` therefore treats as transient: an exception
    named `APIError` with no `status_code` raised while iterating, `finish_reason == "error"`,
    and a stream that ends with no chunk that has `choices` (the last two raise `ValueError`
    inside `_openrouter_text_once`). The stream is closed on every exit path. The test fake
    raises an SDK-shaped exception from its iterator.
  - Refusal = `finish_reason == "content_filter"`, or a non-empty refusal with empty text:
    `spend.refusals += 1`, print `  llm refusal: <model>`, return `REFUSED` if
    `mark_refusals` else `None`. Stated limit: with no schema, a refusal written as ordinary
    content ("I can't help with that") is an answer; the text fallback fires only on
    `content_filter`, a `refusal` field or the moderation 403.
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
- **Structured calls get the same refusal rule** (`_openrouter_once`): `finish_reason ==
  "content_filter"`, or a non-empty `message.refusal` with empty content (both read with
  `getattr`, so the conftest `SimpleNamespace` fakes pass unchanged) -> `spend.refusals += 1`,
  `REFUSED_JSON` under `mark_refusals`, else `None`; `_openrouter_once` gains the
  `mark_refusals` keyword and `_one` passes it. Without this a declined structured Luna call is
  a `JSONDecodeError`, retried 12 times and lost as an error, and step 4 would do nothing for
  model_multihop and jailbreak. This changes OpenRouter judges only (never a pin): 12 retries
  + one error becomes one refusal.
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
  re-run, send `reasoning` exactly as today. `reasoning_for` accepts `None` and returns a copy
  of the constant, never the constant itself.
- `stream_json_async` and `mcjudge.run_calls` gain `refusal_reasoning: dict | None = None`.
  When given it is the re-run's reasoning as is (no helper). Reason: a family's own
  `anthropic_effort` is dropped by `resolve` for an OpenRouter judge, so the callee cannot
  recover it; without this a Sonnet 5.5 fallback under a Luna judge would run at `effort: low`
  while the same fallback on the pin runs at the family's `default`. Callers pass it only for
  a cross-route fallback (step 4).
- Cross-route re-run, the other per-route knobs (`stream_json_async` only): `max_tokens` the
  caller left `None` resolves to the refusal route's own `_DEFAULT_MAX_TOKENS`; `temperature`
  is not sent to an Anthropic refusal model (the existing drop rule) and is sent to an
  OpenRouter one (the caller's ORIGINAL value: today's code overwrites `temperature` with
  `None` on an Anthropic primary, so keep the original for an Anthropic -> OpenRouter re-run); `overflow_max_tokens` state carries over only between Anthropic calls (an
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

A cross-route digest re-run keeps `s.max_tokens` and `overflow_kw` (the latter ignored off
Anthropic); a cap the other route's provider rejects is that run's configuration problem, not
re-shaped here.

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

- **Fallback effort = the family's own instrument.** For a cross-route fallback, its reasoning
  is `judge_config.resolve(<the family's JudgeConfig>, flag=fb).reasoning` (hinted_cutoff and
  model_multihop set `anthropic_effort: default`; agentic and jailbreak set none, so theirs is
  `None`): hinted_cutoff's fallback `ResolvedJudge` takes it, the other `run_calls` sites pass
  it as `refusal_reasoning=`. A same-route fallback sends what it sends today
  (`args.judge.reasoning`; no `refusal_reasoning`). hinted_cutoff's key suffix `|fb`, its "any
  failed call" trigger and `n_fallback_calls` are retained.
- `opts=fallback=on|none` (hinted_cutoff has it; default `on`) is read the same way by the
  other three, through one shared helper in `wsbench/family.py` (`fallback_on(args, name)`;
  any other value exits 2 with hinted_cutoff's message form), called at the top of each
  family's `run()`, so a bad value exits 2 in native mode too (where agentic and jailbreak have
  no judge fallback and the value is otherwise unused). It turns off the JUDGE-call fallback
  only (the digest's is `opts=summ_fallback=`), and it is how a judge-comparison run gets a
  row whose judge calls are one model's. It keys no fingerprint.
- **`fallback=none` never reuses a fallback-served cache row.** `run_calls` caches a served
  row under the judge's fingerprint and agentic's `_batch` does the same, so a resumed out dir
  would silently mix. `run_calls` gains `accept_served: bool = True`; with `False` a cached
  row carrying a serving model is a miss (re-asked on the judge, the new row appended), and
  `_batch` gets the same switch. The three families pass `False` exactly when their fallback
  is off by `opts=fallback=none`. Default `True` = today's lookup, byte-identical.
  hinted_cutoff needs nothing (its `|fb` rows are read only when the fallback is on).
- **Credentials up front.** When a family's effective fallback is on another route than the
  judge and the run is not a dry run, the family resolves `llm.api_key(fallback)` before its
  first call (a missing key is a `JudgeConfigError`, exit 3, naming `opts=fallback=none`);
  `digest.collapse` does the same for a cross-route `s.refusal_model`. Same-route: no new
  call (the pinned path is untouched).
- **Stamps.** On the pin with the fallback on (the default) `config` is byte-identical to
  today (the logit-lens floor test matches full live configs). Otherwise, i.e. when the judge
  is not the pin or `fallback=none`, agentic / jailbreak / model_multihop add
  `config.judge_fallback = <effective fallback model> | "none"`; hinted_cutoff keeps its
  existing `config.fallback_model`. `summary_pinned` (agentic, jailbreak) and model_multihop's
  `pinned` gain "and the judge fallback is on", so `fallback=none` on the pin is not the
  instrument of record; hinted_cutoff's existing `fallback=none` behaviour is left as it is.
  A Luna row still reports `pinned_instrument = false` and `complete = false`. On the pin,
  `REFUSAL_MODEL != judge.model` and `fb != judge.model` always hold, so the pinned path is
  byte-identical.
- **Provenance.** agentic and jailbreak rows already record the serving model (`judge_model`
  / `judge_served_by`). model_multihop and hinted_cutoff record nothing per row today, so
  under a Luna judge an unknown subset of cells would be Sonnet 5.5's: a row whose verdict a
  fallback served gains `judge_served_by` (model_multihop: from `llm.SERVED_BY`; hinted_cutoff:
  the fallback model for a chunk whose `|fb` result was used, as a per-row list or count,
  whichever its row shape takes with least change), and `extras.n_judge_fallback_served` is
  added when > 0. A row no fallback served is byte-identical.

### 5. Docs

README §Judges (the override bullets: fallbacks follow an override judge, `opts=fallback=none`,
the digest pin answer above, free-text stages on both routes), the agentic / jailbreak /
hinted_cutoff / model_multihop family READMEs where they say "when pinned", `CLAUDE.md` judge
layer (`stream_text` is no longer Anthropic-only; cross-route `refusal_model`;
`reasoning_for`), `docs/summarizer.md` where `summ_fallback` is specified, and the
`stream_json_async` / `mcjudge.run_calls` docstrings ("same route" wording).

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
- structured OpenRouter refusal (`content_filter`, `message.refusal`) -> `REFUSED_JSON` and the
  re-run; `refusal_reasoning=` is what the re-run sends; `accept_served=False` re-asks a
  served row (a fallback-on run, then `fallback=none` on the same out dir, per family).
- one request-capture test per family ON ITS PIN: the request kwargs of every judge and
  fallback call, and `config`, equal a golden captured from `origin/main` before the change.
- **No pre-existing test is edited** except exactly these three, which pin deleted behaviour:
  `tests/test_llm_text.py::test_openrouter_model_raises`, the `"none+gpt-6"` entry of the bad
  list in `tests/test_llm_refusal_rerun.py` (~l.311), and the `default+gpt-5` / `none+gpt-5`
  params in `tests/test_digest_hierarchical.py` (~l.395). Each is listed in the PR body with
  its old and new assertion. Any other red pre-existing test is a stop-and-report, never an
  edit.

## Live smoke (after CI, before merge)

OpenRouter is only called from Modal (secret `openrouter`), never from the dev box; the smoke
also needs Anthropic credentials in the container (the Sonnet 5 digest and the Opus 5 / Sonnet
5.5 fallbacks). It adds one model_multihop `limit=3` run (its `temperature=0.0` under
`require_parameters`) to the list. From Modal:
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
src/wsbench/mcjudge.py                            refusal_reasoning, accept_served
src/wsbench/digest.py                             step 3
src/wsbench/family.py                             fallback_on
src/wsbench/evals/{hinted_cutoff,model_multihop,agentic_misalignment,jailbreak_recognition}/judge.py   step 4
README.md, CLAUDE.md, docs/summarizer.md, evals/<4 families>/README.md   step 5
tests/test_llm_text.py, test_llm_refusal_rerun.py, + family / digest tests   new cases
```
