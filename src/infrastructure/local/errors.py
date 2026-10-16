from __future__ import annotations

from src.infrastructure.workflow.errors import (
    WorkflowError,
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
    WorkflowRequestFailed,
    WorkflowTimeout,
)


class LocalError(WorkflowError):
    code: str = "LOCAL_REQUEST_FAILED"


class LocalRequestFailed(LocalError, WorkflowRequestFailed):
    code = "LOCAL_REQUEST_FAILED"


class LocalTimeout(LocalError, WorkflowTimeout):
    code = "LOCAL_TIMEOUT"


class LocalOutputInvalidJson(LocalError, WorkflowOutputInvalidJson):
    code = "LOCAL_OUTPUT_INVALID_JSON"


class LocalOutputSchemaMismatch(LocalError, WorkflowOutputSchemaMismatch):
    code = "LOCAL_OUTPUT_SCHEMA_MISMATCH"
