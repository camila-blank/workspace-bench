"""One structured-JSON judge client with two routes.

* ``claude-*`` model ids go to the Anthropic SDK (structured outputs via ``output_config``);
  everything else goes to OpenRouter through the OpenAI SDK (``response_format`` json_schema).
* Requests are paced under a process-wide requests-per-minute cap shared by every event loop
  and thread (``_PACER``), so several families judged concurrently share one budget.
* Transient failures (408/409/429/5xx, timeouts, connection errors, garbled bodies) are
  retried with jittered backoff; any other per-call failure yields ``None`` (an unjudged cell,
  never a clean one). Problems that would fail every call raise :class:`JudgeConfigError`.
* ``on_result(i, result)`` fires as each call lands so callers can cache as they go.
* OpenRouter route only (plan 0059): ``WSBENCH_OPENROUTER_MAX_OUT`` sets every call's
  ``max_tokens`` (the per-model output budget, raising or lowering the request); a 400 naming
  the context length is too-long (``TOO_LONG_JSON`` under
  ``mark_refusals``); ``message.refusal``, ``finish_reason == "content_filter"`` and a moderation
  403 are refusals; ``finish_reason == "length"`` with unparseable JSON is one failed call; an
  error delivered in a 200 body is classified by its ``error.code`` like an HTTP status.
* Plan 0059 addendum A: with ``WSBENCH_OPENAI_ROUTE=wif`` an ``openai/*`` id (not
  ``openai/gpt-oss-*``) takes a third route, ``openai``: OpenAI directly through
  ``wsbench.baselines.ati_remote.Clients("openai-wif")`` (workload identity federation; a 401
  re-exchanges the token up to ``ati_remote.AUTH_REFRESHES`` times, then is fatal). Unset, routing
  is unchanged. The request carries ``max_completion_tokens`` (the same output budget),
  ``reasoning_effort`` from the reasoning's ``effort``, no ``temperature``, no OpenRouter
  ``extra_body``; the response rules are the OpenRouter ones (a 403 is always fatal). OpenAI
  returns no cost, so ``Spend.usd`` stays 0 on this route.
"""

import asyncio
import json
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from wsbench import ledger

OPENROUTER = "https://openrouter.ai/api/v1"
_TRANSIENT = (408, 409, 429, 500, 502, 503, 504, 529)
_FATAL = ("AuthenticationError", "PermissionDeniedError", "NotFoundError")
_ATTEMPTS = 12
_DEFAULT_MAX_TOKENS = {"openrouter": 8000, "anthropic": 16000, "openai": 8000}
# plan 0059: the OpenRouter output budget — every call's max_tokens, raised or lowered to it
# (outside every fingerprint; the sweep driver sets it to min(max_out, 32 000, context // 4), so
# the output request can never be what overflows)
MAX_OUT_ENV = "WSBENCH_OPENROUTER_MAX_OUT"
# an OpenRouter 400 whose message names the context length is a too-long input (pinned by a test;
# "too many tokens" is deliberately absent: it also names an output request over the cap)
OPENROUTER_TOO_LONG = (
    "maximum context length",
    "context length",
    "context window",
    "prompt is too long",
)
# the final statuses that mean a call was throttled (rate limit / exhausted quota), not broken
THROTTLE_STATUSES = frozenset({"429", "quota"})
# plan 0059 addendum A: "wif" sends openai/* ids (not gpt-oss) to OpenAI directly (route openai)
OPENAI_ROUTE_ENV = "WSBENCH_OPENAI_ROUTE"
_OPEN_WEIGHTS = "openai/gpt-oss"  # served by other providers, never by OpenAI: stays on OpenRouter

Route = Literal["anthropic", "openrouter", "openai"]


class JudgeConfigError(RuntimeError):
    """A configuration problem that would fail every judge call (raised, never degraded)."""


@dataclass
class Spend:
    """Per-run tally. ``calls`` counts HTTP 200 responses (including garbled bodies that were
    then retried); ``errors``/``refusals`` count cells left unjudged. ``input_tokens`` excludes
    cached prompt tokens on the Anthropic route (those are ``cache_read_tokens`` /
    ``cache_write_tokens``) and includes them on OpenRouter (``prompt_tokens``); output tokens
    include thinking / reasoning on both. ``transient``: status -> count of retried transient
    failures (``"408"`` ... ``"529"``, ``"timeout"``, ``"connection"``; parse retries are not
    counted), recovered or not; ``failed``: final status -> count of calls that ENDED failed with
    one (an HTTP status, ``"timeout"`` / ``"connection"``, or a 200-body ``error.code``), so a
    call that ended throttled (:meth:`throttled`) is told from a recovered retry. A Spend built
    inside a runner family registers in the spend ledger (``wsbench.ledger``)."""

    usd: float = 0.0
    calls: int = 0
    retries: int = 0
    errors: int = 0
    refusals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    transient: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ledger.register(self)

    def throttled(self) -> int:
        """Calls that ENDED failed on a rate limit or an exhausted quota (recovered 429s are
        not counted)."""
        return sum(n for k, n in self.failed.items() if k in THROTTLE_STATUSES)

    def report(self) -> str:
        s = (
            f"calls={self.calls} retries={self.retries} errors={self.errors} "
            f"refusals={self.refusals} spend=${self.usd:.2f}"
        )
        if self.input_tokens or self.output_tokens:
            s += f" in_tok={self.input_tokens} out_tok={self.output_tokens}"
        return s


