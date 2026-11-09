"""Plan 0087: the one-cue screen, the repair, the builder hook and the sweep CLI (offline fakes)."""

import json
import re

import pytest

from wsbench.adapt import onecue, store

TEXT = "Fact: the Swedish chemist who invented dynamite founded a prize named after"
MH = {
    "id": "mh-au-nobel",
    "prompt": TEXT,
    "mc": {
        "bridge_type": "person",
        "gold": "Alfred Nobel",
        "aliases": ["诺贝尔"],
        "distractors": ["Albert Einstein", "Jöns Jacob Berzelius", "Isaac Newton", "Carl Scheele"],
        "distractor_aliases": {"Albert Einstein": ["爱因斯坦"], "Carl Scheele": ["舍勒"]},
    },
    "adapt": {"origin": "authored"},
}
# a screen reply flags these options, citing these cues ("Mars" is never in the text)
BAD = {"Albert Einstein": "Swedish", "Isaac Newton": "Swedish", "Pierre Curie": "Mars"}


def _listing(user: str) -> list[str]:
    return re.findall(r"^\d+\. (.*)$", user, re.M)


class Fake:
    """Answers every onecue call kind by its system prompt; ``draws_flag`` limits which draws
    flag (default: every draw)."""

    def __init__(self, propose=None, bad=None, pick=None, draws_flag=None):
        self.propose = propose or []
        self.bad = BAD if bad is None else bad
        self.pick = pick or set()
        self.draws_flag = draws_flag
        self.calls: list[str] = []

    def __call__(self, tasks, schema, cache_path, model=None):
        out = {}
        for k, (system, user) in tasks.items():
            self.calls.append(system[:20])
            if system == onecue.SYSTEM:
                d = int(k.rsplit("|d", 1)[1])
                opts = _listing(user.split("WRONG options:")[1])
                out[k] = {
                    "frame": "a Swedish chemist",
                    "options": [
                        {
                            "option": i + 1,
                            "ruled_out": o in self.bad
                            and (self.draws_flag is None or d in self.draws_flag),
                            "cue": self.bad.get(o, ""),
                            "why": "",
                        }
                        for i, o in enumerate(opts)
                    ],
                }
            elif system.startswith("You write replacement"):
                out[k] = {
                    "candidates": [{"option": c, "forms": [c + " (zh)"]} for c in self.propose]
                }
            elif system == onecue.VALID_SYSTEM:
                opts = _listing(user)
                hit = [i for i, o in enumerate(opts) if o in self.pick]
                gold = next(i for i, o in enumerate(opts) if o in ("Alfred Nobel",))
                out[k] = {"choice": (hit[0] if hit else gold) + 1}
        return out


def test_rows_of_per_family():
    (r,) = onecue.rows_of("multihop", MH)
    assert (r.role, r.gold, r.distractors[0]) == ("bridge", "Alfred Nobel", "Albert Einstein")
    brmt = {"id": "b", "prompt": "q", "mc": {"readout": {"gold": "g", "distractors": ["x"]}}}
    assert [x.role for x in onecue.rows_of("basic_readout_mt", brmt)] == ["readout"]
    ml = {
        "id": "m",
        "prompt": "q",
        "mc_roles": ["concept", "language"],
        "mc": {
            "concept": {"gold": "g", "distractors": ["x"]},
            "language": {"gold": "Russian", "distractors": ["Ukrainian"]},
        },
    }
    assert [x.role for x in onecue.rows_of("multilingual_mt", ml)] == ["concept"]  # never language
    assert onecue.rows_of("multihop", {"id": "z", "prompt": "q"}) == []
    with pytest.raises(SystemExit):
        onecue.rows_of("poetry", ml)


def test_flag_needs_two_of_three_draws_and_a_verified_cue(tmp_path):
    rows = onecue.rows_of("multihop", MH)
    got = onecue.flags(rows, tmp_path / "c.jsonl", ask=Fake())
    assert set(got[("multihop", "mh-au-nobel", "bridge")]) == {"Albert Einstein", "Isaac Newton"}
    assert onecue.flags(rows, tmp_path / "c.jsonl", ask=Fake(draws_flag={0})) == {}  # 1 of 3
    two = onecue.flags(rows, tmp_path / "c.jsonl", ask=Fake(draws_flag={0, 2}))
    assert set(two[("multihop", "mh-au-nobel", "bridge")]) == {"Albert Einstein", "Isaac Newton"}
    # a cue that is not a substring of the text flags nothing
    unverified = Fake(bad={"Albert Einstein": "Mars"})
    assert onecue.flags(rows, tmp_path / "c.jsonl", ask=unverified) == {}
    assert onecue.cue_ok("swedish CHEMIST", TEXT) and not onecue.cue_ok("", TEXT)


