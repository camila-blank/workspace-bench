"""Plan 0059 `llm` changes: token / cache-token tallies, the OpenRouter output cap and overflow
classifier, OpenRouter refusals, 200-body errors, the ``transient`` / ``failed`` tallies, and the
addendum A ``openai`` (WIF) route."""

from types import SimpleNamespace
from typing import Any

import pytest

from wsbench import llm
from wsbench.llm import JudgeConfigError, Spend, schema_block, stream_json, stream_text

SCHEMA = schema_block("t", {"a": {"type": "integer"}}, ["a"])
OR_MODEL = "deepseek/deepseek-v4-flash"
CLAUDE = "claude-sonnet-5"


class RateLimitError(Exception):
    status_code = 429


class BadRequestError(Exception):
    status_code = 400


class PermissionDeniedError(Exception):
    """Named as the openai SDK class (in ``llm._FATAL``): a 403 that would raise today."""

    status_code = 403

    def __init__(self, message: str, body: object = None) -> None:
        super().__init__(message)
        self.body = body


def _or(content: str = '{"a": 1}', **kw: Any) -> Any:
    usage = kw.pop("usage", SimpleNamespace(cost=0.01))
    msg = SimpleNamespace(content=content, refusal=kw.pop("refusal", None))
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=kw.pop("finish_reason", "stop"))],
        usage=usage,
    )


def _body_error(code: object, message: str = "upstream error") -> Any:
    return SimpleNamespace(choices=[], error={"code": code, "message": message}, usage=None)


class FakeOpenRouter:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    async def close(self) -> None:
        return None


class FakeAnthropic:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw: Any) -> Any:
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    async def close(self) -> None:
        return None


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv(llm.MAX_OUT_ENV, raising=False)
    monkeypatch.setattr(llm, "_backoff", lambda attempt, status: 0.0)

    async def no_pace(rpm: float) -> None:
        return None

    monkeypatch.setattr(llm, "_pace", no_pace)


def _run(monkeypatch, fake: Any, model: str = OR_MODEL, **kw: Any) -> tuple[list, Spend]:
    monkeypatch.setattr(llm, "_make_client", lambda route, key: fake)
    got: list[Any] = []
    spend = stream_json(
        [("s", "u")], schema=SCHEMA, model=model, on_result=lambda i, r: got.append(r), **kw
    )
    return got, spend


# ---------------------------------------------------------------- tokens


def test_openrouter_usage_tallies_tokens(env, monkeypatch):
    usage = SimpleNamespace(cost=0.02, prompt_tokens=120, completion_tokens=30)
    got, spend = _run(monkeypatch, FakeOpenRouter([_or(usage=usage)]))
    assert got == [{"a": 1}]
    assert (spend.input_tokens, spend.output_tokens, spend.usd) == (120, 30, 0.02)
    assert (spend.cache_read_tokens, spend.cache_write_tokens) == (0, 0)


def test_openrouter_usage_without_token_fields_tallies_zero(env, monkeypatch):
    _got, spend = _run(monkeypatch, FakeOpenRouter([_or()]))  # usage = {cost} only
    assert (spend.input_tokens, spend.output_tokens) == (0, 0)


def test_anthropic_cache_usage_tallies_cache_tokens(env, monkeypatch):
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=4,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=50,
    )
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"a": 1}')],
        usage=usage,
    )
    got, spend = _run(monkeypatch, FakeAnthropic([resp]), model=CLAUDE)
    assert got == [{"a": 1}]
    assert (spend.input_tokens, spend.output_tokens) == (10, 4)
    assert (spend.cache_read_tokens, spend.cache_write_tokens) == (900, 50)


def test_anthropic_usage_without_cache_fields_tallies_zero(env, monkeypatch):
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"a": 1}')],
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
    )
    _got, spend = _run(monkeypatch, FakeAnthropic([resp]), model=CLAUDE)
    assert (spend.cache_read_tokens, spend.cache_write_tokens) == (0, 0)