def route(model: str) -> Route:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("openai/") and not model.startswith(_OPEN_WEIGHTS) and _openai_wif():
        return "openai"
    return "openrouter"


def _openai_wif() -> bool:
    """``WSBENCH_OPENAI_ROUTE`` is ``wif`` (unset / empty: ``False``; anything else is a
    :class:`JudgeConfigError`)."""
    raw = os.environ.get(OPENAI_ROUTE_ENV, "").strip()
    if raw and raw != "wif":
        raise JudgeConfigError(f"{OPENAI_ROUTE_ENV}={raw!r}: expected 'wif' or unset")
    return raw == "wif"


def api_key(model: str) -> str:
    route_ = route(model)
    if route_ == "openai":
        return ""  # workload identity federation: the token is exchanged by the client
    if route_ == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            return key
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return OAUTH_PREFIX + os.environ["ANTHROPIC_AUTH_TOKEN"]  # keyless OAuth bearer token
        if os.environ.get("ANTHROPIC_PROFILE"):
            return ""  # "" = let the SDK resolve the named `ant auth login` profile
        raise JudgeConfigError(
            f"ANTHROPIC_API_KEY is missing (needed for {model}); or set ANTHROPIC_AUTH_TOKEN, or "
            "ANTHROPIC_PROFILE to use an `ant auth login` profile"
        )
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key.startswith("sk-or-"):
        raise JudgeConfigError("OPENROUTER_API_KEY is missing or not an OpenRouter key (sk-or-…)")
    return key


def schema_block(name: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": list(required),
            "properties": properties,
        },
    }


# ---------------------------------------------------------------- pacing / backoff seams


def _now() -> float:
    return time.monotonic()


