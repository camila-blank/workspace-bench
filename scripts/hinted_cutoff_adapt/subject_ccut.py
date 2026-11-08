"""Run a global-workspace ccut v2 stage with an adapt subject in Qwen's place (plan 0084).

    HC_SUBJECT=glm-5.3 python subject_ccut.py <ccut_stages|ccut_frozen|ccut_exist4> variant=v2 \
        out_dir=<dir> stage=<stage> [...]

cwd = a gw source tree (``evals/chint_cutoff/bank_v2.json``); PYTHONPATH = gw ``src`` + the
cf_influence scripts dir + workspace-bench ``src``. The cwd must be a scratch copy holding only
``evals/chint_cutoff/`` (``bank`` and ``freeze`` write into it), never a gw checkout. The
substitutions, nothing else:
- ``ccut_stages._TOK`` = the subject's tokenizer with its pinned thinking-off template
  (``subjects.load_tokenizer``), so prompt lengths, the re-render check against the generated
  ``prompt_token_ids``, the reply-token count and every read position index the SUBJECT's
  render;
- ``ccut.TEMPLATES = ("metadata",)``: only the metadata hint is screened (gw #753 froze the
  metadata pairs only; the other two templates' read spans end before the hint);
- ``HC_P_WINDOW=lo,hi`` (optional) moves v2's probe window (``ccut.P_WINDOW``): the window the
  bank is drafted / filtered in (plan 0084 §1: the subject's fade-out lies later than Qwen's).
- ``HC_SURV=sI,sN,sI_screen,sN_screen`` (optional): the per-kind survivals v2's projection gates
  use (``ccut.V2_SURV_*``, Qwen v1's), replaced by the subject's measured pilot survivals;
- ``HC_CONFIRM_ALL=1`` (the pilot only, plan 0084 §2): ``confirm_gen`` confirms EVERY
  screen-passing condition of both kinds, not only the pairable I and their <= 4 N, so the
  pilot measures each stage's survival on all its candidates.
The ``[gen]`` step is ``subject_gen.py`` (the subject's adapt Modal app).
"""

import importlib
import os
import sys


def main() -> None:
    mod_name = sys.argv.pop(1)
    from pathlib import Path

    cwd = Path.cwd()
    assert not (cwd / ".git").exists() and not (cwd / "src/global_workspace").exists(), (
        f"{cwd}: run in a scratch gw cwd, not a gw checkout (bank / freeze write into it)"
    )
    from global_workspace.olens_suite.cf_influence import ccut

    from wsbench.adapt import subjects

    ccut.TEMPLATES = ("metadata",)
    if w := os.environ.get("HC_P_WINDOW"):
        lo, hi = w.split(",")
        ccut.P_WINDOW = (lo, hi)
    if sv := os.environ.get("HC_SURV"):
        ccut.V2_SURV_I, ccut.V2_SURV_N, ccut.V2_SURV_I_SCREEN, ccut.V2_SURV_N_SCREEN = (
            float(x) for x in sv.split(",")
        )
    s = subjects.get(os.environ.get("HC_SUBJECT", "glm-5.3"))
    tok = subjects.load_tokenizer(s.hf_id, s.revision, s.tokenizer_dir, s.chat_template,
                                  s.trust_remote_code)  # fmt: skip
    import ccut_stages

    ccut_stages._TOK = tok
    if os.environ.get("HC_CONFIRM_ALL") == "1":

        def confirm_all(cfg, d, bnk, passed, keys, lens):  # noqa: ARG001
            return ([k for k in keys if passed[k]["kind"] == "I"],
                    [k for k in keys if passed[k]["kind"] == "N"])  # fmt: skip

        ccut_stages._confirm_pick_v2 = confirm_all
    mod = importlib.import_module(mod_name)
    mod.main()


if __name__ == "__main__":
    main()
