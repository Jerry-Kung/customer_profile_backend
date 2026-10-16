from __future__ import annotations

# Mirrors the DB column width (VARCHAR(64) — see 004_add_work_env_and_batch_id.sql) and the
# submit form's accepted charset (alnum plus '-' and '_').
MAX_BATCH_ID_LEN = 64


def effective_batch_id(
    *,
    customer_phonenumber: str,
    task_type: str,
    batch_id: str | None,
) -> str:
    """Resolve the batch id to hand the local service.

    The local service requires a non-blank `batch_id` and echoes it back verbatim in the
    writeback body, so it doubles as the correlation key for the finished profile. Our own
    submit API treats `batch_id` as optional, so when the caller did not supply one we derive
    a deterministic stand-in from values that survive a DB round-trip (phone + task type).

    The callback route recomputes the same value to check that a writeback belongs to the
    run it thinks is in flight — a stale writeback for a previous submission must not
    overwrite a newer one, so this must stay a pure function of stored row fields.
    """
    supplied = (batch_id or "").strip()
    if supplied:
        return supplied[:MAX_BATCH_ID_LEN]
    return f"cp-local-{task_type}-{customer_phonenumber}"[:MAX_BATCH_ID_LEN]