class _Pacer:
    """Process-wide next-slot time behind a lock: shared by every loop, thread and Spend."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def reset(self) -> None:
        with self._lock:
            self._next = 0.0

    def reserve(self, rpm: float) -> float:
        """Reserve the next slot; return how long the caller must wait for it (seconds)."""
        with self._lock:
            now = _now()
            slot = max(now, self._next)
            self._next = slot + 60.0 / rpm
            return slot - now


_PACER = _Pacer()


_sleep = asyncio.sleep  # seam: tests patch this instead of the global asyncio.sleep


async def _pace(rpm: float) -> None:
    delay = _PACER.reserve(rpm)
    if delay > 0:
        await _sleep(delay)


def _backoff(attempt: int, status: int | None) -> float:
    cap = 90.0 if status == 429 else 30.0
    return min(cap, 2.0 * 2**attempt) * (0.5 + random.random())


OAUTH_PREFIX = "oauth:"  # api_key() marks an OAuth bearer token (ANTHROPIC_AUTH_TOKEN) with this


def _make_client(route: str, key: str) -> Any:
    """The ONLY place SDK clients are built (tests monkeypatch this to inject fakes)."""
    if route == "anthropic":
        from anthropic import AsyncAnthropic

        if key.startswith(OAUTH_PREFIX):  # keyless judges: bearer auth + the OAuth beta flag
            return AsyncAnthropic(
                auth_token=key[len(OAUTH_PREFIX) :],
                api_key=None,
                default_headers={"anthropic-beta": "oauth-2025-04-20"},
                max_retries=0,
            )
        if not key:  # an `ant` profile (ANTHROPIC_PROFILE): the SDK reads its OAuth credentials
            return AsyncAnthropic(max_retries=0)
        return AsyncAnthropic(api_key=key, max_retries=0)
    if route == "openai":
        from wsbench.baselines import ati_remote  # lazily: the route is opt-in

        try:
            return _WifClient(ati_remote.Clients("openai-wif"))
        except ati_remote.AbortError as e:  # the openai-wif secret / Modal identity is missing
            raise JudgeConfigError(str(e)) from e
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=key, base_url=OPENROUTER, max_retries=0)


# ---------------------------------------------------------------- one call


def _parse_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    data = json.loads(content)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("not a JSON object", content, 0)
    return data


async def _openrouter_once(
    client: Any,
    system: str,
    user: str,
    *,
    schema: dict[str, Any],
    model: str,
    reasoning: dict[str, Any] | None,
    temperature: float | None,
    max_tokens: int,
    timeout: float,
    spend: Spend,
    mark_refusals: bool = False,
) -> dict[str, Any] | None:
    extra_body: dict[str, Any] = {
        "usage": {"include": True},
        "provider": {"require_parameters": True},
    }
    if reasoning is not None:
        extra_body["reasoning"] = reasoning
    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = await client.chat.completions.create(
        timeout=timeout,
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": schema},
        extra_body=extra_body,
        **kwargs,
    )
    return _chat_result(
        resp, model=model, max_tokens=max_tokens, spend=spend, mark_refusals=mark_refusals
    )


class _WifClient:
    """The ``openai`` route's client: ``ati_remote.Clients("openai-wif")`` (one ``AsyncOpenAI``
    per access token) behind the ``close()`` the batch calls at the end."""

    def __init__(self, clients: Any) -> None:
        self.clients = clients
        self._built: list[Any] = []

    async def get(self) -> Any:
        c = await self.clients.get()
        if not any(c is b for b in self._built):
            self._built.append(c)
        return c

    def auth_failed(self, client: Any) -> None:
        self.clients.auth_failed(client)

    async def close(self) -> None:
        for c in self._built:
            close = getattr(c, "close", None)
            if close is not None:
                await close()


async def _openai_once(
    wif: Any,
    system: str,
    user: str,
    *,
    schema: dict[str, Any],
    model: str,
    reasoning: dict[str, Any] | None,
    max_tokens: int,
    timeout: float,
    spend: Spend,
    mark_refusals: bool = False,
) -> dict[str, Any] | None:
    """One OpenAI chat completion through WIF (plan 0059 addendum A). A 401 drops the access
    token and re-exchanges, up to ``ati_remote.AUTH_REFRESHES`` times; the last 401 propagates
    (fatal in :func:`_one`). A failed token exchange is fatal (key / identity problem) or, on a
    429 / 5xx from the token endpoint, a retried transient failure."""
    from wsbench.baselines import ati_remote

    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "model": ati_remote.route_model(model, "openai-wif"),
        "max_completion_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": schema},
    }
    effort = (reasoning or {}).get("effort")
    if effort:
        kwargs["reasoning_effort"] = str(effort)
    for auth in range(ati_remote.AUTH_REFRESHES + 1):
        try:
            client = await wif.get()
        except ati_remote.AbortError as e:
            raise JudgeConfigError(str(e)) from e
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            if getattr(e, "status_code", None) == 401 and auth < ati_remote.AUTH_REFRESHES:
                wif.auth_failed(client)  # the short-lived token may have expired mid-run
                continue
            raise
        return _chat_result(
            resp, model=model, max_tokens=max_tokens, spend=spend, mark_refusals=mark_refusals
        )
    raise AssertionError("unreachable: the last 401 propagates")


def _chat_result(
    resp: Any, *, model: str, max_tokens: int, spend: Spend, mark_refusals: bool
) -> dict[str, Any] | None:
    """A chat-completions response (OpenRouter or OpenAI) -> the parsed object, a refusal or
    ``None``; tallies cost and tokens."""
    spend.calls += 1
    if not getattr(resp, "choices", None):  # an error delivered in a 200 body
        raise _body_error(resp)
    usage = getattr(resp, "usage", None)
    cost = getattr(usage, "cost", None) if usage is not None else None
    if cost is None and usage is not None:
        cost = (getattr(usage, "model_extra", None) or {}).get("cost")
    spend.usd += float(cost or 0.0)
    if usage is not None:  # getattr defaults: the test fakes carry only ``cost``
        spend.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        spend.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
    choice = resp.choices[0]
    message = getattr(choice, "message", None)
    finish = getattr(choice, "finish_reason", None)
    content = getattr(message, "content", None) or ""
    if getattr(message, "refusal", None) or finish == "content_filter":
        spend.refusals += 1  # non-transient: a refusal is never retried
        print(f"  llm refusal: {model} finish_reason={finish}")
        return REFUSED_JSON if mark_refusals else None
    try:
        return _parse_object(content)
    except (ValueError, json.JSONDecodeError):
        if finish != "length":
            raise  # a garbled body: retried
        # cut off at the output cap: a retry would be cut off again (and paid again)
        spend.errors += 1
        _tally(spend.failed, "length")
        print(f"  llm error: {model} finish_reason=length (max_tokens={max_tokens})")
        return None


class _BodyError(Exception):
    """An error OpenRouter delivered in a 200 body, classified by ``status_code`` = its
    ``error.code`` (an int when numeric, else the code string, e.g. ``"quota"``)."""

    def __init__(self, message: str, status_code: int | str | None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _body_error(resp: Any) -> Exception:
    """The exception for a 200 body with no choices: a :class:`_BodyError` carrying
    ``error.code`` (a code-less rate-limit / quota message counts as 429), else the old
    ``ValueError`` parse retry."""
    err = getattr(resp, "error", None)
    if err is None:
        err = (getattr(resp, "model_extra", None) or {}).get("error")
    if not isinstance(err, dict):
        err = {"code": getattr(err, "code", None), "message": getattr(err, "message", "")}
    code: Any = err.get("code")
    msg = str(err.get("message") or "")
    if isinstance(code, str) and code.strip().isdigit():
        code = int(code.strip())
    if code is None and any(w in msg.lower() for w in ("rate limit", "rate-limit", "quota")):
        code = 429
    if code is None or isinstance(code, bool) or not isinstance(code, int | str):
        return ValueError("response has no choices")
    return _BodyError(f"200-body error {code}: {msg[:300]}", code)


def _moderated(e: Exception) -> bool:
    """An OpenRouter 403 that is a moderation flag (``error.metadata.reasons``, or a message
    saying the input was flagged), not an auth / access problem."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else None
    meta = err.get("metadata") if isinstance(err, dict) else None
    if isinstance(meta, dict) and "reasons" in meta:
        return True
    msg = str(err.get("message") or "") if isinstance(err, dict) else ""
    return "flagged" in (msg + " " + str(e)).lower()


