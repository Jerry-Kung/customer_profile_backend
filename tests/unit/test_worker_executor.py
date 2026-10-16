from __future__ import annotations

import logging
from pathlib import Path

from src.domain.task.models import TaskRecord
from src.domain.task.service import TaskService
from src.infrastructure.workflow.errors import (
    WorkflowRequestFailed,
    WorkflowTimeout,
)
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.worker.executor import WorkerExecutor


class _Repo:
    def __init__(self, claimed: TaskRecord | None):
        self.claimed = claimed
        self.succeeded: tuple[int, dict, str, str] | None = None
        self.failed: tuple[int, str] | None = None
        self.last_claim_task_type: str | None = None

    def upsert_queued(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> TaskRecord:
        return TaskRecord(
            id=1,
            customer_phonenumber=customer_phonenumber,
            status="queued",
            work_env=work_env,
            batch_id=batch_id,
            task_type=task_type,
        )

    def claim_next_queued(self, *, task_type: str | None = None) -> TaskRecord | None:
        self.last_claim_task_type = task_type
        item = self.claimed
        self.claimed = None
        return item

    def mark_succeeded(self, *, task_id: int, output: dict, card_id: str, nickname: str) -> None:
        self.succeeded = (task_id, output, card_id, nickname)

    def mark_failed(self, *, task_id: int, error_message: str) -> None:
        self.failed = (task_id, error_message)


class _WorkflowSuccess:
    def __init__(self):
        self.last_args: dict | None = None

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> str:
        self.last_args = {
            "customer_phonenumber": customer_phonenumber,
            "work_env": work_env,
            "batch_id": batch_id,
            "task_type": task_type,
        }
        return '{"card_id":"card-1","nickname":"nick-1"}'


class _WorkflowBadJson:
    def run_workflow(self, **_kwargs) -> str:
        return "{bad-json}"


class _WorkflowMissingFields:
    def run_workflow(self, **_kwargs) -> str:
        return '{"nickname":"nick-1"}'


class _WorkflowRequestFail:
    def run_workflow(self, **_kwargs) -> str:
        raise WorkflowRequestFailed("request fail")


class _WorkflowDeferred:
    """Stands in for LocalClient: acknowledges submission, delivers no output.

    `DEFERS_RESULT` is the flag the executor reads to leave the row in `processing`.
    """

    DEFERS_RESULT = True

    def __init__(self):
        self.calls = 0

    def run_workflow(self, **_kwargs) -> str:
        self.calls += 1
        return "c2846ca7c73643acaa3da7bbfabf0f9d"


class _WorkflowTimeoutFail:
    def run_workflow(self, **_kwargs) -> str:
        raise WorkflowTimeout("timeout")


def _logger() -> logging.Logger:
    logger = logging.getLogger("test_worker_executor")
    logger.handlers = []
    logger.addHandler(logging.StreamHandler())
    logger.setLevel("INFO")
    return logger


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "customer-profile-output.schema.json"


def _parser() -> WorkflowOutputParser:
    return WorkflowOutputParser(schema_path=_schema_path())


def test_executor_success_path():
    repo = _Repo(
        claimed=TaskRecord(
            id=1,
            customer_phonenumber="13800138000",
            status="processing",
            work_env="production",
            batch_id="2026Q2-001",
            task_type="customer_profile",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    workflow = _WorkflowSuccess()
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "succeeded"
    assert repo.succeeded is not None
    assert repo.succeeded[2] == "card-1"
    assert repo.failed is None
    assert workflow.last_args == {
        "customer_phonenumber": "13800138000",
        "work_env": "production",
        "batch_id": "2026Q2-001",
        "task_type": "customer_profile",
    }


def test_executor_passes_invitation_strategy_task_type():
    repo = _Repo(
        claimed=TaskRecord(
            id=10,
            customer_phonenumber="13800138000",
            status="processing",
            task_type="invitation_strategy",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    workflow = _WorkflowSuccess()
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "succeeded"
    assert workflow.last_args is not None
    assert workflow.last_args["task_type"] == "invitation_strategy"


def test_executor_dedicated_to_task_type_claims_only_that_type():
    repo = _Repo(
        claimed=TaskRecord(
            id=11,
            customer_phonenumber="13800138000",
            status="processing",
            task_type="invitation_strategy",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
        task_type="invitation_strategy",
    )
    assert executor.task_type == "invitation_strategy"
    status = executor.execute_once()
    assert status == "succeeded"
    assert repo.last_claim_task_type == "invitation_strategy"


def test_executor_without_task_type_claims_any_type():
    repo = _Repo(claimed=TaskRecord(id=12, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowSuccess(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "succeeded"
    assert repo.last_claim_task_type is None


def test_executor_invalid_json_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=2, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowBadJson(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_OUTPUT_INVALID_JSON" in repo.failed[1]


def test_executor_missing_fields_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=3, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowMissingFields(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_OUTPUT_SCHEMA_MISMATCH" in repo.failed[1]


def test_executor_request_failed_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=4, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowRequestFail(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_REQUEST_FAILED" in repo.failed[1]


def test_executor_timeout_marks_failed():
    repo = _Repo(claimed=TaskRecord(id=5, customer_phonenumber="13800138000", status="processing"))
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowTimeoutFail(),
        output_parser=_parser(),
        logger=_logger(),
    )
    status = executor.execute_once()
    assert status == "failed"
    assert repo.failed is not None
    assert "WORKFLOW_TIMEOUT" in repo.failed[1]


def test_deferred_client_leaves_task_in_processing():
    """The local service returns 200 on submission and posts the profile later.

    Marking succeeded here would store an empty profile; marking failed would race the
    writeback that is still in flight.
    """
    repo = _Repo(
        claimed=TaskRecord(
            id=20,
            customer_phonenumber="13415100087",
            status="processing",
            batch_id="batch-20261016-01",
            task_type="customer_profile",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    workflow = _WorkflowDeferred()
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=workflow,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="local",
        task_type="customer_profile",
    )
    status = executor.execute_once()
    assert status == "submitted"
    assert workflow.calls == 1
    assert repo.succeeded is None
    assert repo.failed is None


def test_real_local_client_is_treated_as_deferred():
    """Guards the wiring the executor depends on: the flag must be readable off a real
    LocalClient instance, not just off a hand-written fake."""
    from src.config.settings import LocalConfig
    from src.infrastructure.local.client import LocalClient

    local_client = LocalClient(
        config=LocalConfig(
            base_url="http://local-profile.example:8099",
            task_path="/api/v1/tasks",
            api_key="k",
            callback_api_key="c",
            callback_path="/api/v1/remote/callback/update-profile",
            submit_timeout_ms=10000,
            max_retries=3,
            retry_after_default_seconds=30,
        )
    )
    repo = _Repo(
        claimed=TaskRecord(
            id=22,
            customer_phonenumber="13415100087",
            status="processing",
            task_type="customer_profile",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=local_client,
        output_parser=_parser(),
        logger=_logger(),
        platform_name="local",
    )
    # The client fails fast on a phone the local service would reject, so stub the submit.
    local_client.run_workflow = lambda **_kw: "run-id-123"  # type: ignore[method-assign]
    assert executor.execute_once() == "submitted"
    assert repo.succeeded is None
    assert repo.failed is None


def test_deferred_client_records_run_id_in_log(caplog):
    repo = _Repo(
        claimed=TaskRecord(
            id=21,
            customer_phonenumber="13415100087",
            status="processing",
            task_type="customer_profile",
        )
    )
    service = TaskService(repository=repo, logger=_logger())
    executor = WorkerExecutor(
        task_service=service,
        workflow_client=_WorkflowDeferred(),
        output_parser=_parser(),
        logger=_logger(),
        platform_name="local",
    )
    with caplog.at_level(logging.INFO, logger="test_worker_executor"):
        executor.execute_once()
    assert any(
        "worker_task_submitted_deferred" in record.getMessage()
        and "c2846ca7c73643acaa3da7bbfabf0f9d" in record.getMessage()
        for record in caplog.records
    )
