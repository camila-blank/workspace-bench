"""``digest._key_hits`` / ``support_of``: the substring pre-check and the compiled-pattern cache
give exactly the old answers (the old body is restated here as the reference)."""

import re

from wsbench import digest
from wsbench.digest import Line


def _old_key_hits(key: str, ln: Line) -> bool:
    if ln.pieces is not None:
        return digest._token_level(key, ln.pieces) == 0
    k, t = digest._plain(key), digest._plain(ln.text)
    if not k:
        return False
    if not digest._ASCII_WORD.search(k):
        return k in t
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", t) is not None


KEYS = [
    "Kyle",
    "kyle johnson",
    "affair",
    "Café Luna",
    "CAFE LUNA",
    "5pm",
    "答案",
    "a",
    "",
    " ",
    "wipe at 5pm",
    "x+y",
    "(1)",
    "ID: 3",
    "über",
]
LINES = [
    Line("Kyle Johnson is having an affair."),
    Line("I will email Kyle; the wipe at 5pm must stop."),
    Line("cafe luna yesterday with Jessica"),
    Line("Kyles and kyle-johnson and akyle"),
    Line("答案是四十二"),
    Line("x+y=(1) ID: 3, ID: 30"),
    Line("Uber and über and ÜBER"),
    Line(""),
    Line("tokens", pieces=("Kyle", " Johnson", "affair")),
    Line("tokens", pieces=("5", "pm")),
]


def test_key_hits_unchanged() -> None:
    for k in KEYS:
        for ln in LINES:
            assert digest._key_hits(k, ln) == _old_key_hits(k, ln), (k, ln)


def test_support_of_unchanged() -> None:
    for k in KEYS:
        n = max(1, sum(1 for ln in LINES if _old_key_hits(k, ln)))
        want = "one" if n == 1 else "few" if n <= 3 else "several" if n <= 9 else "many"
        assert digest.support_of(k, LINES) == want
