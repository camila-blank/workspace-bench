"""Plan 0121 A1 / A2 / A7: the index versions are exactly v8 and v9 (default v8); hindex-v9 is
the v8 code paths plus the junk-summaries count (and its own note, display names and the
``am_situation`` junk rates: ``test_v9_review_fixups.py``); ``hbench final`` selects by the
manifest's ``index_version`` and a v9 manifest needs its summariser stamps; the v8 manifest is
accepted exactly as before."""

import json
from pathlib import Path

import pytest

from wsbench.hb import DATA, cli, final
from wsbench.hb import index as ix

from .test_final import CLAIM_DIRS, _main, _manifest
from .test_index_v8 import _chat_cell, _w, v8_sources


@pytest.fixture(autouse=True)
def _two_layer_grid(monkeypatch):
    monkeypatch.setattr(final, "EXPECTED_LAYERS", (20, 24))


# ---------------------------------------------------------------- versions


def test_versions_are_v8_and_v9_default_v8():
    assert ix.VERSIONS == {"v8": "hindex-v8", "v9": "hindex-v9"}
    assert ix.INDEX_VERSION == ix.INDEX_VERSION_V8 == "hindex-v8"
    assert final.INDEX_VERSIONS == {"hindex-v8": "v8", "hindex-v9": "v9"}
    p = cli.build_parser()
    assert p.parse_args(["index"]).version == "v8"
    assert p.parse_args(["index", "--version", "v9"]).version == "v9"
    with pytest.raises(SystemExit):
        p.parse_args(["index", "--version", "v7"])
    # the helpers take v9 (the v8 code paths) and still refuse anything else
    for v in ("v8", "v9"):
        assert ix.families_for(v) == ix.FAMILIES_V8
        assert ix.claim_families(v) == ix.CLAIM_FAMILIES_V8
        assert ix.safety_families(v) == ix.SAFETY_FAMILIES_V8
    for v in ("v7", "v10", "hindex-v9", ""):
        with pytest.raises(ValueError, match="unknown index version"):
            ix.families_for(v)
    assert ix.FAMILY_WEIGHTS_V8 == {
        "main": 9.0,
        "am_claims": 3.0,
        "am_situation": 3.0,
        "sibling": 1.0,
        "story": 1.0,
        "overrefusal": 1.0,
        "benign_roleplay": 2.0,
    }


# ---------------------------------------------------------------- hindex-v9 = v8 + junk summaries


def _junk_chat(item: str, layer: int) -> dict:
    return {
        "key": f"{item}__j",
        "id": item,
        "layer": layer,
        "status": "empty",
        "samples": None,
        "stages": {"summary": "junk", "map": None},
        "map": None,
        "tokens": ["##"],
    }


def _junk_am(item: str, layer: int) -> dict:
    return {
        "key": f"{item}__j",
        "id": item,
        "layer": layer,
        "status": "ok",
        "n_mentions": 0,
        "bag": {"status": "ok", "n_mentions": 1},
        "summary_status": "junk",
        "map_status": None,
    }


def _add(path: Path, rows: list[dict]) -> None:
    with path.open("a") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)


def _summarised_chat(item: str, layer: int) -> dict:
    """A cell whose J-lens summary is a text (never mapped here, so it adds no claim)."""
    return {**_junk_chat(item, layer), "status": "pending", "stages": {"summary": "ok"}}


def _v9_sources(tmp: Path) -> dict:
    src = v8_sources(tmp)
    rows = [_junk_chat("i0", 20), _junk_chat("i1", 20)]
    rows += [_summarised_chat(i, 20) for i in ("i0", "i1", "i2")]
    _add(tmp / "main" / "jlens" / ix.CELLS, rows)
    _add(tmp / "am" / "jlens" / ix.CELLS, [_junk_am("i0", 24)])
    return src


# hindex-v9's own top-level keys (everything else is the hindex-v8 build of the same roots)
V9_KEYS = {"junk_summaries", "am_situation_junk", "display_names"}