def test_spend_report_unchanged_by_new_fields():
    s = Spend(usd=1.0, calls=2, cache_read_tokens=5, transient={"429": 1}, failed={"500": 1})
    assert s.report() == "calls=2 retries=0 errors=0 refusals=0 spend=$1.00"


# ---------------------------------------------------------------- output cap / overflow


def test_max_out_env_sets_the_openrouter_budget_only(env, monkeypatch):
    monkeypatch.setenv(llm.MAX_OUT_ENV, "500")
    fake = FakeOpenRouter([_or(), _or()])
    _run(monkeypatch, fake)
    _run(monkeypatch, fake, max_tokens=300)
    # the budget replaces the request both ways (a reasoning judge at 300 would be cut off)
    assert [c["max_tokens"] for c in fake.calls] == [500, 500]
    calls: list[dict] = []

    class Anth(FakeAnthropic):
        async def _create(self, **kw: Any) -> Any:
            calls.append(kw)
            return await super()._create(**kw)

    resp = SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"a": 1}')]
    )
    _run(monkeypatch, Anth([resp]), model=CLAUDE)
    assert calls[0]["max_tokens"] == llm._DEFAULT_MAX_TOKENS["anthropic"]  # never capped


def test_max_out_env_bad_value_is_config_error(env, monkeypatch):
    monkeypatch.setenv(llm.MAX_OUT_ENV, "zero")
    with pytest.raises(JudgeConfigError):
        _run(monkeypatch, FakeOpenRouter([_or()]))


@pytest.mark.parametrize("phrase", llm.OPENROUTER_TOO_LONG)
def test_openrouter_context_length_400_is_too_long(env, monkeypatch, phrase):
    assert llm.OPENROUTER_TOO_LONG == (
        "maximum context length",
        "context length",
        "context window",
        "prompt is too long",
    )
    err = BadRequestError(f"Error code: 400 - This endpoint's {phrase} is 131072 tokens")
    # without the output cap only "prompt is too long" is too-long (a "maximum context length"
    # 400 can then be the output request; review of #217)
    got, _spend = _run(monkeypatch, FakeOpenRouter([err]), mark_refusals=True)
    assert got == ([llm.TOO_LONG_JSON] if phrase == "prompt is too long" else [None])
    monkeypatch.setenv(llm.MAX_OUT_ENV, "8000")
    got, spend = _run(monkeypatch, FakeOpenRouter([err]), mark_refusals=True)
    assert got == [llm.TOO_LONG_JSON] and spend.errors == 1 and spend.retries == 0
    got, _spend = _run(monkeypatch, FakeOpenRouter([err]))  # without mark_refusals: None
    assert got == [None]


def test_openrouter_max_tokens_400_is_a_plain_error(env, monkeypatch):
    err = BadRequestError("Error code: 400 - too many tokens: max_tokens exceeds the limit")
    got, spend = _run(monkeypatch, FakeOpenRouter([err]), mark_refusals=True)
    assert got == [None] and spend.errors == 1 and spend.failed == {"400": 1}


# ---------------------------------------------------------------- refusals / length


@pytest.mark.parametrize(
    "resp",
    [
        _or("", refusal="I can't help with that"),
        _or('{"a": 1}', finish_reason="content_filter"),
    ],
)
def test_openrouter_refusal_is_non_transient(env, monkeypatch, resp):
    fake = FakeOpenRouter([resp])
    got, spend = _run(monkeypatch, fake, mark_refusals=True)
    assert got == [llm.REFUSED_JSON] and spend.refusals == 1 and spend.retries == 0
    assert len(fake.calls) == 1
    got, spend = _run(monkeypatch, FakeOpenRouter([resp]))
    assert got == [None] and spend.refusals == 1


