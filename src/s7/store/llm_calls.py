"""Cost/prompt log for calls to providers/llm.py -- the LLM-side analogue
of provider_calls.py. Powers the contract inspector's prompt/response view
and the UI's cost panel.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def insert_llm_call(
    conn: Connection,
    *,
    run_id: str,
    stage: str,
    entity_id: str | None,
    provider: str,
    model: str,
    prompt_hash: str,
    prompt: str,
    response: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
    latency_ms: int,
    ok: bool,
) -> str:
    call_id = new_id()
    conn.execute(
        text(
            "INSERT INTO llm_calls (id, run_id, stage, entity_id, provider, model, "
            "prompt_hash, prompt, response, input_tokens, output_tokens, cost_usd, "
            "latency_ms, ok, created_at) VALUES (:id, :run_id, :stage, :entity_id, "
            ":provider, :model, :prompt_hash, :prompt, :response, :input_tokens, "
            ":output_tokens, :cost_usd, :latency_ms, :ok, :created_at)"
        ),
        {
            "id": call_id,
            "run_id": run_id,
            "stage": stage,
            "entity_id": entity_id,
            "provider": provider,
            "model": model,
            "prompt_hash": prompt_hash,
            "prompt": prompt,
            "response": response,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "ok": int(ok),
            "created_at": now_iso(),
        },
    )
    return call_id


def delete_llm_calls_for_run(conn: Connection, run_id: str, *, stage: str) -> None:
    """Scoped to one stage -- llm_calls is shared across every LLM-calling
    stage (S5 contract induction and S8 semantic validation), so an unscoped
    delete on --force would wipe another stage's calls too.
    """
    conn.execute(
        text("DELETE FROM llm_calls WHERE run_id = :run_id AND stage = :stage"),
        {"run_id": run_id, "stage": stage},
    )


def list_llm_calls_for_entity(conn: Connection, entity_id: str) -> list[dict[str, Any]]:
    """The contract inspector's prompt/response panel: every call keyed to
    one entity_id (e.g. a parsed_table_id for S5's dual-model induction).
    """
    rows = conn.execute(
        text("SELECT * FROM llm_calls WHERE entity_id = :entity_id ORDER BY created_at"),
        {"entity_id": entity_id},
    )
    return [dict(r._mapping) for r in rows]
