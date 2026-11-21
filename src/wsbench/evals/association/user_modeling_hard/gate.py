"""Verifying a pick of the user_modeling_hard judge without a lexicon: the quote is real, lies
in ONE statement and is not the user's own words (the blind quote check then reads that
statement); plus the non-gating ``broader_suspect`` flag."""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from wsbench import digest, mc, nearquote
from wsbench.readouts import Cell

ECHO_MIN_CHARS = 20  # a shorter text is never an echo by itself (a clue word or two)
ECHO_RATIO = 0.8  # the share of a text that is the user turn's (longest block, or content words)
ECHO_MIN_WORDS = 3  # the word rule needs this many content words
MAX_STATEMENTS = 4  # distinct statements holding one quote that the quote check reads
GATES = ("quote", "bullet", "echo", "desc_rejected")
RETRIED = frozenset({"quote", "bullet", "echo"})  # the gates that earn the one re-ask
# function words and the words a readout calls the writer by: never content
_STOP_WORDS = (
    "a an the and or but if so as of to in on at by for from with without into onto over under "
    "about after before between during through than then that this these those there here it "
    "its is are was were be been being am do does did done have has had having will would can "
    "could should may might must shall not no nor only just also very too more most much many "
    "some any all each every both either neither own same such other another who whom whose "
    "which what when where why how i me my mine myself we us our ours you your yours he him his "
    "she her hers they them their theirs user users person someone writer one s t re ve ll d m "
    "up out off again still even ever never now yet get got"
)
ECHO_STOP = frozenset(_STOP_WORDS.split())
_WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Located:
    """Where a quote sits: ``gate`` (``""`` = it stands so far), the statement it lies in (the
    readout's own text), whether it was located near-verbatim, whether that statement is a
    token list, and the quoted part the quote check is shown. ``more`` = the further distinct
    statements that hold the same quote (each a standing ``Located``; see :meth:`readings`)."""

    gate: str
    statement: str = ""
    near: bool = False
    tokens: bool = False
    shown: str = ""
    more: tuple["Located", ...] = ()

    def readings(self) -> tuple["Located", ...]:
        """Every statement the quote check reads for a standing quote, at most
        :data:`MAX_STATEMENTS`: this one, then ``more``."""
        return (replace(self, more=()), *self.more) if not self.gate else ()


def content_words(text: str) -> list[str]:
    """The folded words of ``text`` that carry content: no :data:`ECHO_STOP` word, no single
    character; a final ``ing`` / ``es`` / ``ed`` / ``s`` is dropped when three letters remain
    (``bank.words``' rule), so "submitted" and "submitting" are one word."""
    out = []
    for w in _WORD.findall(digest.fold_quote(text)):
        if w in ECHO_STOP or len(w) < 2:
            continue
        for suffix in ("ing", "es", "ed", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


def chat_share(text: str, chat: str) -> float | None:
    """The share of ``text``'s content words that are the conversation's (None: it has none)."""
    words = content_words(text)
    have = set(content_words(chat))
    return sum(w in have for w in words) / len(words) if words else None


def mostly_chat(text: str, chat: str) -> bool:
    """``text`` is the user's own words, copied or reworded. On ``digest.fold_quote`` forms, a
    text of at least :data:`ECHO_MIN_CHARS` characters is mostly chat when EITHER its longest
    common block with the conversation covers :data:`ECHO_RATIO` of it (a verbatim copy, cut
    anywhere, even mid-word) OR it has at least :data:`ECHO_MIN_WORDS` content words and
    :data:`ECHO_RATIO` of them are the conversation's (a copy with the pronouns changed, words
    dropped, swapped or reordered: one edited word does not hide it)."""
    fq, fc = digest.fold_quote(text), digest.fold_quote(chat)
    if len(fq) < ECHO_MIN_CHARS or not fc:
        return False
    m = SequenceMatcher(None, fq, fc, autojunk=False).find_longest_match(0, len(fq), 0, len(fc))
    if m.size >= math.ceil(ECHO_RATIO * len(fq)):
        return True
    share = chat_share(text, chat)
    return share is not None and len(content_words(text)) >= ECHO_MIN_WORDS and share >= ECHO_RATIO


def echoes(quote: str, chat: str, statement: str = "") -> bool:
    """The quote repeats the user's own words. (a) The quote itself is :func:`mostly_chat`. (b)
    The quote adds NO content word of its own to the conversation's (whatever its length: this
    is the rule for a quote under :data:`ECHO_MIN_CHARS`, and for a quote of function words)
    and the ``statement`` that holds it is :func:`mostly_chat`: a short span lifted out of a
    copied line. A short quote whose statement goes beyond the chat is a clue word or two, not
    an echo; a quote that carries a word of its own ("so a school nurse" at the end of a copied
    line) is not one either, whatever its statement: the quote check reads it."""
    if mostly_chat(quote, chat):
        return True
    if not statement or not mostly_chat(statement, chat):
        return False
    share = chat_share(quote, chat)
    return share is None or share == 1.0


def locate(quote: str, lines: Sequence[digest.Line], cells: Sequence[Cell], chat: str) -> Located:
    """Steps 1-3 of the verification (plan 0106 §3.4). ``lines`` = the digest's evidence lines
    (summary) or ``digest.native_lines`` of the unit (native); ``chat`` = the conversation as
    the judge was shown it. A near-verbatim quote (``nearquote.close_lines``; prose only) is
    read through the line it came from: the statement is that line and so is the quoted part,
    never the judge's wording. EVERY distinct statement that holds the quote is a reading (in
    line order): the echo step (prose statements only) drops the readings that are the user's
    own words, the gate is ``echo`` when none is left, and the first :data:`MAX_STATEMENTS`
    standing ones go to the quote check. One word split across adjacent token positions lies
    in no single list: gate ``bullet`` (the value and the user must share one list)."""
    lines = list(lines)
    exact = digest.quote_ok(quote, lines)
    if not exact and not nearquote.close_lines(quote, lines):
        return Located("quote")
    statements = digest.statement_lines(lines, cells)
    found: dict[tuple[str, bool], Located] = {}
    if exact:
        if not digest.quote_ok(quote, statements):
            return Located("bullet")
        for s in statements:
            if digest.line_holds(quote, s):
                tok = s.pieces is not None
                found.setdefault((s.text, tok), Located("", s.text, tokens=tok, shown=quote))
    else:
        for s in nearquote.close_lines(quote, statements):
            found.setdefault((s.text, False), Located("", s.text, near=True, shown=s.text))
    if not found:  # a split word (exact) or a near quote that no single bullet holds
        return Located("bullet")
    readings = list(found.values())
    standing = [r for r in readings if r.tokens or not echoes(r.shown, chat, r.statement)]
    if not standing:
        return replace(readings[0], gate="echo")
    return replace(standing[0], more=tuple(standing[1:MAX_STATEMENTS]))


def broader_suspect(statement: str, part: Mapping[str, Any], broader: Mapping[str, Any]) -> bool:
    """A statement that holds a broader term of the part's type (``broader_by_type``,
    word-bounded) and neither the gold nor any of its forms. Never gating: it steers the hand
    read toward passes that may rest on a broader class."""
    terms = [str(t) for t in broader.get(str(part.get("type")), ())]
    if not terms or not mc.form_in(statement, terms):
        return False
    return not mc.form_in(statement, [str(part["gold"]), *map(str, part.get("forms") or ())])