def test_openrouter_length_cut_json_is_one_failed_call(env, monkeypatch):
    fake = FakeOpenRouter([_or('{"a": ', finish_reason="length")])
    got, spend = _run(monkeypatch, fake)
    assert got == [None] and len(fake.calls) == 1 and spend.retries == 0
    assert spend.errors == 1 and spend.failed == {"length": 1}


def test_openrouter_length_with_valid_json_is_used(env, monkeypatch):
    got, _spend = _run(monkeypatch, FakeOpenRouter([_or('{"a": 1}', finish_reason="length")]))
    assert got == [{"a": 1}]


def test_garbled_body_is_still_a_parse_retry_and_not_tallied(env, monkeypatch):
    got, spend = _run(monkeypatch, FakeOpenRouter([_or("not json"), _or()]))
    assert got == [{"a": 1}] and spend.retries == 1
    assert spend.transient == {} and spend.failed == {}


def test_moderation_403_is_a_refusal_not_a_config_error(env, monkeypatch):
    body = {"error": {"code": 403, "message": "flagged", "metadata": {"reasons": ["violence"]}}}
    err = PermissionDeniedError("Error code: 403 - moderation", body=body)
    assert type(err).__name__ in llm._FATAL  # the check must run BEFORE the fatal one
    got, spend = _run(monkeypatch, FakeOpenRouter([err]), mark_refusals=True)
    assert got == [llm.REFUSED_JSON] and spend.refusals == 1 and spend.errors == 0
    msg_only = PermissionDeniedError("Error code: 403 - Your input was flagged for harassment")
    got, spend = _run(monkeypatch, FakeOpenRouter([msg_only]))
    assert got == [None] and spend.refusals == 1


def test_other_403_stays_fatal(env, monkeypatch):
    err = PermissionDeniedError("Error code: 403 - key not allowed", body={"message": "no"})
    with pytest.raises(JudgeConfigError):
        _run(monkeypatch, FakeOpenRouter([err]))


def test_anthropic_403_with_moderation_words_stays_fatal(env, monkeypatch):
    err = PermissionDeniedError("flagged", body={"metadata": {"reasons": ["x"]}})
    with pytest.raises(JudgeConfigError):
        _run(monkeypatch, FakeAnthropic([err]), model=CLAUDE)


# ---------------------------------------------------------------- transient / failed tallies


def test_recovered_429_is_transient_not_failed(env, monkeypatch):
    got, spend = _run(monkeypatch, FakeOpenRouter([RateLimitError("slow"), _or()]))
    assert got == [{"a": 1}] and spend.transient == {"429": 1} and spend.failed == {}
    assert spend.throttled() == 0


def test_exhausted_429_ends_failed(env, monkeypatch):
    fake = FakeOpenRouter([RateLimitError("slow") for _ in range(llm._ATTEMPTS)])
    got, spend = _run(monkeypatch, fake)
    assert got == [None] and spend.errors == 1
    assert spend.transient == {"429": llm._ATTEMPTS - 1} and spend.failed == {"429": 1}
    assert spend.throttled() == 1


def test_timeout_and_connection_kinds(env, monkeypatch):
    class APITimeoutError(Exception):
        status_code = None

    class APIConnectionError(Exception):
        status_code = None

    got, spend = _run(
        monkeypatch, FakeOpenRouter([APITimeoutError("t"), APIConnectionError("c"), _or()])
    )
    assert got == [{"a": 1}] and spend.transient == {"timeout": 1, "connection": 1}