def test_v9_is_the_v8_index_plus_junk_summaries(tmp_path):
    v8 = ix.build_index(v8_sources(tmp_path / "a"), ix.Methods(), version="v8")
    src = _v9_sources(tmp_path / "b")
    v8b = ix.build_index(src, ix.Methods(), version="v8")
    v9 = ix.build_index(src, ix.Methods(), version="v9")
    assert v9 == ix.build_index_v9(src, ix.Methods())
    assert v8b == ix.build_index_v8(src, ix.Methods()) == ix.build_index(src, ix.Methods())
    assert "junk_summaries" not in v8 and "junk_summaries" not in v8b
    assert (v8["version"], v9["version"]) == ("hindex-v8", "hindex-v9")

    def strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in ("version", "notes", *V9_KEYS)}

    # v9 = the v8 build of the same roots, plus its own keys and its own note
    assert strip(v9) == strip(v8b) and set(v9) - set(v8b) == V9_KEYS
    assert set(v9["notes"]) == {"hindex-v9"} and set(v8b["notes"]) == {"hindex-v8"}
    # junk cells add no claim: every row, CI and macro is what the roots give without them
    assert v9["rows"] == v8["rows"] and v9["over_layers"] == v8["over_layers"]

    def c(n_cells: int, n_summarised: int, n_junk: int) -> dict:
        return {"n_cells": n_cells, "n_summarised": n_summarised, "n_junk": n_junk}

    assert v9["junk_summaries"] == {
        "main": {"jlens": {"20": c(9, 5, 2), "24": c(4, 0, 0)}},
        "am_claims": {"jlens": {"20": c(4, 0, 0), "24": c(5, 1, 1)}},
    }  # story has no junk summary: not listed
    md = ix.render_md(v9)
    assert md.startswith("# Hallucination index (hindex-v9)")
    assert "## Junk summaries" in md
    # the share is junk / summarised (2 of 5), never junk / cells (2 of 9)
    assert "| main | jlens | 20 | 2 | 5 | 40.0 | 9 |" in md
    assert "| am_claims | jlens | 24 | 1 | 1 | 100.0 | 5 |" in md
    assert "Junk summaries" not in ix.render_md(v8)
    # above the v9 sections the table is hindex-v8's but for the name, the display name of the
    # off-topic refusal column and the note
    head8 = ix.render_md(v8).split("\n- hindex-v8: ")[0]
    head9 = md.split("\n## Junk")[0].split("\n- hindex-v9: ")[0]
    assert (
        head8.replace("hindex-v8", "hindex-v9", 1).replace("overrefusal", "Off-topic refusal")
        == head9
    )
    # a text that merely contains the marker is not counted
    quoted = {**_chat_cell("i9", 20, ["correct"]), "samples": ['"summary": "junk"']}
    _w(tmp_path / "q.jsonl", [quoted])
    assert ix.junk_summaries(tmp_path / "q.jsonl", ix.MAIN) == {20: c(1, 0, 0)}


def test_cli_index_v9(tmp_path, monkeypatch, capsys):
    for var in ix.SAFETY_ENV.values():
        monkeypatch.delenv(var, raising=False)
    src = _v9_sources(tmp_path)
    argv = ["index", "--out", str(tmp_path / "none"), "--dest", str(tmp_path / "d")]
    argv += ["--workers", "1", "--version", "v9"]
    for fam, flag in (
        (ix.MAIN, "--main-root"),
        (ix.STORY, "--story-root"),
        (ix.AM_CLAIMS, "--am-root"),
        (ix.SIBLING, "--um-root"),
        (ix.OVERREFUSAL, "--overrefusal-root"),
        (ix.BENIGN_ROLEPLAY, "--benign-roleplay-root"),
    ):
        argv += [flag, str(src[fam][0])]
    assert cli.main(argv) == 0
    built = json.loads((tmp_path / "d" / "index.json").read_text())
    assert built["version"] == "hindex-v9" and built["junk_summaries"]["main"]["jlens"]
    assert "## Junk summaries" in (tmp_path / "d" / "index.md").read_text()


# ---------------------------------------------------------------- hbench final


def _stamp(roots: Path, *, subs=("main", "story", "am"), joint=True, kind="tokens") -> None:
    """Mark the claim-map arm dirs as interp-v2 token arms."""
    for sub in subs:
        d = roots / sub / "jlens"
        names = ("arm.json", "joint.json") if joint else ("arm.json",)
        for name in names:
            if (p := d / name).is_file():
                rec = {**json.loads(p.read_text()), "summ_prompt": "interp-v2"}
                if name == "arm.json":
                    rec["kind"] = kind
                p.write_text(json.dumps(rec))


def _v9(m: dict) -> None:
    m["index_version"] = "hindex-v9"
    m["summarizer_prompt"] = "interp-v2"


def test_v9_manifest_builds_hindex_v9(tmp_path):
    def prep(roots: Path) -> None:
        _stamp(roots)
        _add(roots / "main" / "jlens" / ix.CELLS, [_junk_chat("i0", 20)])

    m = _manifest(tmp_path, mutate=_v9, prep=prep)
    assert _main(tmp_path, m) == 0
    built = json.loads((tmp_path / "final" / "index.json").read_text())
    assert built["version"] == "hindex-v9"
    assert built["manifest"]["summarizer_prompt"] == "interp-v2"
    assert built["manifest"]["excuse_verifier"] == "cverify-v1"
    # the junk cell is dropped from the recount like an empty cell, and reported
    assert built["junk_summaries"]["main"]["jlens"]["20"] == {
        "n_cells": 5,
        "n_summarised": 1,
        "n_junk": 1,
    }
    assert "## Junk summaries" in (tmp_path / "final" / "index.md").read_text()


