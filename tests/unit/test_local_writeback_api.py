from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import create_api_app
from src.api.routes_local_callback import DEFAULT_CALLBACK_PATH
from src.config.settings import LocalConfig
from src.domain.task.models import TaskRecord
from src.infrastructure.workflow.output_parser import WorkflowOutputParser

CALLBACK_KEY = "writeback-SECRET"
CALLBACK_URL = DEFAULT_CALLBACK_PATH


class _DBOK:
    def ping(self) -> bool:
        return True


class _UploadStore:
    pass


class _Repo:
    def __init__(self):
        self.succeeded: tuple[int, dict, str, str] | None = None

    def mark_succeeded(self, *, task_id: int, output: dict, card_id: str, nickname: str) -> None:
        self.succeeded = (task_id, output, card_id, nickname)


class _TaskService:
    """Serves one row for the writeback route to find."""

    def __init__(self, row: TaskRecord | None):
        self.row = row
        self.repository = _Repo()

    def get_task_by_phone(self, customer_phonenumber: str, *, task_type: str = "customer_profile"):
        if self.row is None or self.row.customer_phonenumber != customer_phonenumber:
            return None
        return self.row

    def claim_next_task(self, **_kwargs):
        return None


def _local_config() -> LocalConfig:
    return LocalConfig(
        base_url="http://local-profile.example:8099",
        task_path="/api/v1/tasks",
        api_key="inbound-SECRET",
        callback_api_key=CALLBACK_KEY,
        callback_path=DEFAULT_CALLBACK_PATH,
        submit_timeout_ms=10000,
        max_retries=3,
        retry_after_default_seconds=30,
    )


def _parser() -> WorkflowOutputParser:
    root = Path(__file__).resolve().parents[2]
    return WorkflowOutputParser(schema_path=root / "contracts" / "customer-profile-output.schema.json")


def _client(service: _TaskService) -> TestClient:
    app = create_api_app(
        db_client=_DBOK(),
        task_service=service,
        upload_store=_UploadStore(),
        local_config=_local_config(),
        output_parser=_parser(),
    )
    return TestClient(app)


def _processing_row(batch_id: str | None = "batch-20261016-01") -> TaskRecord:
    return TaskRecord(
        id=7,
        customer_phonenumber="13415100087",
        status="processing",
        work_env="test",
        batch_id=batch_id,
        task_type="customer_profile",
    )


def _writeback_body(**overrides) -> dict:
    body = {
        "phone": "13415100087",
        "batch_id": "batch-20261016-01",
        "analysis_result": {"card_id": "C123", "nickname": "Alice"},
    }
    body.update(overrides)
    return body