def test_200_body_error_code_is_classified(env, monkeypatch):
    # a body-delivered 429 retries like an HTTP 429 and is tallied (it was a parse retry)
    got, spend = _run(monkeypatch, FakeOpenRouter([_body_error(429, "rate limited"), _or()]))
    assert got == [{"a": 1}] and spend.transient == {"429": 1} and spend.failed == {}
    fake = FakeOpenRouter([_body_error("429") for _ in range(llm._ATTEMPTS)])
    got, spend = _run(monkeypatch, fake)
    assert got == [None] and spend.failed == {"429": 1} and spend.throttled() == 1
    # a non-numeric code keeps the old retry; still failing at the end it is throttled
    fake = FakeOpenRouter([_body_error("quota", "daily quota") for _ in range(llm._ATTEMPTS)])
    got, spend = _run(monkeypatch, fake)
    assert got == [None] and spend.failed == {"quota": 1} and spend.throttled() == 1
    # a plain body 400 keeps the old retry too
    got, spend = _run(monkeypatch, FakeOpenRouter([_body_error(400, "upstream error"), _or()]))
    assert got == [{"a": 1}] and spend.retries == 1
    # a code-less rate-limit message reads as 429
    got, spend = _run(
        monkeypatch, FakeOpenRouter([_body_error(None, "Rate limit exceeded"), _or()])
    )
    assert got == [{"a": 1}] and spend.transient == {"429": 1}
    # a context-length body error is too long under mark_refusals (with the output cap set)
    monkeypatch.setenv(llm.MAX_OUT_ENV, "8000")
    body = _body_error(400, "maximum context length is 131072 tokens")
    got, _spend = _run(monkeypatch, FakeOpenRouter([body]), mark_refusals=True)
    assert got == [llm.TOO_LONG_JSON]
    # a body with no error at all keeps the old parse retry (not tallied)
    empty = SimpleNamespace(choices=[], usage=None)
    got, spend = _run(monkeypatch, FakeOpenRouter([empty, _or()]))
    assert got == [{"a": 1}] and spend.retries == 1 and spend.transient == {}


def test_200_body_error_attribute_object_and_model_extra(env, monkeypatch):
    obj = SimpleNamespace(choices=[], error=SimpleNamespace(code=502, message="bad gateway"))
    extra = SimpleNamespace(choices=[], model_extra={"error": {"code": 503, "message": "x"}})
    got, spend = _run(monkeypatch, FakeOpenRouter([obj, extra, _or()]))
    assert got == [{"a": 1}] and spend.transient == {"502": 1, "503": 1}


def test_anthropic_stop_reason_errors_get_a_failed_key(env, monkeypatch):
    def resp(stop: str) -> Any:
        return SimpleNamespace(stop_reason=stop, content=[], usage=None)

    _got, spend = _run(monkeypatch, FakeAnthropic([resp("max_tokens")]), model=CLAUDE)
    assert spend.failed == {"max_tokens": 1}
    _got, spend = _run(
        monkeypatch, FakeAnthropic([resp("model_context_window_exceeded")]), model=CLAUDE
    )
    assert spend.failed == {"context_window": 1}


class _Stream:
    def __init__(self, out: Any):
        self.out = out

    async def __aenter__(self) -> "_Stream":
        if isinstance(self.out, BaseException):
            raise self.out
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self.out


def test_text_route_tallies_transient_failed_and_cache_tokens(env, monkeypatch):
    usage = SimpleNamespace(
        input_tokens=5, output_tokens=2, cache_read_input_tokens=3, cache_creation_input_tokens=1
    )
    ok = SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text="hi")], usage=usage
    )
    outcomes: list[Any] = [RateLimitError("slow"), ok]
    outcomes += [RateLimitError("slow") for _ in range(llm._ATTEMPTS)]
    client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **kw: _Stream(outcomes.pop(0))),
    )

    async def close() -> None:
        return None

    client.close = close
    monkeypatch.setattr(llm, "_make_client", lambda route, key: client)
    got: list[Any] = []
    spend = stream_text(
        ["u"], model=CLAUDE, on_result=lambda i, r: got.append(r), thinking=False, max_tokens=10
    )
    assert got == ["hi"] and spend.transient == {"429": 1} and spend.failed == {}
    assert (spend.cache_read_tokens, spend.cache_write_tokens) == (3, 1)
    spend = stream_text(
        ["u"], model=CLAUDE, on_result=lambda i, r: got.append(r), thinking=False, max_tokens=10
    )
    assert got[-1] is None and spend.failed == {"429": 1} and spend.throttled() == 1


