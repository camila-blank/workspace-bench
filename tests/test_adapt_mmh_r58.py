"""plan 0078 R58 (corrected, as plan 0077 §3b): a per-item leaker meets the per-run test in >= 2 of
the 5 runs of one measurement; the tier C screen and every epoch measurement are 5 runs."""

import pytest
import test_adapt_mmh_tierc as tc_tests
from test_adapt_model_multihop import MODEL, _write_baseline

from wsbench.adapt import mmh, store

LEAK = {"blind": 3, "described": 1}  # the per-run test (a guesser >= 3/5, total >= 4/10)
ZERO = {"blind": 0, "described": 0}


def test_leaker_runs_needs_two_runs():
    assert mmh.RUNS == 5 and mmh.LEAK_RUNS_MIN == 2
    assert not mmh.leaker_runs([LEAK, ZERO, ZERO, ZERO, ZERO])  # one noisy run never counts
    assert mmh.leaker_runs([LEAK, ZERO, LEAK, ZERO, ZERO])
    # a run below the per-run test never counts, however often
    assert not mmh.leaker_runs([{"blind": 2, "described": 1}] * 5)


def test_tier_c_screen_needs_five_runs_and_two_leaks():
    item = {"mc": {"order": list("abcde"), "gold": "a"}}
    r = {"id": "c1iv0000", "item": item}
    d = {"c1iv0000": {"blind": [3], "described": [1]}}
    assert not mmh.screen_ok(r, d, {}, {"c1iv0000": [ZERO] * 3})  # 4 runs: not screened yet
    assert mmh.screen_ok(r, d, {}, {"c1iv0000": [ZERO] * 4})  # leaks in 1 of 5: kept
    assert not mmh.screen_ok(r, d, {}, {"c1iv0000": [LEAK, ZERO, ZERO, ZERO]})  # 2 of 5


def test_epoch_measurement_is_five_runs_and_drops_only_two_run_leakers(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    base, tc, _old, _rows = tc_tests._setup()
    tc_tests._open(base)
    ids = tc_tests._union(base, tc)
    with pytest.raises(SystemExit, match="5 distinct fresh runs"):
        mmh.confirm_ext(MODEL, str(tmp_path / "one"), "confirm")
    for k in range(mmh.RUNS):
        hits = {i: dict(ZERO) for i in ids}
        if k < 2:
            hits["c1iv0000"] = dict(LEAK)  # leaks in 2 runs: a leaker
        if k == 0:
            hits["c1iv0001"] = dict(LEAK)  # 1 run: kept, listed
        _write_baseline(tmp_path / f"m_r{k}", hits)
    rec = mmh.confirm_ext(MODEL, tc_tests._run_dirs(tmp_path, "m"), "confirm")
    assert rec["pass"] and rec["leak_rule"] == "r58-runs" and rec["runs"] == 5
    assert rec["tierc_leakers"] == ["c1iv0000"] and rec["kept_one_run"] == ["c1iv0001"]
    assert len(rec["run_rates"]) == 5 and rec["n"] == len(ids)