def _status_kind(e: Exception, status: Any) -> str | None:
    """The ``Spend.transient`` / ``Spend.failed`` key of a failure: its status, or ``timeout`` /
    ``connection`` by exception name; ``None`` for a parse failure."""
    if status is not None:
        return str(status)
    name = type(e).__name__
    if name == "TransientExchangeError":
        return "token_exchange"
    if "Timeout" in name or "Deadline" in name:
        return "timeout"
    if "Connection" in name:
        return "connection"
    return None


def _tally(d: dict[str, int], kind: str | None) -> None:
    if kind is not None:
        d[kind] = d.get(kind, 0) + 1


# What a free-text call returns for a model refusal when the caller passes mark_refusals=True
# (agentic Stage A treats a refused position as uninformative; plan 0012, Camila 2026-10-11).
REFUSED = "\x00refused\x00"
# What a structured-output call returns for a model refusal when the caller passes
# mark_refusals=True (the summary-mode digest tells a refusal from a failure; plan 0021)
REFUSED_JSON: dict[str, Any] = {"\x00refused": True}
# ... and for a context overflow (stop_reason model_context_window_exceeded, or a 400 "prompt is too
# long"), also only under mark_refusals=True
TOO_LONG_JSON: dict[str, Any] = {"\x00too_long": True}
# key a structured result carries when a server-side refusal fallback served it (the model id)
SERVED_BY = "\x00served_by"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Models that reject thinking={"type": "disabled"} (thinking is always on): a no-thinking call
# omits the parameter and asks for low effort instead, so the reply budget goes to the answer.
_ALWAYS_THINK = ("claude-opus-5-5", "claude-fable-", "claude-mythos-", "claude-sonnet-5-5")


def always_thinks(model: str) -> bool:
    return model.startswith(_ALWAYS_THINK)


def anthropic_effort(model: str, reasoning: dict[str, Any] | None) -> dict[str, str]:
    """The ``output_config`` effort of a structured Anthropic call. No family setting: low effort
    on always-thinking models (the repo default). A family's
    ``reasoning={"anthropic_effort": "default"}`` omits it (the model's own default, as the source
    instrument of introspection ran); any other value is passed through. The
    OpenRouter-style ``effort`` key stays ignored on this route."""
    effort = (reasoning or {}).get("anthropic_effort")
    if effort == "default":
        return {}
    if effort:
        return {"effort": str(effort)}
    return {"effort": "low"} if always_thinks(model) else {}


# JSON-schema keywords Anthropic structured outputs reject (OpenRouter accepted them): numeric
# bounds. The prompts state every range in words, so dropping them from the schema is safe.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
)


