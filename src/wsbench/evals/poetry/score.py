"""Poetry's deterministic gates (plan 0038 P4), ADDED on top of the shared quote verifier
``digest.quote_ok`` (never in place of it), so leniency cannot come from judge drift.

- :func:`line_final`: a raw sample writes a verse line that ENDS with the exact word (before a
  newline, a "/", a bullet start, or the end of the text; trailing punctuation allowed).
- :func:`named_plan`: a sample states outright that the line ends / rhymes on the exact word and
  names no other option.
- :func:`token_ok`: a token list holds the exact token, no other option, and is not a run of
  rhyme words (fewer than 3 of its tokens rhyme with line one's last word); rank is never used.
- :func:`counts`: per option, the number of samples (generations or token lists, per the declared
  sampling) where it passes the gate: the dominance veto needs ``n(gold) > max n(distractor)``.

A sample is whatever ``reads.load`` returns under the declared sampling: ``prose1`` = one whole
generation (its bullets count once), ``bullet4`` = 4 separate generations, ``tokens10`` = one
top-10 list. Poetry does no splitting of its own."""

import re
from collections.abc import Sequence

from wsbench import digest, mc
from wsbench.adapt.rhyme import rhymes
from wsbench.readouts import Cell

FLAT_RUN = 3  # >= this many rhyme words in one token list = a flat run (singles out none)
_MD = re.compile(r"\*\*|__|[*_`]")
_TRAIL = "[.,;:!?\u2026\"'*)\\]]*"  # trailing punctuation / closing quotes (folded)
_PLAN_CUE = re.compile(
    r"rhym|\bend(?:s|ing|ed)?\s+(?:with|on|in)\b|\blast\s+word\b|\bfinal\s+word\b|韵|押",
    re.IGNORECASE,
)
_LATIN_WORD = re.compile("[a-z\u00df-\u024f]+(?:['\\-][a-z\u00df-\u024f]+)*")


def _plain(text: str) -> str:
    """Markdown stripped and ``mc.fold``-ed (curly quotes -> ASCII, case, accents)."""
    return mc.fold(_MD.sub("", text))


def _word_re(word: str) -> str:
    return rf"(?<![\w'\-]){re.escape(mc.fold(word.strip()))}(?![\w\-]|'\w)"


def has_word(word: str, text: str) -> bool:
    """The exact word (case-folded, markdown stripped, word-bounded) occurs in ``text``."""
    return bool(word.strip()) and re.search(_word_re(word), _plain(text)) is not None


def line_final(word: str, text: str) -> bool:
    """``word`` (exact form) ends a line of ``text``: followed only by trailing punctuation /
    closing quotes, then a newline, a "/", a bullet start ``- `` or the end of the text. Runs on
    the RAW sample (the digest's one-line rendering hides newlines)."""
    if not word.strip():
        return False
    t = _plain(text).replace(digest.BULLET_MARK.strip(), "\n")
    pat = _word_re(word) + _TRAIL + r"[ \t]*(?:$|\n|/|- )"
    return re.search(pat, t) is not None


def named_plan(word: str, text: str, others: Sequence[str]) -> bool:
    """``text`` names the exact word with a plan cue (rhyme / ends with / last word / 韵 / 押) and
    names none of the ``others`` (the other options)."""
    return (
        has_word(word, text)
        and _PLAN_CUE.search(_plain(text)) is not None
        and not any(has_word(o, text) for o in others)
    )


def prose_ok(word: str, text: str, others: Sequence[str]) -> bool:
    return line_final(word, text) or named_plan(word, text, others)


def _tok(t: str) -> str:
    return mc.fold(t).strip()


def token_ok(word: str, tokens: Sequence[str], others: Sequence[str], end: str) -> bool:
    """The exact token is in the list, no other option is, and the list is not a flat run of
    rhyme words (fewer than :data:`FLAT_RUN` of its tokens rhyme with line one's last word
    ``end``; the word itself counts). Rank is never used (decision Q4)."""
    toks = [_tok(t) for t in tokens]
    w = _tok(word)
    if not w or w not in toks:
        return False
    if any(_tok(o) in toks for o in others):
        return False
    n_rhyme = sum(1 for t in set(toks) if t.isalpha() and (t == w or (end and rhymes(t, end))))
    return n_rhyme < FLAT_RUN


