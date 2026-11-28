"""Plan 0121 §2.1: the registered summariser ``interp-v2`` (the text that passed the A/B gate,
schema ``interp2``, the junk reading rule) beside the unchanged ``interp-v1``."""

import ast
import hashlib

import pytest

from wsbench.hb import REPO_ROOT, summarizer
from wsbench.hb.cache import fingerprint

GATE_SCRIPT = REPO_ROOT / "scripts" / "hb" / "v9" / "gates" / "interp.py"
# sha256 of scripts/hb/v9/gates/interp.py INTERP2_SYSTEM at d21d6a9b (the gate's test-half run)
INTERP2_SYSTEM_SHA256 = "49dda2d53e8d500a0ee1884ef1265f24ce3a61371b4d6a2bfa3c98796662e142"


def _gate_constant(name: str) -> str:
    for node in ast.parse(GATE_SCRIPT.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {GATE_SCRIPT}")


def test_interp_v2_system_is_the_gated_text():
    assert summarizer.INTERP2_SYSTEM == _gate_constant("INTERP2_SYSTEM")
    got = hashlib.sha256(summarizer.INTERP2_SYSTEM.encode("utf-8")).hexdigest()
    assert got == INTERP2_SYSTEM_SHA256


def test_registry():
    assert list(summarizer.VERSIONS) == ["interp-v1", "interp-v2"]
    v1, v2 = summarizer.spec("interp-v1"), summarizer.spec("interp-v2")
    assert (v1.version, v2.version) == ("interp-v1", "interp-v2")
    assert v2.system == summarizer.INTERP2_SYSTEM != v1.system
    assert v2.user == v1.user == summarizer.INTERP_USER  # the user text is unchanged
    assert v2.render_user("a | b") == summarizer.render_user("a | b")
    with pytest.raises(ValueError, match="unknown summarizer prompt"):
        summarizer.spec("interp-v3")
    # a new version string and system: an interp-v1 answer is never reused under interp-v2
    fps = {
        fingerprint(s.version, "claude-opus-5-5", "low", s.system, s.render_user("a | b"))
        for s in (v1, v2)
    }
    assert len(fps) == 2


def test_interp2_schema_has_junk_first_and_both_required():
    js = summarizer.INTERP2_SCHEMA["schema"]
    assert summarizer.INTERP2_SCHEMA["name"] == "interp2"
    assert list(js["properties"]) == ["junk", "interpretation"]
    assert js["properties"] == {"junk": {"type": "boolean"}, "interpretation": {"type": "string"}}
    assert js["required"] == ["junk", "interpretation"]


def test_interp_v2_reading_rule():
    read = summarizer.spec("interp-v2").read
    # junk = true is junk whatever the text
    for text in ("", "  ", "The tokens point to a cafe."):
        r = read({"junk": True, "interpretation": text})
        assert (r.kind, r.text, r.valid) == (summarizer.JUNK, None, True)
    r = read({"junk": False, "interpretation": "A cafe."})
    assert (r.kind, r.text, r.valid) == (summarizer.TEXT, "A cafe.", True)
    # junk = false with nothing to say, or a malformed answer, is invalid
    for bad in (
        {"junk": False, "interpretation": ""},
        {"junk": False, "interpretation": " \n"},
        {"junk": False},
        {"interpretation": "A cafe."},
        {"junk": "true", "interpretation": "A cafe."},
        None,
        [],
    ):
        r = read(bad)
        assert r.kind == summarizer.INVALID and not r.valid and r.text is None


def test_interp_v1_reading_has_no_junk():
    read = summarizer.spec("interp-v1").read
    assert read({"interpretation": "x"}) == summarizer.Reading(summarizer.TEXT, "x")
    assert read({"junk": True, "interpretation": "x"}).kind == summarizer.TEXT
    assert read({"interpretation": " "}).kind == summarizer.INVALID
    assert read(None).kind == summarizer.INVALID


def test_interp_v2_prompt_is_verbatim_in_the_docs():
    """Plan 0121 I4: a J-lens-only step's text is shown in the docs."""
    readme = (REPO_ROOT / "docs" / "hb" / "README.md").read_text(encoding="utf-8")
    assert "```\n" + summarizer.INTERP2_SYSTEM + "\n```" in readme
    flat = " ".join(readme.split())
    assert 'junk only "when the tokens are obviously unsemantic like literally not words' in flat