# ---------------------------------------------------------------- openai route (addendum A)

WIF_MODEL = "openai/gpt-6-luna"
WIF_ENV = {
    "MODAL_IDENTITY_TOKEN": "jwt-identity",
    "OPENAI_IDENTITY_PROVIDER_ID_MODAL": "idp-1",
    "OPENAI_SERVICE_ACCOUNT_ID": "sa-1",
    "OPENAI_PROJECT_ID": "proj-1",
}


class AuthenticationError(Exception):
    """Named as the openai SDK class (in ``llm._FATAL``)."""

    status_code = 401


class _TokenPost:
    """The token endpoint: a fresh access token per exchange (``tok-1``, ``tok-2``, ...)."""

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, url: str, payload: dict) -> tuple[int, Any]:
        self.n += 1
        return 200, {"access_token": f"tok-{self.n}", "expires_in": 3600}


class FakeOpenAI:
    """One shared outcome script across every ``AsyncOpenAI`` the WIF clients build (one per
    access token); each call records the token it was made with."""

    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.built: list[tuple[str, str | None, dict[str, str]]] = []

    def factory(self, key: str, base_url: str | None, headers: dict[str, str]) -> Any:
        self.built.append((key, base_url, headers))

        async def create(**kw: Any) -> Any:
            self.calls.append({**kw, "_token": key})
            out = self.outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return out

        async def close() -> None:
            return None

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close
        )


@pytest.fixture
def wif(env, monkeypatch):
    monkeypatch.setenv(llm.OPENAI_ROUTE_ENV, "wif")


def _run_wif(monkeypatch, fake: FakeOpenAI, **kw: Any) -> tuple[list, Spend, Any]:
    from wsbench.baselines import ati_remote as ar

    post = _TokenPost()
    clients = ar.Clients(
        "openai-wif", env=WIF_ENV, factory=fake.factory, token=ar.WifToken(WIF_ENV, post=post)
    )
    routes: list[str] = []

    def make(route: str, key: str) -> Any:
        routes.append(route)
        return llm._WifClient(clients)

    monkeypatch.setattr(llm, "_make_client", make)
    got: list[Any] = []
    spend = stream_json(
        [("s", "u")], schema=SCHEMA, model=WIF_MODEL, on_result=lambda i, r: got.append(r), **kw
    )
    assert routes == ["openai"]
    return got, spend, post


def test_openai_route_only_with_the_env(monkeypatch):
    for m in (WIF_MODEL, "openai/gpt-6-astra", "openai/gpt-oss-120b", OR_MODEL):
        assert llm.route(m) == "openrouter"  # env unset: unchanged
    monkeypatch.setenv(llm.OPENAI_ROUTE_ENV, "wif")
    assert llm.route(WIF_MODEL) == llm.route("openai/gpt-5-nano") == "openai"
    assert llm.route("openai/gpt-oss-20b") == "openrouter"  # open weights: other providers
    assert llm.route(OR_MODEL) == "openrouter" and llm.route(CLAUDE) == "anthropic"
    assert llm.api_key(WIF_MODEL) == ""  # no key: the WIF client exchanges a token
    monkeypatch.setenv(llm.OPENAI_ROUTE_ENV, "yes")
    with pytest.raises(JudgeConfigError, match="expected 'wif'"):
        llm.route(WIF_MODEL)


