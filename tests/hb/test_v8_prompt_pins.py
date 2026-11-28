"""Plan 0121 A4: sha256 pins of the v8 prompt constants of the modules plan 0121 PR a edits
(``interp-v1``: system / user / schema / registry export), recorded from commit ``dc4c003c``
before any edit. Never regenerate these values: a wording change is a new registered version."""

import hashlib
import inspect
import json

from wsbench.hb import summarizer
from wsbench.hb.cache import fingerprint


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


PINS = {
    "INTERP_SYSTEM": "0c4a159352ff350897445c57cf7b38a5a2078532f9dab8a1ff6b51b2379d98de",
    "INTERP_USER": "07d1d3e4c19a8c7ac1783856d35dbc8828861e6c579c23b02266393a28e85f0e",
    "INTERP_SCHEMA": "ac043657db2559d4f378743cf17fc63e95212b4c59a6bfd154cb0ea1bc4d8679",
    "PROMPTS": "7e5eeb90ac235c344f873706cf5497d0d952b490e077d53debb1107d80d91e2e",
    "interpretation_of": "d42021d14957ebdf73901a39c5b405e407722f5de119be16fad752a83c906b14",
}
# fingerprint("interp-v1", "claude-opus-5-5", "low", INTERP_SYSTEM, render_user(BAG)) at dc4c003c
BAG_TOKENS, BAG_SCORES = ["the", " cat", "猫"], [0.5, 0.25, 0.125]
BAG_TEXT = "the (0.50) |  cat (0.25) | 猫 (0.12)"
BAG_FINGERPRINT = "ff74a8ac9bf15240"


def test_interp_v1_constants_are_pinned():
    assert summarizer.SUMMARIZER_PROMPT_VERSION == "interp-v1"
    assert _sha(summarizer.INTERP_SYSTEM) == PINS["INTERP_SYSTEM"]
    assert _sha(summarizer.INTERP_USER) == PINS["INTERP_USER"]
    assert _sha(json.dumps(summarizer.INTERP_SCHEMA, sort_keys=True)) == PINS["INTERP_SCHEMA"]
    assert _sha(json.dumps(summarizer.PROMPTS, sort_keys=True)) == PINS["PROMPTS"]
    assert set(summarizer.PROMPTS) == {"INTERP_SYSTEM", "INTERP_USER"}


def test_interpretation_of_is_unchanged():
    assert _sha(inspect.getsource(summarizer.interpretation_of)) == PINS["interpretation_of"]
    assert summarizer.interpretation_of({"interpretation": "x"}) == "x"
    for bad in (None, [], {}, {"interpretation": ""}, {"interpretation": "  "}, {"junk": True}):
        assert summarizer.interpretation_of(bad) is None


def test_interp_v1_fingerprint_of_a_known_bag_is_unchanged():
    bag = summarizer.render_bag(BAG_TOKENS, BAG_SCORES)
    assert bag == BAG_TEXT
    user = summarizer.render_user(bag)
    assert user == "TOKEN READOUTS:\n" + BAG_TEXT
    fp = fingerprint("interp-v1", "claude-opus-5-5", "low", summarizer.INTERP_SYSTEM, user)
    assert fp == BAG_FINGERPRINT
    # the registered interp-v1 spec is those same objects, so every v8 call is unchanged
    v1 = summarizer.spec("interp-v1")
    assert v1 is summarizer.spec() is summarizer.VERSIONS[summarizer.V1]
    assert v1.version == summarizer.SUMMARIZER_PROMPT_VERSION
    assert v1.system is summarizer.INTERP_SYSTEM and v1.schema is summarizer.INTERP_SCHEMA
    assert v1.render_user(bag) == user
    assert fingerprint(v1.version, "claude-opus-5-5", "low", v1.system, v1.render_user(bag)) == fp
