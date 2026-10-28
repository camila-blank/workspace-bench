"""The prompts this family sends: the forced-choice judge shared by the multi-token families."""

from wsbench.multitoken.prompts import PROMPT_VERSION, SCHEMA, SYSTEM, render_user, summary_prompts
from wsbench.multitoken.prompts import PROMPTS as SHARED

PROMPTS: dict[str, str] = {**SHARED, **summary_prompts("multihop_mt")}

__all__ = ["PROMPTS", "PROMPT_VERSION", "SCHEMA", "SYSTEM", "render_user"]