def test_openai_route_request_shape(wif, monkeypatch):
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)  # OpenAI: no cost field
    fake = FakeOpenAI([_or(usage=usage), _or(usage=usage)])
    got, spend, _post = _run_wif(monkeypatch, fake, reasoning={"effort": "low"}, temperature=0.0)
    monkeypatch.setenv(llm.MAX_OUT_ENV, "900")
    _run_wif(monkeypatch, fake, reasoning=None)
    assert got == [{"a": 1}]
    first, second = fake.calls
    assert first["model"] == "gpt-6-luna"  # the openai/ prefix stripped
    assert first["max_completion_tokens"] == llm._DEFAULT_MAX_TOKENS["openai"]
    assert second["max_completion_tokens"] == 900  # the sweep output budget applies
    assert first["reasoning_effort"] == "low" and "reasoning_effort" not in second
    for c in fake.calls:
        assert not {"temperature", "extra_body", "max_tokens", "reasoning"} & set(c)
        assert c["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]
        assert c["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}
    assert fake.built[0] == ("tok-1", None, {"OpenAI-Project": "proj-1"})
    assert (spend.input_tokens, spend.output_tokens, spend.usd) == (120, 30, 0.0)


def test_openai_route_401_re_exchanges_then_succeeds(wif, monkeypatch):
    fake = FakeOpenAI([AuthenticationError("expired"), _or()])
    got, spend, post = _run_wif(monkeypatch, fake)
    assert got == [{"a": 1}] and post.n == 2
    assert [c["_token"] for c in fake.calls] == ["tok-1", "tok-2"]
    assert spend.calls == 1 and spend.retries == 0 and spend.failed == {}


def test_openai_route_repeated_401_is_fatal(wif, monkeypatch):
    from wsbench.baselines import ati_remote as ar

    n = ar.AUTH_REFRESHES + 1
    fake = FakeOpenAI([AuthenticationError("bad identity") for _ in range(n)])
    with pytest.raises(JudgeConfigError, match="AuthenticationError"):
        _run_wif(monkeypatch, fake)
    assert len(fake.calls) == n and len({c["_token"] for c in fake.calls}) == n


def test_openai_route_403_is_fatal_even_when_flagged(wif, monkeypatch):
    body = {"error": {"message": "flagged", "metadata": {"reasons": ["x"]}}}
    fake = FakeOpenAI([PermissionDeniedError("Your input was flagged", body)])
    with pytest.raises(JudgeConfigError):
        _run_wif(monkeypatch, fake)


def test_openai_route_refusal_and_length_rules_are_shared(wif, monkeypatch):
    fake = FakeOpenAI([_or(refusal="no"), _or('{"a":', finish_reason="length")])
    got, spend, _post = _run_wif(monkeypatch, fake, mark_refusals=True)
    got2, spend2, _post = _run_wif(monkeypatch, fake)
    assert got == [llm.REFUSED_JSON] and spend.refusals == 1
    assert got2 == [None] and spend2.failed == {"length": 1} and len(fake.calls) == 2


def test_openai_route_missing_wif_env_is_a_config_error(wif, monkeypatch):
    for k in WIF_ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(JudgeConfigError, match="openai-wif"):
        stream_json([("s", "u")], schema=SCHEMA, model=WIF_MODEL, on_result=lambda i, r: None)


def test_openai_route_judge_effort_and_digest_reasoning(monkeypatch):
    from wsbench import digest
    from wsbench.judge_config import DEFAULT_REASONING, JudgeConfig, resolve

    monkeypatch.setenv(llm.OPENAI_ROUTE_ENV, "wif")
    cfg = JudgeConfig(prompt_version="v", reasoning={"effort": "low", "anthropic_effort": "x"})
    r = resolve(cfg, flag=WIF_MODEL, env={}, effort="high")
    assert (r.reasoning, r.pinned) == ({"effort": "high"}, False)
    assert resolve(cfg, flag=WIF_MODEL, env={}).reasoning == {"effort": "low"}  # non-Anthropic
    assert resolve(JudgeConfig(), flag=WIF_MODEL, env={}).reasoning == DEFAULT_REASONING
    assert resolve(cfg, flag=WIF_MODEL, env={}, effort="default").reasoning is None
    assert digest.digest_reasoning(WIF_MODEL, "low") == {"effort": "low"}
    assert digest.digest_reasoning(WIF_MODEL, "default") is None