def test_writeback_applies_result_and_marks_succeeded():
    service = _TaskService(_processing_row())
    response = _client(service).post(
        CALLBACK_URL, json=_writeback_body(), headers={"X-API-Key": CALLBACK_KEY}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    assert service.repository.succeeded is not None
    task_id, output, card_id, nickname = service.repository.succeeded
    assert task_id == 7
    assert card_id == "C123"
    assert nickname == "Alice"
    assert output == {"card_id": "C123", "nickname": "Alice"}


def test_writeback_missing_key_returns_401():
    service = _TaskService(_processing_row())
    response = _client(service).post(CALLBACK_URL, json=_writeback_body())
    assert response.status_code == 401
    assert service.repository.succeeded is None


def test_writeback_wrong_key_returns_403():
    service = _TaskService(_processing_row())
    response = _client(service).post(
        CALLBACK_URL, json=_writeback_body(), headers={"X-API-Key": "not-the-key"}
    )
    assert response.status_code == 403
    assert service.repository.succeeded is None


def test_writeback_unknown_phone_returns_404():
    service = _TaskService(_processing_row())
    response = _client(service).post(
        CALLBACK_URL,
        json=_writeback_body(phone="13900139000"),
        headers={"X-API-Key": CALLBACK_KEY},
    )
    assert response.status_code == 404
    assert service.repository.succeeded is None


def test_writeback_stale_batch_id_returns_409():
    """A late writeback for a previous submission must not overwrite the current run."""
    service = _TaskService(_processing_row(batch_id="batch-NEW"))
    response = _client(service).post(
        CALLBACK_URL,
        json=_writeback_body(batch_id="batch-OLD"),
        headers={"X-API-Key": CALLBACK_KEY},
    )
    assert response.status_code == 409
    assert service.repository.succeeded is None


def test_writeback_matches_synthesized_batch_id():
    """A row submitted without batch_id must still accept its writeback."""
    service = _TaskService(_processing_row(batch_id=None))
    expected = "cp-local-customer_profile-13415100087"
    response = _client(service).post(
        CALLBACK_URL,
        json=_writeback_body(batch_id=expected),
        headers={"X-API-Key": CALLBACK_KEY},
    )
    assert response.status_code == 200
    assert service.repository.succeeded is not None


def test_writeback_invalid_analysis_result_returns_422():
    service = _TaskService(_processing_row())
    response = _client(service).post(
        CALLBACK_URL,
        json=_writeback_body(analysis_result={"nickname": "Alice"}),  # card_id missing
        headers={"X-API-Key": CALLBACK_KEY},
    )
    assert response.status_code == 422
    assert "OUTPUT_SCHEMA_MISMATCH" in response.json()["detail"]
    assert service.repository.succeeded is None


def test_writeback_duplicate_delivery_is_idempotent():
    row = _processing_row()
    row.status = "succeeded"
    service = _TaskService(row)
    response = _client(service).post(
        CALLBACK_URL, json=_writeback_body(), headers={"X-API-Key": CALLBACK_KEY}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "already_applied"
    assert service.repository.succeeded is None


def test_writeback_for_queued_row_returns_409():
    row = _processing_row()
    row.status = "queued"
    service = _TaskService(row)
    response = _client(service).post(
        CALLBACK_URL, json=_writeback_body(), headers={"X-API-Key": CALLBACK_KEY}
    )
    assert response.status_code == 409
    assert service.repository.succeeded is None


def test_writeback_blank_batch_id_is_rejected_by_validation():
    service = _TaskService(_processing_row())
    response = _client(service).post(
        CALLBACK_URL,
        json=_writeback_body(batch_id="   "),
        headers={"X-API-Key": CALLBACK_KEY},
    )
    assert response.status_code == 400


def test_route_not_registered_without_local_config():
    """Non-local deployments keep the writeback endpoint closed."""
    app = create_api_app(
        db_client=_DBOK(),
        task_service=_TaskService(_processing_row()),
        upload_store=_UploadStore(),
    )
    response = TestClient(app).post(
        CALLBACK_URL, json=_writeback_body(), headers={"X-API-Key": CALLBACK_KEY}
    )
    assert response.status_code == 404


def test_unconfigured_callback_key_closes_endpoint():
    """Mirrors the local service's own 503-when-unconfigured rule."""
    config = _local_config()
    config = LocalConfig(**{**config.__dict__, "callback_api_key": ""})
    app = create_api_app(
        db_client=_DBOK(),
        task_service=_TaskService(_processing_row()),
        upload_store=_UploadStore(),
        local_config=config,
        output_parser=_parser(),
    )
    response = TestClient(app).post(
        CALLBACK_URL, json=_writeback_body(), headers={"X-API-Key": CALLBACK_KEY}
    )
    assert response.status_code == 503


def test_callback_path_is_configurable():
    custom = "/custom/writeback/path"
    config = LocalConfig(**{**_local_config().__dict__, "callback_path": custom})
    service = _TaskService(_processing_row())
    app = create_api_app(
        db_client=_DBOK(),
        task_service=service,
        upload_store=_UploadStore(),
        local_config=config,
        output_parser=_parser(),
    )
    response = TestClient(app).post(
        custom, json=_writeback_body(), headers={"X-API-Key": CALLBACK_KEY}
    )
    assert response.status_code == 200
    assert service.repository.succeeded is not None
