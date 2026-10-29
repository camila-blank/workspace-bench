"""The prompts this family sends: the bank judge shared by the basic families, plus its own
family rule and `FORBIDDEN_TEMPLATE` (plan 0027)."""

from wsbench.basic.prompts import (
    FAMILY_RULE_MEDICAL,
    FORBIDDEN_TEMPLATE,
    SCHEMA,
    SYSTEM,
    prompt_version_of,
    render_user,
)
from wsbench.basic.prompts import PROMPTS as SHARED

PROMPT_VERSION = prompt_version_of("medical_diagnosis")
PROMPTS: dict[str, str] = {
    **SHARED,
    "FAMILY_RULE_MEDICAL": FAMILY_RULE_MEDICAL,
    "FORBIDDEN_TEMPLATE": FORBIDDEN_TEMPLATE,
}

__all__ = ["PROMPTS", "PROMPT_VERSION", "SCHEMA", "SYSTEM", "render_user"]