def test_repair_replaces_flagged_in_place_and_bans(tmp_path):
    (row,) = onecue.rows_of("multihop", MH)
    flagged = {row: {"Albert Einstein", "Isaac Newton"}}
    fake = Fake(
        # Nobel: the gold (linted); dynamite: named in the text (linted); Pierre Curie: the
        # validity screen picks it (banned); Arrhenius / Klaproth fill the two slots
        propose=["Nobel", "dynamite", "Pierre Curie", "Svante Arrhenius", "Martin Klaproth"],
        pick={"Pierre Curie"},
    )
    (rep,) = onecue.repair(flagged, tmp_path, ask=fake).values()
    assert rep.distractors == [
        "Svante Arrhenius",
        "Jöns Jacob Berzelius",
        "Martin Klaproth",
        "Carl Scheele",
    ]
    assert rep.replaced == {
        "Albert Einstein": "Svante Arrhenius",
        "Isaac Newton": "Martin Klaproth",
    }
    assert rep.aliases["Svante Arrhenius"] == ["Svante Arrhenius (zh)"]
    block = onecue.new_block("multihop", MH, "bridge", rep)
    assert block["gold"] == "Alfred Nobel" and block["aliases"] == ["诺贝尔"]
    assert "Albert Einstein" not in block["distractor_aliases"]
    assert block["distractor_aliases"]["Carl Scheele"] == ["舍勒"]
    assert block["distractor_aliases"]["Martin Klaproth"] == ["Martin Klaproth (zh)"]


def test_repair_bans_a_candidate_the_rescreen_flags(tmp_path):
    (row,) = onecue.rows_of("multihop", MH)
    fake = Fake(
        propose=["Pierre Curie", "Svante Arrhenius"],
        bad={"Albert Einstein": "Swedish", "Pierre Curie": "Swedish"},
    )
    (rep,) = onecue.repair({row: {"Albert Einstein"}}, tmp_path, ask=fake).values()
    assert rep.replaced == {"Albert Einstein": "Svante Arrhenius"}


def test_repair_replaces_a_kept_option_its_rescreen_flags(tmp_path):
    (row,) = onecue.rows_of("multihop", MH)
    fake = Fake(propose=["Svante Arrhenius", "Martin Klaproth"])  # BAD also flags Newton
    (rep,) = onecue.repair({row: {"Albert Einstein"}}, tmp_path, ask=fake).values()
    assert rep.replaced == {
        "Albert Einstein": "Svante Arrhenius",
        "Isaac Newton": "Martin Klaproth",
    }


def test_rows_carry_gold_forms_for_lint():
    brmt = {
        "id": "b",
        "prompt": "Which church in Barcelona?",
        "target": "Sagrada Familia",
        "target_alts": ["Sagrada Família"],
        "name_forms": {"readout": {"zh": ["圣家堂"]}},
        "mc": {"readout": {"gold": "Sagrada Familia", "distractors": ["x"]}},
    }
    (row,) = onecue.rows_of("basic_readout_mt", brmt)
    assert "圣家堂" in row.gold_forms and "Sagrada Familia" not in row.gold_forms
    assert onecue.lint_candidate(row, ["Holy Family church", "圣家堂"], [])  # a gold translation


def test_repair_without_enough_clean_candidates_is_none(tmp_path):
    (row,) = onecue.rows_of("multihop", MH)
    fake = Fake(propose=["Nobel", "Svante Arrhenius"])  # one clean candidate for two slots
    assert onecue.repair({row: {"Albert Einstein", "Isaac Newton"}}, tmp_path, ask=fake) == {
        row: None
    }


def test_lint_candidate():
    (row,) = onecue.rows_of("multihop", MH)
    assert onecue.lint_candidate(row, ["alfred nobel"], [])
    assert onecue.lint_candidate(row, ["Carl Scheele"], [["Carl Scheele"]])
    assert onecue.lint_candidate(row, ["chemist"], [])  # named in the text
    assert onecue.lint_candidate(row, ["cannot tell"], [])
    assert onecue.lint_candidate(row, ["Svante Arrhenius", "阿伦尼乌斯"], [["Carl Scheele"]]) == ""


@pytest.fixture
def mdir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    return tmp_path