def test_v9_manifest_without_the_stamp_is_refused(tmp_path, capsys):
    def no_stamp(m: dict) -> None:
        m["index_version"] = "hindex-v9"

    m = _manifest(tmp_path, mutate=no_stamp, prep=_stamp)
    assert _main(tmp_path, m) == 2
    assert "declares summarizer_prompt None; hindex-v9 needs 'interp-v2'" in capsys.readouterr().err

    def wrong(m: dict) -> None:
        _v9(m)
        m["summarizer_prompt"] = "interp-v1"

    assert _main(tmp_path / "w", _manifest(tmp_path / "w", mutate=wrong, prep=_stamp)) == 2
    assert "declares summarizer_prompt 'interp-v1'" in capsys.readouterr().err


@pytest.mark.parametrize("sub", sorted(CLAIM_DIRS.values()))
def test_v9_refuses_a_token_arm_without_interp_v2(tmp_path, capsys, sub):
    def prep(roots: Path) -> None:
        _stamp(roots)
        a = roots / sub / "jlens" / "arm.json"
        rec = json.loads(a.read_text())
        del rec["summ_prompt"]  # a missing record means interp-v1
        a.write_text(json.dumps(rec))

    assert _main(tmp_path, _manifest(tmp_path, mutate=_v9, prep=prep)) == 2
    err = capsys.readouterr().err
    assert "records summ_prompt 'interp-v1'; hindex-v9 needs 'interp-v2' for a token arm" in err
    assert f"{sub}/jlens/arm.json" in err


def test_v9_checks_joint_json_and_the_am_situation_entries(tmp_path, capsys):
    def stale_joint(roots: Path) -> None:
        _stamp(roots)
        j = roots / "story" / "jlens" / "joint.json"
        j.write_text(json.dumps({**json.loads(j.read_text()), "summ_prompt": "interp-v1"}))

    assert _main(tmp_path, _manifest(tmp_path, mutate=_v9, prep=stale_joint)) == 2
    err = capsys.readouterr().err
    assert "story x jlens x all" in err and "joint.json records summ_prompt 'interp-v1'" in err

    # am_situation is not a claim-map family, but its J-lens entries are summarised too (A7)
    def sit_only(m: dict) -> None:
        _v9(m)
        m["entries"] = [e for e in m["entries"] if e["family"] != ix.AM_CLAIMS]

    def prep(roots: Path) -> None:
        _stamp(roots, subs=("main", "story"))
        a = roots / "am" / "jlens" / "arm.json"
        a.write_text(json.dumps({**json.loads(a.read_text()), "kind": "tokens"}))

    m = _manifest(tmp_path / "s", mutate=sit_only, prep=prep)
    with pytest.raises(final.ManifestError, match=r"am_situation x jlens x L20.*summ_prompt"):
        final.check_v9(final.load_manifest(m), tmp_path / "s")


def test_v9_does_not_constrain_prose_arms(tmp_path, capsys):
    """A7: only arms whose arm.json has kind == "tokens" need interp-v2 (O-lens / NLA rows are
    the frozen v8 files); an arm.json whose kind is neither "tokens" nor "prose" is refused
    (review of PR a: no fail-open)."""

    def prep(roots: Path) -> None:
        for sub in CLAIM_DIRS.values():
            a = roots / sub / "jlens" / "arm.json"
            a.write_text(json.dumps({**json.loads(a.read_text()), "kind": "prose"}))

    assert _main(tmp_path, _manifest(tmp_path, mutate=_v9, prep=prep)) == 0
    # ... but an arm.json without `kind` could be an unsummarised token arm: refused
    assert _main(tmp_path / "n", _manifest(tmp_path / "n", mutate=_v9)) == 2
    assert "records kind None (neither 'tokens' nor 'prose')" in capsys.readouterr().err
    with pytest.raises(final.ManifestError, match="neither 'tokens' nor 'prose'"):
        final.check_v9(final.load_manifest(tmp_path / "n" / "v8_manifest.json"), tmp_path / "n")


def test_v8_manifest_is_accepted_exactly_as_before(tmp_path):
    m = _manifest(tmp_path)
    assert _main(tmp_path, m) == 0
    built = json.loads((tmp_path / "final" / "index.json").read_text())
    assert built["version"] == "hindex-v8" and "junk_summaries" not in built
    assert set(built["manifest"]) == {"path", "sha256", "prompt_version", "excuse_verifier"}
    assert "Junk summaries" not in (tmp_path / "final" / "index.md").read_text()
    # a v8 manifest is never asked for the v9 stamps, whatever its arm dirs record
    assert "summarizer_prompt" not in json.loads(m.read_text())
    # the frozen manifest of record still loads as hindex-v8
    frozen = final.load_manifest(DATA / "index" / "v8_manifest.json")
    assert frozen["index_version"] == "hindex-v8" and "summarizer_prompt" not in frozen


def test_unknown_index_version_is_refused(tmp_path, capsys):
    def v7(m: dict) -> None:
        m["index_version"] = "hindex-v7"

    assert _main(tmp_path, _manifest(tmp_path, mutate=v7)) == 2
    assert "index_version must be hindex-v8" in capsys.readouterr().err
