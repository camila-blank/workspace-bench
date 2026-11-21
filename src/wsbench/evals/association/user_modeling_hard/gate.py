"""Verifying a pick of the user_modeling_hard judge without a lexicon: the quote is real, lies
in ONE statement and is not the user's own words (the blind quote check then reads that
statement); plus the non-gating ``broader_suspect`` flag."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from wsbench import digest, mc, nearquote
from wsbench.readouts import Cell

ECHO_MIN_CHARS = 20  # shorter quotes are never an echo (a clue word or two)
ECHO_RATIO = 0.8  # the longest block shared with the user turn covers this share of the quote
GATES = ("quote", "bullet", "echo", "desc_rejected")
RETRIED = frozenset({"quote", "bullet", "echo"})  # the gates that earn the one re-ask


@dataclass(frozen=True)
class Located:
    """Where a quote sits: ``gate`` (``""`` = it stands so far), the ONE statement it lies in
    (the readout's own text), whether it was located near-verbatim, whether that statement is a
    token list, and the quoted part the quote check is shown."""

    gate: str
    statement: str = ""
    near: bool = False
    tokens: bool = False
    shown: str = ""


def echoes(quote: str, chat: str) -> bool:
    """The quote repeats the user's own words: on ``digest.fold_quote`` forms, a quote of at
    least :data:`ECHO_MIN_CHARS` characters whose longest common block with the conversation
    covers at least :data:`ECHO_RATIO` of it."""
    fq, fc = digest.fold_quote(quote), digest.fold_quote(chat)
    if len(fq) < ECHO_MIN_CHARS or not fc:
        return False
    m = SequenceMatcher(None, fq, fc, autojunk=False).find_longest_match(0, len(fq), 0, len(fc))
    return m.size >= math.ceil(ECHO_RATIO * len(fq))


def locate(quote: str, lines: Sequence[digest.Line], cells: Sequence[Cell], chat: str) -> Located:
    """Steps 1-3 of the verification (plan 0106 §3.4). ``lines`` = the digest's evidence lines
    (summary) or ``digest.native_lines`` of the unit (native); ``chat`` = the conversation as
    the judge was shown it. A near-verbatim quote (``nearquote.close_lines``; prose only) is
    read through the line it came from: the statement is that line and so is the quoted part,
    never the judge's wording. The echo step is for prose statements only."""
    lines = list(lines)
    exact = digest.quote_ok(quote, lines)
    if not exact and not nearquote.close_lines(quote, lines):
        return Located("quote")
    statements = digest.statement_lines(lines, cells)
    if exact:
        if not digest.quote_ok(quote, statements):
            return Located("bullet")
        held = next((s for s in statements if digest.line_holds(quote, s)), None)
        if held is None:  # one word split across adjacent token positions: no line holds it
            return Located("", statement=quote, tokens=True, shown=quote)
        loc = Located("", statement=held.text, tokens=held.pieces is not None, shown=quote)
    else:
        close = nearquote.close_lines(quote, statements)
        if not close:
            return Located("bullet")
        loc = Located("", statement=close[0].text, near=True, shown=close[0].text)
    if not loc.tokens and echoes(loc.shown, chat):
        return Located("echo", statement=loc.statement, near=loc.near, shown=loc.shown)
    return loc


def broader_suspect(statement: str, part: Mapping[str, Any], broader: Mapping[str, Any]) -> bool:
    """A statement that holds a broader term of the part's type (``broader_by_type``,
    word-bounded) and neither the gold nor any of its forms. Never gating: it steers the hand
    read toward passes that may rest on a broader class."""
    terms = [str(t) for t in broader.get(str(part.get("type")), ())]
    if not terms or not mc.form_in(statement, terms):
        return False
    return not mc.form_in(statement, [str(part["gold"]), *map(str, part.get("forms") or ())])
