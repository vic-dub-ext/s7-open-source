"""Human corrections, logged once for every override so they can become
labeled training data for tuning the classifier later.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def insert_human_label(
    conn: Connection,
    *,
    target_type: str,
    target_id: str,
    field: str | None,
    original_value: str | None,
    corrected_value: str | None,
    action: str,
    note: str | None = None,
) -> str:
    label_id = new_id()
    conn.execute(
        text(
            "INSERT INTO human_labels (id, target_type, target_id, field, original_value, "
            "corrected_value, action, note, created_at) VALUES "
            "(:id, :target_type, :target_id, :field, :original_value, :corrected_value, "
            ":action, :note, :created_at)"
        ),
        {
            "id": label_id,
            "target_type": target_type,
            "target_id": target_id,
            "field": field,
            "original_value": original_value,
            "corrected_value": corrected_value,
            "action": action,
            "note": note,
            "created_at": now_iso(),
        },
    )
    return label_id