def quote_last_word(quote: str) -> str:
    """The last Latin-script word of a quote (``""`` for a quote with none, e.g. 希望)."""
    ws = _LATIN_WORD.findall(digest.fold_quote(quote))
    return ws[-1] if ws else ""


def exact_form(quote: str, gold: str, spellings: Sequence[str] = ()) -> bool:
    """The quote ENDS with the gold word exactly (or a listed orthographic variant, grey/gray):
    no inflection, translation or homograph."""
    last = quote_last_word(quote)
    return bool(last) and any(_tok(last) == _tok(f) for f in (gold, *spellings))


def raw_samples(cells: Sequence[Cell]) -> list[tuple[digest.Line, str | None, Cell]]:
    """Every loaded sample with its shared bundle Line (``digest.native_lines``) and its RAW text
    (``None`` for a token list), in (layer, pos) order: the raw-sample map."""
    out: list[tuple[digest.Line, str | None, Cell]] = []
    for c in sorted(cells, key=lambda c: (c.layer, c.pos)):
        lines = digest.native_lines([c])
        if c.tokens is not None:
            out.extend((ln, None, c) for ln in lines)
            continue
        raws = [s for s in (c.samples or ()) if s.strip()]
        out.extend((ln, raw, c) for ln, raw in zip(lines, raws, strict=True))
    return out


def counts(
    options: Sequence[str],
    cells: Sequence[Cell],
    end: str,
    spellings: dict[str, Sequence[str]] | None = None,
) -> dict[str, int]:
    """Per word option: the number of samples where it passes its gate (prose: line_final or
    named_plan; token list: token_ok, or the word spelled across adjacent positions of one
    layer). ``spellings`` maps an option to its orthographic variants."""
    sp = spellings or {}
    out = dict.fromkeys(options, 0)
    samples = raw_samples(cells)
    for o in options:
        others = [x for x in options if x != o]
        forms = (o, *sp.get(o, ()))
        n = 0
        for ln, raw, _c in samples:
            if raw is None:
                n += any(token_ok(f, ln.pieces or (), others, end) for f in forms)
            else:
                n += any(prose_ok(f, raw, others) for f in forms)
        # a word split across adjacent token positions of one layer counts once per layer
        tok_lines = [ln for ln, raw, _c in samples if raw is None]
        for layer in sorted({ln.layer for ln in tok_lines if ln.layer is not None}):
            at = [ln for ln in tok_lines if ln.layer == layer]
            if any(
                not any(_tok(t) == _tok(f) for ln in at for t in ln.pieces or ())
                and digest.split_word(f, at) is not None
                for f in forms
            ):
                n += 1
        out[o] = n
    return out


def evidence_samples(
    quote: str, lines: Sequence[digest.Line], cells: Sequence[Cell]
) -> list[tuple[digest.Line, str | None]]:
    """The loaded samples a verified quote came from: each line of ``lines`` the quote verifies
    against (``digest.quote_ok``) mapped back to its raw sample, the one at the same (layer, pos)
    whose shared ``native_lines`` rendering equals the line (``digest.fold_quote``). A word
    split across positions maps to its token lists. Empty = no raw sample (``quote_failed``)."""
    samples = raw_samples(cells)
    out: list[tuple[digest.Line, str | None]] = []
    for ln in lines:
        if not digest.quote_ok(quote, [ln]):
            continue
        key = digest.fold_quote(ln.text)
        for nl, raw, c in samples:
            if (c.layer, c.pos) == (ln.layer, ln.pos) and digest.fold_quote(nl.text) == key:
                out.append((nl, raw))
    if not out and digest.quote_ok(quote, lines):  # a word spelled across adjacent positions
        tok = [nl for nl, raw, _c in samples if raw is None]
        if tok and digest.split_word(quote, tok) is not None:
            word = " ".join(quote.split())
            out.append((digest.Line(word, (word,)), None))
    return out
