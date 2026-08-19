"""Pipeline stages. Each module exposes:

    async def run(run_id: str, *, force: bool = False) -> StageResult

It reads its input from the DB, writes its output to the DB, emits events via
s7.store.events, and is idempotent. `force=True` re-runs even if output
exists.
"""

from __future__ import annotations

from s7.stages import (
    s0_acquire,
    s1_explode,
    s2_parse,
    s3_classify,
    s4_context,
    s5_contract,
    s6_project,
    s7_normalize,
    s8_validate,
    s9_arbitrate,
    s10_publish,
)

# The one place stage name -> module is defined. Both the CLI's `stage`
# command and the UI's stage-run button read from here so they can never
# drift out of sync with each other.
STAGE_MODULES = {
    "s0_acquire": s0_acquire,
    "s1_explode": s1_explode,
    "s2_parse": s2_parse,
    "s3_classify": s3_classify,
    "s4_context": s4_context,
    "s5_contract": s5_contract,
    "s6_project": s6_project,
    "s7_normalize": s7_normalize,
    "s8_validate": s8_validate,
    "s9_arbitrate": s9_arbitrate,
    "s10_publish": s10_publish,
}
