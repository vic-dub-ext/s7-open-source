"""Cost/latency log for calls to external providers (Extend, Ensembl, OLS4,
Europe PMC, Unpaywall). Powers the UI's cost panel.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def record_provider_call(
    conn: Connection,
    *,
    run_id: str,
    stage: str,
    provider: str,
    operation: str,
    cost_credits: float | None,
    cached: bool,
    latency_ms: int,
    ok: bool,
) -> None:
    conn.execute(
        text(
            "INSERT INTO provider_calls (id, run_id, stage, provider, operation, cost_credits, "
            "cached, latency_ms, ok, created_at) VALUES "
            "(:id, :run_id, :stage, :provider, :operation, :cost_credits, :cached, "
            ":latency_ms, :ok, :created_at)"
        ),
        {
            "id": new_id(),
            "run_id": run_id,
            "stage": stage,
            "provider": provider,
            "operation": operation,
            "cost_credits": cost_credits,
            "cached": int(cached),
            "latency_ms": latency_ms,
            "ok": int(ok),
            "created_at": now_iso(),
        },
    )
