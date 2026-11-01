"""Per-family judge configuration and the flag > env > family-pin override resolution."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from wsbench.llm import route

DEFAULT_JUDGE = "claude-opus-5-5"  # plan 0023 (Camila 2026-10-29)
DEFAULT_REASONING = {"effort": "minimal"}  # OpenRouter judges: Gemini cannot turn reasoning off
ANTHROPIC_REASONING_KEYS = frozenset({"anthropic_effort"})  # every other key is OpenRouter's
ENV_OVERRIDE = "WSBENCH_JUDGE_MODEL"
# plan 0059: ``judge_effort=`` (OpenRouter judges only): replaces the reasoning resolve would give
JUDGE_EFFORTS = ("default", "minimal", "low", "medium", "high")


@dataclass(frozen=True)
class JudgeConfig:
    model: str = DEFAULT_JUDGE
    prompt_version: str = "v1"
    reasoning: dict | None = None  # route-keyed; see resolve()
    aux_models: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedJudge:
    model: str
    reasoning: dict | None
    pinned: bool  # True iff model == config.model (no override took effect)
    source: Literal["flag", "env", "family"]


def resolve(
    config: JudgeConfig,
    *,
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
    effort: str | None = None,
) -> ResolvedJudge:
    """The judge of a family: model flag > env > family pin. ``effort`` (``judge_effort=``, plan
    0059), when given, REPLACES the OpenRouter reasoning this would produce (the family's keys
    and :data:`DEFAULT_REASONING` alike): ``default`` -> ``None``, else ``{"effort": effort}``,
    and the judge is no longer the pinned instrument. A ``claude-*`` judge with ``effort``, or an
    unknown level, exits 2. The ``openai`` route (``WSBENCH_OPENAI_ROUTE=wif``, plan 0059
    addendum A) is non-Anthropic: it resolves exactly like OpenRouter (its ``effort`` becomes the
    request's ``reasoning_effort``)."""
    env = env or {}
    env_model = env.get(ENV_OVERRIDE, "")
    if flag:
        model, source = flag, "flag"
    elif env_model:
        model, source = env_model, "env"
    else:
        model, source = config.model, "family"
    # a family's reasoning keys are route-specific: `anthropic_effort` rides the Anthropic route,
    # every other key the OpenRouter one; keys for the other route are dropped, so an Opus judge
    # of a family with an OpenRouter-only setting resolves to None (plan 0023)
    is_anthropic = route(model) == "anthropic"
    kept = {
        k: v
        for k, v in (config.reasoning or {}).items()
        if (k in ANTHROPIC_REASONING_KEYS) == is_anthropic
    }
    reasoning: dict | None = kept or (None if is_anthropic else dict(DEFAULT_REASONING))
    pinned = model == config.model
    if effort is not None:
        from wsbench.family import fail  # wsbench.family imports this module

        if effort not in JUDGE_EFFORTS:
            fail(f"judge_effort={effort!r}: expected one of {', '.join(JUDGE_EFFORTS)}")
        if is_anthropic:
            fail(f"judge_effort={effort!r} is for OpenRouter judges; {model} is a claude-* judge")
        reasoning = None if effort == "default" else {"effort": effort}
        pinned = False
    return ResolvedJudge(model=model, reasoning=reasoning, pinned=pinned, source=source)
