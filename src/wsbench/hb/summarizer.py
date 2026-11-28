"""The token-bag -> prose step for token-lens readouts (``interp-v1``, verbatim prompt; plan
0121 adds the registered ``interp-v2``, see :data:`VERSIONS`).

Ported from WorkspaceBench ``src/wsbench/summarizer.py`` @ 92d763e (MIT): the prompt, schema,
``render_user``, ``render_bag`` and the result reader. The cached call itself lives in the eval
pipeline (``evals/wsbench_chat/run.py``), keyed ``summ:<cell key>`` with the fingerprint
``(SUMMARIZER_PROMPT_VERSION, model, effort, bundle text)``.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from wsbench.hb.llm import schema_block

SUMMARIZER_PROMPT_VERSION = "interp-v1"

INTERP_SYSTEM = """You are shown the top-10 token readouts from an interpretability lens at one \
position inside a language model that was reading a passage you cannot see. Tokens may include \
noise, fragments, other languages (translate them), or byte artifacts. In one or two sentences, \
state what these outputs are collectively trying to say — the situation or mental content they \
point to. Commit to the most specific reading the tokens support; do not just say they are noisy."""
INTERP_SCHEMA = schema_block("interp", {"interpretation": {"type": "string"}}, ["interpretation"])
INTERP_USER = "TOKEN READOUTS:\n{txt}"

PROMPTS: dict[str, str] = {"INTERP_SYSTEM": INTERP_SYSTEM, "INTERP_USER": INTERP_USER}


def render_user(txt: str) -> str:
    return INTERP_USER.replace("{txt}", txt)


def render_bag(tokens: Sequence[str], scores: Sequence[float] | None) -> str:
    """``tok (score)`` list, best first, scores to 2 dp; without scores the tokens joined by
    ``" | "``."""
    if scores is None:
        return " | ".join(tokens)
    pairs = sorted(zip(tokens, scores, strict=True), key=lambda p: -p[1])
    return " | ".join(f"{t} ({s:.2f})" for t, s in pairs)


def interpretation_of(r: object) -> str | None:
    if not isinstance(r, dict):
        return None
    t = r.get("interpretation")
    if not isinstance(t, str) or not t.strip():
        return None
    return t


# ---------------------------------------------------------------- versions (plan 0121 §2)

V1 = SUMMARIZER_PROMPT_VERSION
V2 = "interp-v2"
# plan 0121 §2.1: byte-identical to the text that passed the A/B gate
# (scripts/hb/v9/gates/interp.py INTERP2_SYSTEM; pinned in tests/hb/test_v9_summarizer.py)
INTERP2_SYSTEM = """You are shown the top-10 tokens an interpretability lens read at one position \
inside a language model that was reading a passage you cannot see. Decide what, if anything, \
these tokens say.

Many tokens carry no meaning and must be ignored: word fragments, single letters, punctuation, \
formatting and byte artifacts, and a token in another language or script that is not a readable \
word or phrase.

A meaningful word in another language counts for its meaning only. The lens emits Chinese and \
other non-English tokens whatever the passage is, so do not name a token's language and never \
conclude anything about the passage's language, or about anyone's nationality or location, from \
the language or script of the tokens.

State in one or two sentences what the meaningful tokens point to: the situation or content, as \
specific as the tokens support and no more specific. If the real words share no topic, do not \
force one: answer "The tokens include the unrelated words X, Y, Z." and nothing more.

Answer junk = true only when the tokens are obviously not words at all: every one of them is a \
fragment, a single letter, punctuation, formatting or a byte artifact. If even one token is a \
real word, name, number or emoji in any language, junk = false."""
INTERP2_SCHEMA = schema_block(
    "interp2",
    {"junk": {"type": "boolean"}, "interpretation": {"type": "string"}},
    ["junk", "interpretation"],
)

TEXT, JUNK, INVALID = "text", "junk", "invalid"


@dataclass(frozen=True)
class Reading:
    """What a summariser answer says: ``text`` (a summary, in ``text``), ``junk`` (a settled
    answer that says nothing: the tokens are not words) or ``invalid`` (no usable answer)."""

    kind: str
    text: str | None = None

    @property
    def valid(self) -> bool:
        return self.kind != INVALID


def read_v1(r: object) -> Reading:
    t = interpretation_of(r)
    return Reading(INVALID) if t is None else Reading(TEXT, t)


def read_v2(r: object) -> Reading:
    """``junk = true`` is junk whatever the text; ``junk = false`` with a non-empty
    interpretation is a summary; anything else is invalid."""
    if not isinstance(r, dict) or not isinstance(r.get("junk"), bool):
        return Reading(INVALID)
    if r["junk"]:
        return Reading(JUNK)
    t = r.get("interpretation")
    if not isinstance(t, str) or not t.strip():
        return Reading(INVALID)
    return Reading(TEXT, t)


@dataclass(frozen=True)
class Spec:
    version: str
    system: str
    user: str  # the user template (``{txt}`` = the rendered bag)
    schema: dict[str, Any]
    read: Callable[[object], Reading]

    def render_user(self, txt: str) -> str:
        return self.user.replace("{txt}", txt)


VERSIONS: dict[str, Spec] = {
    V1: Spec(V1, INTERP_SYSTEM, INTERP_USER, INTERP_SCHEMA, read_v1),
    V2: Spec(V2, INTERP2_SYSTEM, INTERP_USER, INTERP2_SCHEMA, read_v2),
}


def spec(version: str | None = None) -> Spec:
    """The registered summariser ``version`` (``None`` = ``interp-v1``); unknown: ValueError."""
    v = version or V1
    if v not in VERSIONS:
        raise ValueError(f"unknown summarizer prompt {v!r} (known: {', '.join(VERSIONS)})")
    return VERSIONS[v]
