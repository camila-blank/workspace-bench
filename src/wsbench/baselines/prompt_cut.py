"""The prompt/reply boundary of a rendered read: a prompt-only reader never sees a reply
(plan 0069). Assistant turns are found in token ids, from markers probed off the subject's
own chat template."""

from dataclasses import dataclass
from typing import Any

_PROBE_USER, _PROBE_REPLY, _PROBE_NEXT = "Probe question one", "Probe reply text", "Probe two"


@dataclass(frozen=True)
class TurnMarks:
    """Token markers of a chat template: ``header`` opens an assistant turn, ``think`` is the
    fixed rest of the generation prompt (Qwen: the empty think block; may be empty), ``end``
    closes an assistant turn's content (may be empty) and ``user`` opens a user turn."""

    header: tuple[int, ...]
    think: tuple[int, ...]
    end: tuple[int, ...]
    user: tuple[int, ...]

    @property
    def gen(self) -> tuple[int, ...]:
        return self.header + self.think


def _ids(tok: Any, messages: list[dict[str, str]], gen: bool) -> list[int]:
    out = tok.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=gen, enable_thinking=False
    )
    return [int(i) for i in (out["input_ids"] if hasattr(out, "keys") else out)]


def _find(
    seq: list[int] | tuple[int, ...], sub: tuple[int, ...] | list[int], start: int = 0
) -> int:
    sub = list(sub)
    n = len(sub)
    for i in range(start, len(seq) - n + 1):
        if list(seq[i : i + n]) == sub:
            return i
    return -1


def _fail(why: str) -> SystemExit:
    return SystemExit(
        f"prompt_cut: cannot find the assistant turn markers of this chat template ({why})"
    )


def turn_marks(tok: Any) -> TurnMarks:
    """Probe ``tok``'s chat template (thinking off, as every chat render is built) for its turn
    markers; a template whose renders are not prefixes of each other exits loudly."""
    if not getattr(tok, "chat_template", None):
        raise _fail("the tokenizer has no chat template")
    u = [{"role": "user", "content": _PROBE_USER}]
    ua = [*u, {"role": "assistant", "content": _PROBE_REPLY}]
    p0, pg = _ids(tok, u, False), _ids(tok, u, True)
    three = _ids(tok, [*ua, {"role": "user", "content": _PROBE_NEXT}], True)
    if pg[: len(p0)] != p0 or three[: len(p0)] != p0:
        raise _fail("the one-turn render is not a prefix of the longer renders")
    gen = pg[len(p0) :]
    reply = tok(_PROBE_REPLY, add_special_tokens=False)["input_ids"]
    at = _find(three, reply, len(p0))
    if at < 0:
        raise _fail("the probe reply is not tokenized on its own inside the template")
    header = three[len(p0) : at]
    if not header or gen[: len(header)] != header:
        raise _fail("the earlier assistant turn's header is not the generation prompt's start")
    think = gen[len(header) :]
    nxt = tok(_PROBE_NEXT, add_special_tokens=False)["input_ids"]
    at2 = _find(three, nxt, at + len(reply))
    if at2 < 0 or three[len(three) - len(gen) :] != gen:
        raise _fail("the second user turn or the closing generation prompt is not found")
    between = three[at + len(reply) : at2]  # the assistant turn's end + the next user header
    q = tok(_PROBE_USER, add_special_tokens=False)["input_ids"]
    aq = _find(p0, q)
    if aq < 0:
        raise _fail("the probe question is not tokenized on its own inside the template")
    # the user header: the longest common suffix of what precedes each user turn's text
    pre, k = p0[:aq], 0
    while (
        k < min(len(pre), len(between)) and pre[len(pre) - 1 - k] == between[len(between) - 1 - k]
    ):
        k += 1
    if not k:
        raise _fail("the two user turns share no opening tokens")
    user, end = between[len(between) - k :], between[: len(between) - k]
    return TurnMarks(tuple(header), tuple(think), tuple(end), tuple(user))


@dataclass(frozen=True)
class Turn:
    """One assistant turn in a render: ``start`` = its header, ``content`` = the first token after
    the header (and after the fixed think block when it follows), ``stop`` = one past its last
    content token, ``close`` = one past its end marker (the next turn's start, or the render
    end)."""

    start: int
    content: int
    stop: int
    close: int


def assistant_turns(ids: list[int], m: TurnMarks) -> list[Turn]:
    """Every assistant turn of ``ids`` (a full render or a cut prefix of one)."""
    out = []
    i = _find(ids, m.header)
    while i >= 0:
        c = i + len(m.header)
        if m.think and list(ids[c : c + len(m.think)]) == list(m.think):
            c += len(m.think)
        elif m.think and list(m.think[: len(ids) - c]) == list(ids[c:]):
            c = len(ids)  # cut inside the fixed think block
        nexts = [j for j in (_find(ids, m.header, c), _find(ids, m.user, c)) if j >= 0]
        nxt = min(nexts) if nexts else len(ids)
        e = _find(ids[:nxt], m.end, c) if m.end else -1
        stop = e if e >= 0 else nxt
        close = (e + len(m.end)) if e >= 0 else nxt
        out.append(Turn(i, c, stop, max(close, stop)))
        i = _find(ids, m.header, max(c, close))
    return out


def has_content(ids: list[int], t: Turn, tok: Any) -> bool:
    """True when the turn holds reply text: any content token that is not whitespace."""
    return bool(t.stop > t.content and tok.decode(list(ids[t.content : t.stop])).strip())


def reply_tokens(ids: list[int], m: TurnMarks, tok: Any) -> int:
    """How many reply-content tokens ``ids`` holds (0 = prompt only)."""
    return sum(t.stop - t.content for t in assistant_turns(ids, m) if has_content(ids, t, tok))


def prompt_only_ids(ids: list[int], last: int, m: TurnMarks, tok: Any) -> list[int]:
    """``ids`` through ``last`` with no reply in it. Unchanged when no assistant turn before the cut
    holds text; else every such turn is dropped whole and, when ``last`` lies inside one, the
    prefix ends at the generation prompt (assistant header + fixed think block) that opens it."""
    cut = list(ids[: last + 1])
    turns = [t for t in assistant_turns(cut, m) if has_content(cut, t, tok)]
    if not turns:
        return cut
    out: list[int] = []
    pos = 0
    for t in turns:
        out += cut[pos : t.start]
        if t.close >= len(cut):  # the cut ends inside this turn (or at its very end)
            out += list(m.gen)
            pos = len(cut)
            break
        pos = t.close
    out += cut[pos:]
    assert reply_tokens(out, m, tok) == 0, "prompt_only_ids left reply text"
    return out


def assert_no_reply(ids: list[int], m: TurnMarks, tok: Any, where: str = "") -> None:
    """Raise when ``ids`` hold any reply text (Camila 2026-11-04: the control NEVER reads a
    reply)."""
    n = reply_tokens(ids, m, tok)
    if n:
        raise AssertionError(f"prompt-only input holds {n} reply tokens {where}".rstrip())