def test_repair_pending_repairs_or_pops_mc(mdir):
    m = "qwen3.5-4b"
    lost = json.loads(json.dumps(MH)) | {"id": "mh-au-lost"}
    orig = json.loads(json.dumps(MH)) | {"id": "mh-orig", "adapt": {"origin": "original"}}
    store.add_candidates(m, "multihop", [MH, lost, orig])
    # one clean candidate: the first item's two flagged slots can't both be filled -> both drop;
    # so give enough for a single-slot repair by flagging only Einstein on mh-au-nobel
    fake = Fake(propose=["Svante Arrhenius", "Martin Klaproth"], bad={"Albert Einstein": "Swedish"})
    got = onecue.repair_pending(m, "multihop", ask=fake)
    assert got == {"screened": 2, "repaired": 2, "dropped": 0}  # the original is never touched
    rows = {store.item_id(c): c for c in store._read_jsonl(store.cand_path(m, "multihop"))}
    assert rows["mh-au-nobel"]["mc"]["distractors"][0] == "Svante Arrhenius"
    rec = rows["mh-au-nobel"]["adapt"]["mc_build"]["onecue"]
    assert rec["flagged"] == {"bridge": ["Albert Einstein"]}
    assert rec["replaced"] == {"bridge": {"Albert Einstein": "Svante Arrhenius"}}
    assert rows["mh-orig"]["mc"] == MH["mc"] and "mc_build" not in rows["mh-orig"]["adapt"]
    # a rerun skips candidates already screened
    assert onecue.repair_pending(m, "multihop", ask=Fake())["screened"] == 0

    store.add_candidates(m, "multihop", [json.loads(json.dumps(MH)) | {"id": "mh-au-dead"}])
    got = onecue.repair_pending(m, "multihop", ask=Fake(propose=["Nobel"]))
    assert got == {"screened": 1, "repaired": 0, "dropped": 1}
    rows = {store.item_id(c): c for c in store._read_jsonl(store.cand_path(m, "multihop"))}
    assert "mc" not in rows["mh-au-dead"]
    assert rows["mh-au-dead"]["adapt"]["mc_build"]["dropped"] is True  # lint.py rejects it
    from wsbench.adapt import lint

    full = {"name": "n", "target": "t", "intermediates": ["i"], "eval_render": "plain"}
    item = rows["mh-au-dead"] | full | {"readout": {"kind": "last_word_token", "offsets": [-2]}}
    assert lint.lint("multihop", item) == ["mc: the MC builder dropped this candidate (no options)"]
    assert rows["mh-au-dead"]["adapt"]["mc_build"]["onecue"]["dropped"] is True


def test_repair_pending_other_family_is_a_noop(mdir):
    assert onecue.repair_pending("qwen3.5-4b", "poetry", ask=Fake()) == {
        "screened": 0,
        "repaired": 0,
        "dropped": 0,
    }


def test_fill_screens_after_mc_and_before_gate(mdir, monkeypatch):
    from wsbench.adapt import author, mcstage
    from wsbench.adapt import run as arun

    calls = []
    monkeypatch.setattr(author, "author", lambda m, f, *a, **k: calls.append("author"))
    monkeypatch.setattr(mcstage, "build", lambda m, f: calls.append("mc") or {})
    monkeypatch.setattr(onecue, "repair_pending", lambda m, f: calls.append("onecue") or {})
    monkeypatch.setattr(arun, "lint_and_gate", lambda *a, **k: calls.append("gate") or ([], {}))
    arun.fill("qwen3.5-4b", "basic_readout_mt", llm=None, max_waves=1, runner=object())
    assert calls == ["author", "mc", "onecue", "gate"]
    calls.clear()
    arun.fill("qwen3.5-4b", "multilingual_mt", llm=None, max_waves=1, runner=object())
    assert calls == ["author", "onecue", "gate"]
    calls.clear()
    arun.fill("qwen3.5-4b", "typo_mt", llm=None, max_waves=1, runner=object())
    assert calls == ["author", "gate"]


def test_patch_stage_records_whole_mc_block(mdir, monkeypatch, tmp_path):
    from wsbench.adapt import run as arun

    m = "qwen3.5-4b"
    bank = {"family": "multihop", "items": [MH, json.loads(json.dumps(MH)) | {"id": "mh-x"}]}
    store.write_json(store.model_dir(m) / "multihop.json", bank)
    out = tmp_path / "sweep"
    out.mkdir()
    rep = {
        "family": "multihop",
        "id": "mh-au-nobel",
        "role": "bridge",
        "distractors": ["Svante Arrhenius", *MH["mc"]["distractors"][1:]],
        "aliases": {"Svante Arrhenius": ["阿伦尼乌斯"]},
        "replaced": {"Albert Einstein": "Svante Arrhenius"},
        "cues": {"Albert Einstein": ["Swedish"]},
    }
    (out / "repairs.json").write_text(json.dumps([rep, rep | {"id": "mh-x"}]))
    (out / "no_list.json").write_text(json.dumps([["multihop", "mh-x"]]))
    seen = []
    monkeypatch.setattr(arun, "patch", lambda *a: seen.append(a))
    onecue.run(m, ["multihop"], "patch", out, out)
    ((model, fam, iid, fields, note),) = seen  # mh-x is not repairable: no patch
    assert (model, fam, iid) == (m, "multihop", "mh-au-nobel")
    assert fields["mc"]["distractors"][0] == "Svante Arrhenius"
    assert fields["mc"]["gold"] == "Alfred Nobel" and fields["mc"]["bridge_type"] == "person"
    assert "Albert Einstein -> Svante Arrhenius (cue 'Swedish')" in note
    with pytest.raises(SystemExit):
        onecue.run(m, ["multihop"], "nope", out, out)
    with pytest.raises(SystemExit):
        onecue.run(m, ["multihop"], "patch", out)  # no shared cache
