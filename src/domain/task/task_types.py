from __future__ import annotations

DEFAULT_TASK_TYPE = "customer_profile"

# Every supported type gets its own worker consumption pool (see src/worker/runner.py);
# adding a type here spawns WORKER_CONCURRENCY extra threads in the worker process.
SUPPORTED_TASK_TYPES: tuple[str, ...] = ("customer_profile", "invitation_strategy")