def anthropic_schema(schema: Any) -> Any:
    """A copy of ``schema`` without the keywords the Anthropic route rejects."""
    if isinstance(schema, dict):
        return {
            k: anthropic_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [anthropic_schema(v) for v in schema]
    return schema


async def _anthropic_once(
    client: Any,
    system: str,
    user: str,
    *,
    schema: dict[str, Any],
    model: str,
    max_tokens: int,
    timeout: float,
    spend: Spend,
    reasoning: dict[str, Any] | None = None,
    mark_refusals: bool = False,
    fallbacks: str | None = None,
) -> dict[str, Any] | None:
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "format": {"type": "json_schema", "schema": anthropic_schema(schema["schema"])},
            **anthropic_effort(model, reasoning),
        },
    }
    if fallbacks is None:
        resp = await client.messages.create(**kwargs)
    else:  # server-side refusal fallback: a declined request re-runs on a fallback model
        betas = [FALLBACK_BETA]
        if getattr(client, "auth_token", None):  # betas= replaces the client's default header
            betas.append("oauth-2025-04-20")
        resp = await client.beta.messages.create(**kwargs, betas=betas, fallbacks=fallbacks)
    spend.calls += 1
    usage = getattr(resp, "usage", None)
    if usage is not None:
        spend.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        spend.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
    stop = getattr(resp, "stop_reason", None)
    if usage is not None:  # getattr defaults: older fakes carry no cache fields
        spend.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        spend.cache_write_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    if stop == "refusal":
        spend.refusals += 1
        print(f"  llm refusal: {model}")
        return REFUSED_JSON if mark_refusals else None
    if stop in ("max_tokens", "model_context_window_exceeded"):
        spend.errors += 1
        _tally(spend.failed, "max_tokens" if stop == "max_tokens" else "context_window")
        print(f"  llm error: {model} stop_reason={stop}")
        if mark_refusals and stop == "model_context_window_exceeded":
            return TOO_LONG_JSON
        return None
    blocks = list(resp.content)
    seams = [j for j, b in enumerate(blocks) if getattr(b, "type", None) == "fallback"]
    if fallbacks is not None and seams:
        # a mid-output refusal: the declining model's partial text, a fallback seam, then the
        # serving model's output (which may continue the partial as a prefill)
        texts = [getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text"]
        after = "".join(
            getattr(b, "text", "")
            for b in blocks[seams[-1] + 1 :]
            if getattr(b, "type", None) == "text"
        )
        try:
            out = _parse_object("".join(texts))
        except (ValueError, json.JSONDecodeError):
            out = _parse_object(after)
        if isinstance(out, dict):
            out[SERVED_BY] = str(getattr(resp, "model", "") or "fallback")
        return out
    text = next((b.text for b in blocks if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise ValueError("response has no text block")
    return _parse_object(text)


async def _one(
    route_: Route,
    client: Any,
    system: str,
    user: str,
    *,
    schema: dict[str, Any],
    model: str,
    reasoning: dict[str, Any] | None,
    temperature: float | None,
    max_tokens: int,
    timeout: float,
    rpm: float,
    spend: Spend,
    mark_refusals: bool = False,
    attempts: int | None = None,
    fallbacks: str | None = None,
) -> dict[str, Any] | None:
    # detokenised lens readouts can contain lone surrogates, which both APIs reject every time
    system = system.encode("utf-8", "replace").decode("utf-8")
    user = user.encode("utf-8", "replace").decode("utf-8")
    n_attempts = _ATTEMPTS if attempts is None else max(1, min(attempts, _ATTEMPTS))
    for attempt in range(n_attempts):
        await _pace(rpm)
        try:
            if route_ == "anthropic":
                return await _anthropic_once(
                    client,
                    system,
                    user,
                    schema=schema,
                    model=model,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    spend=spend,
                    reasoning=reasoning,
                    mark_refusals=mark_refusals,
                    fallbacks=fallbacks,
                )
            if route_ == "openai":
                return await _openai_once(
                    client,
                    system,
                    user,
                    schema=schema,
                    model=model,
                    reasoning=reasoning,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    spend=spend,
                    mark_refusals=mark_refusals,
                )
            return await _openrouter_once(
                client,
                system,
                user,
                schema=schema,
                model=model,
                reasoning=reasoning,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                spend=spend,
                mark_refusals=mark_refusals,
            )
        except Exception as e:
            if isinstance(e, JudgeConfigError):  # the openai route's failed token exchange
                raise
            name = type(e).__name__
            status = getattr(e, "status_code", None)
            if route_ == "openrouter" and status == 403 and _moderated(e):
                spend.refusals += 1  # a moderation flag: this input, not the key (plan 0059)
                print(f"  llm refusal: {model} (moderation 403)")
                return REFUSED_JSON if mark_refusals else None
            if (
                name in _FATAL
                or status in (401, 402, 403)  # bad key, no credits, no access
                or (isinstance(e, _BodyError) and status == 404)  # as the HTTP NotFoundError
            ):
                raise JudgeConfigError(f"{name}: {str(e)[:300]}") from e
            kind = _status_kind(e, status)
            transient = (
                status in _TRANSIENT
                or "Timeout" in name
                or "Connection" in name
                or "Deadline" in name
                or isinstance(e, json.JSONDecodeError | ValueError)
                or name == "TransientExchangeError"  # openai route: token endpoint 429 / 5xx
                # a 200-body error keeps the old retry unless it is a too-long input (the
                # fatal 401-404 codes raised above)
                or (isinstance(e, _BodyError) and not (status == 400 and _too_long(route_, str(e))))
            )
            if transient and attempt < n_attempts - 1:
                spend.retries += 1
                if not isinstance(e, json.JSONDecodeError | ValueError):  # parse retries: no
                    _tally(spend.transient, kind)
                await _sleep(_backoff(attempt, status if isinstance(status, int) else None))
                continue
            spend.errors += 1
            _tally(spend.failed, kind)
            print(f"  llm error: {name}: {str(e)[:200]}")
            if mark_refusals and status == 400 and _too_long(route_, str(e)):
                return TOO_LONG_JSON
            return None
    return None


def _too_long(route_: Route, message: str) -> bool:
    """A 400 that is a too-long input: "prompt is too long"; on OpenRouter (and the ``openai``
    route) with ``WSBENCH_OPENROUTER_MAX_OUT`` set, any :data:`OPENROUTER_TOO_LONG` phrase (sound
    only under that output cap, plan 0059: without it a "maximum context length" 400 can be the
    output request)."""
    msg = message.lower()
    if route_ == "anthropic" or openrouter_max_out() is None:  # the broad list needs the cap
        return "prompt is too long" in msg
    return any(p in msg for p in OPENROUTER_TOO_LONG)


def openrouter_max_out() -> int | None:
    """``WSBENCH_OPENROUTER_MAX_OUT`` as a positive int (``None`` when unset); a bad value is a
    :class:`JudgeConfigError`."""
    raw = os.environ.get(MAX_OUT_ENV, "").strip()
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        cap = 0
    if cap <= 0:
        raise JudgeConfigError(f"{MAX_OUT_ENV}={raw!r}: expected a positive integer")
    return cap


# ---------------------------------------------------------------- the primitive


async def stream_json_async(
    prompts: list[tuple[str, str]],
    *,
    schema: dict[str, Any],
    model: str,
    on_result: Callable[[int, dict[str, Any] | None], None],
    reasoning: dict[str, Any] | None = None,
    temperature: float | None = None,
    concurrency: int = 64,
    rpm: float = 240.0,
    max_tokens: int | None = None,
    timeout: float = 180.0,
    spend: Spend | None = None,
    mark_refusals: bool = False,
    attempts: int | None = None,
    fallbacks: str | None = None,
) -> Spend:
    """Run ``(system, user)`` prompt pairs concurrently, handing each parsed result (or
    ``None``) to ``on_result(index, result)`` as it lands. Raises :class:`JudgeConfigError` on
    a config problem. Builds no client when there is nothing to call. ``temperature`` is sent on
    the OpenRouter route only: newer Claude models reject the field (400), so the Anthropic route
    drops it with one printed warning per batch (as does the ``openai`` route)."""
    spend = spend or Spend()
    if not prompts:
        return spend
    if concurrency < 1 or rpm <= 0:
        raise JudgeConfigError(f"concurrency must be >= 1 and rpm > 0 (got {concurrency}, {rpm})")
    route_ = route(model)
    key = api_key(model)
    if temperature is not None and route_ == "anthropic":
        print(f"  llm: dropping temperature={temperature} for {model} (Anthropic route rejects it)")
        temperature = None
    if temperature is not None and route_ == "openai":
        print(f"  llm: dropping temperature={temperature} for {model} (OpenAI reasoning models)")
        temperature = None
    if max_tokens is None:
        max_tokens = _DEFAULT_MAX_TOKENS[route_]
    if route_ != "anthropic" and (cap := openrouter_max_out()) is not None:
        # the sweep's per-model output budget, up AND down: a reasoning judge at the default
        # 8 000 is cut off mid-JSON (smoke, 2026-11-01); only tokens used are billed
        max_tokens = cap
    client = _make_client(route_, key)
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int, system: str, user: str) -> tuple[int, dict[str, Any] | None]:
        async with sem:
            res = await _one(
                route_,
                client,
                system,
                user,
                schema=schema,
                model=model,
                reasoning=reasoning,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                rpm=rpm,
                spend=spend,
                mark_refusals=mark_refusals,
                attempts=attempts,
                fallbacks=fallbacks,
            )
        return i, res

    tasks = [asyncio.ensure_future(one(i, s, u)) for i, (s, u) in enumerate(prompts)]
    try:
        for fut in asyncio.as_completed(tasks):
            i, res = await fut
            on_result(i, res)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
    return spend


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def stream_json(
    prompts: list[tuple[str, str]],
    *,
    schema: dict[str, Any],
    model: str,
    on_result: Callable[[int, dict[str, Any] | None], None],
    reasoning: dict[str, Any] | None = None,
    temperature: float | None = None,
    concurrency: int = 64,
    rpm: float = 240.0,
    max_tokens: int | None = None,
    timeout: float = 180.0,
    spend: Spend | None = None,
    mark_refusals: bool = False,
    attempts: int | None = None,
    fallbacks: str | None = None,
) -> Spend:
    """Synchronous wrapper around :func:`stream_json_async` for the CLI and tests."""
    if _in_running_loop():
        raise JudgeConfigError(
            "stream_json called inside a running event loop; await stream_json_async"
        )
    return asyncio.run(
        stream_json_async(
            prompts,
            schema=schema,
            model=model,
            on_result=on_result,
            reasoning=reasoning,
            temperature=temperature,
            concurrency=concurrency,
            rpm=rpm,
            max_tokens=max_tokens,
            timeout=timeout,
            spend=spend,
            mark_refusals=mark_refusals,
            attempts=attempts,
            fallbacks=fallbacks,
        )
    )


def count_tokens(
    model: str, prompts: list[tuple[str, str]], concurrency: int = 8
) -> list[int | None]:
    """Real input-token counts of ``(system, user)`` pairs (``messages.count_tokens``, Anthropic
    route only; ``None`` where a count failed). Used by the summary-mode digest to decide whether a
    bundle fits the context window (plan 0021)."""
    if route(model) != "anthropic":
        raise JudgeConfigError(f"count_tokens needs an Anthropic model, got {model}")
    if not prompts:
        return []
    client = _make_client("anthropic", api_key(model))
    sem = asyncio.Semaphore(concurrency)

    async def one(system: str, user: str) -> int | None:
        system = system.encode("utf-8", "replace").decode("utf-8")
        user = user.encode("utf-8", "replace").decode("utf-8")
        async with sem:
            for attempt in range(_ATTEMPTS):
                try:
                    r = await client.messages.count_tokens(
                        model=model, system=system, messages=[{"role": "user", "content": user}]
                    )
                    return int(r.input_tokens)
                except Exception as e:
                    status = getattr(e, "status_code", None)
                    if status in _TRANSIENT and attempt < _ATTEMPTS - 1:
                        await _sleep(_backoff(attempt, status))
                        continue
                    print(f"  count_tokens error: {type(e).__name__}: {str(e)[:200]}")
                    return None
        return None

    async def run() -> list[int | None]:
        try:
            return list(await asyncio.gather(*(one(s, u) for s, u in prompts)))
        finally:
            await client.close()

    if _in_running_loop():
        raise JudgeConfigError("count_tokens called inside a running event loop")
    return asyncio.run(run())


async def preflight_async(model: str, reasoning: dict[str, Any] | None) -> None:
    """One tiny structured call, so a bad key/model/route fails before a long run starts."""
    out: list[dict[str, Any] | None] = []
    schema = schema_block("t", {"a": {"type": "integer"}}, ["a"])
    await stream_json_async(
        [("Reply JSON.", 'Return {"a":1}')],
        schema=schema,
        model=model,
        reasoning=reasoning,
        concurrency=1,
        on_result=lambda _i, r: out.append(r),
    )
    if out != [{"a": 1}]:
        raise JudgeConfigError(f"preflight failed for {model}: {out}")


def preflight(model: str, reasoning: dict[str, Any] | None) -> None:
    if _in_running_loop():
        raise JudgeConfigError(
            "preflight called inside a running event loop; await preflight_async"
        )
    asyncio.run(preflight_async(model, reasoning))


# ---------------------------------------------------------------- free-text primitive
# Used by agentic_misalignment (three free-text stages, no schema). Anthropic route only:
# streaming (the SDK refuses non-streaming requests whose max_tokens implies a >10 min
# operation), per-call thinking on/off, and a budget-doubling retry when the text is empty and
# stop_reason == "max_tokens" (thinking ate the budget), up to _TEXT_BUDGET_CEILING.

_TEXT_BUDGET_CEILING = 64_000


async def _anthropic_text_once(
    client: Any,
    user: str,
    *,
    model: str,
    thinking: bool,
    max_tokens: int,
    timeout: float,
    spend: Spend,
    mark_refusals: bool = False,
) -> str | None:
    budget = max_tokens
    while True:
        if always_thinks(model):  # cannot disable: adaptive, low effort for "no thinking" stages
            think: dict[str, Any] = {"thinking": {"type": "adaptive"}}
            if not thinking:
                think["output_config"] = {"effort": "low"}
        else:
            think = {"thinking": {"type": "adaptive" if thinking else "disabled"}}
        async with client.messages.stream(
            timeout=timeout,
            model=model,
            max_tokens=budget,
            messages=[{"role": "user", "content": user}],
            **think,
        ) as stream:
            resp = await stream.get_final_message()
        spend.calls += 1
        usage = getattr(resp, "usage", None)
        if usage is not None:
            spend.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            spend.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            spend.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            spend.cache_write_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            spend.refusals += 1
            print(f"  llm refusal: {model}")
            return REFUSED if mark_refusals else None
        txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if not txt and stop == "max_tokens" and budget < _TEXT_BUDGET_CEILING:
            budget *= 2
            continue
        return txt


async def _one_text(
    client: Any,
    user: str,
    *,
    model: str,
    thinking: bool,
    max_tokens: int,
    timeout: float,
    rpm: float,
    spend: Spend,
    mark_refusals: bool = False,
) -> str | None:
    """Same backoff / fatal classification as :func:`_one`, for one free-text call."""
    user = user.encode("utf-8", "replace").decode("utf-8")
    for attempt in range(_ATTEMPTS):
        await _pace(rpm)
        try:
            return await _anthropic_text_once(
                client,
                user,
                model=model,
                thinking=thinking,
                max_tokens=max_tokens,
                timeout=timeout,
                spend=spend,
                mark_refusals=mark_refusals,
            )
        except Exception as e:
            name = type(e).__name__
            status = getattr(e, "status_code", None)
            if name in _FATAL or status in (401, 402, 403):
                raise JudgeConfigError(f"{name}: {str(e)[:300]}") from e
            transient = (
                status in _TRANSIENT
                or "Timeout" in name
                or "Connection" in name
                or "Deadline" in name
                or isinstance(e, json.JSONDecodeError | ValueError)
            )
            if transient and attempt < _ATTEMPTS - 1:
                spend.retries += 1
                if not isinstance(e, json.JSONDecodeError | ValueError):
                    _tally(spend.transient, _status_kind(e, status))
                await _sleep(_backoff(attempt, status))
                continue
            spend.errors += 1
            _tally(spend.failed, _status_kind(e, status))
            print(f"  llm error: {name}: {str(e)[:200]}")
            return None
    return None


async def stream_text_async(
    prompts: list[str],
    *,
    model: str,
    on_result: Callable[[int, str | None], None],
    thinking: bool,
    max_tokens: int,
    concurrency: int = 64,
    rpm: float = 240.0,
    timeout: float = 600.0,
    spend: Spend | None = None,
    mark_refusals: bool = False,
) -> Spend:
    """Run user-only free-text prompts concurrently on the Anthropic route (no system block,
    no schema), handing each stripped text (or ``None`` = exhausted retries / non-transient
    error / refusal; with ``mark_refusals`` a refusal is :data:`REFUSED` instead) to
    ``on_result(index, text)`` as it lands. ``""`` is a valid result (the
    source returns it after the 64k budget ceiling). Any non-``claude-*`` model raises
    :class:`JudgeConfigError`. Builds no client when there is nothing to call."""
    spend = spend or Spend()
    if not prompts:
        return spend
    if concurrency < 1 or rpm <= 0:
        raise JudgeConfigError(f"concurrency must be >= 1 and rpm > 0 (got {concurrency}, {rpm})")
    if route(model) != "anthropic":
        raise JudgeConfigError(f"free-text stage requires a claude-* model (got {model})")
    client = _make_client("anthropic", api_key(model))
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int, user: str) -> tuple[int, str | None]:
        async with sem:
            res = await _one_text(
                client,
                user,
                model=model,
                thinking=thinking,
                max_tokens=max_tokens,
                timeout=timeout,
                rpm=rpm,
                spend=spend,
                mark_refusals=mark_refusals,
            )
        return i, res

    tasks = [asyncio.ensure_future(one(i, u)) for i, u in enumerate(prompts)]
    try:
        for fut in asyncio.as_completed(tasks):
            i, res = await fut
            on_result(i, res)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
    return spend


def stream_text(
    prompts: list[str],
    *,
    model: str,
    on_result: Callable[[int, str | None], None],
    thinking: bool,
    max_tokens: int,
    concurrency: int = 64,
    rpm: float = 240.0,
    timeout: float = 600.0,
    spend: Spend | None = None,
    mark_refusals: bool = False,
) -> Spend:
    """Synchronous wrapper around :func:`stream_text_async` for the CLI and tests."""
    if _in_running_loop():
        raise JudgeConfigError(
            "stream_text called inside a running event loop; await stream_text_async"
        )
    return asyncio.run(
        stream_text_async(
            prompts,
            model=model,
            on_result=on_result,
            thinking=thinking,
            max_tokens=max_tokens,
            concurrency=concurrency,
            rpm=rpm,
            timeout=timeout,
            spend=spend,
            mark_refusals=mark_refusals,
        )
    )
