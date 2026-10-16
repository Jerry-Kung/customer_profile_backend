from __future__ import annotations

import json

import pytest

from src.config.settings import LocalConfig
from src.infrastructure.local.batch_id import effective_batch_id
from src.infrastructure.local.client import DEFERS_RESULT, LocalClient
from src.infrastructure.local.errors import LocalRequestFailed, LocalTimeout


class _Call:
    def __init__(self, url: str, json_body: dict, headers: dict):
        self.url = url
        self.json_body = json_body
        self.headers = headers


class _FakeResponse:
    def __init__(self, status_code: int = 200, body=None, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self._text = text
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return self._body

    @property
    def text(self) -> str:
        return self._text


class _FakeHttpxClient:
    """Feeds responses in order; the last one repeats. Records every POST."""

    def __init__(self, *args, responses: list[_FakeResponse] | None = None, **kwargs):
        self.responses = responses or []
        self.calls: list[_Call] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, json: dict, headers: dict, timeout=None):
        self.calls.append(_Call(url=url, json_body=dict(json), headers=dict(headers)))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _config() -> LocalConfig:
    return LocalConfig(
        base_url="http://local-profile.example:8099",
        task_path="/api/v1/tasks",
        api_key="inbound-SECRET",
        callback_api_key="writeback-SECRET",
        callback_path="/api/v1/remote/callback/update-profile",
        submit_timeout_ms=10000,
        max_retries=2,
        retry_after_default_seconds=30,
    )


def _install(monkeypatch, responses: list[_FakeResponse]):
    holder: dict = {}

    def _factory(*args, **kwargs):
        client = _FakeHttpxClient(*args, responses=responses, **kwargs)
        holder["client"] = client
        return client

    monkeypatch.setattr("src.infrastructure.local.client.httpx.Client", _factory)
    return holder


def test_client_is_deferred_result_client():
    """The worker probesthis flag on the *instance*, so it must be a class attribute.

    Getting this wrong is silent and total: the executor would fall through to parsing the
    run_id as profile output and fail every local task.
    """
    assert DEFERS_RESULT is True
    client = LocalClient(config=_config())
    assert getattr(client, "DEFERS_RESULT", False) is True


def test_submit_posts_documented_payload_and_returns_run_id(monkeypatch):
    holder = _install(
        monkeypatch,
        responses=[
            _FakeResponse(
                200,
                body={
                    "status": "accepted",
                    "run_id": "c2846ca7c73643acaa3da7bbfabf0f9d",
                    "workflow_id": "customer_profile_pipeline",
                    "phone_number": "13415100087",
                    "batch_id": "batch-20261016-01",
                },
            )
        ],
    )
    client = LocalClient(config=_config())
    run_id = client.run_workflow(
        customer_phonenumber="13415100087",
        batch_id="batch-20261016-01",
    )
    assert run_id == "c2846ca7c73643acaa3da7bbfabf0f9d"

    call = holder["client"].calls[0]
    assert call.url == "http://local-profile.example:8099/api/v1/tasks"
    assert call.json_body == {
        "phone_number": "13415100087",
        "batch_id": "batch-20261016-01",
    }
    # Documented auth header, not Bearer.
    assert call.headers["X-API-Key"] == "inbound-SECRET"


def test_base_url_is_never_assumed_to_be_loopback(monkeypatch):
    """The service may run on another host; the URL comes from config verbatim."""
    holder = _install(monkeypatch, responses=[_FakeResponse(200, body={"run_id": "r1"})])
    client = LocalClient(config=_config())
    client.run_workflow(customer_phonenumber="13415100087")
    assert holder["client"].calls[0].url.startswith("http://local-profile.example:8099")
    assert "127.0.0.1" not in holder["client"].calls[0].url


def test_missing_batch_id_is_synthesized_deterministically(monkeypatch):
    """Our submit API makes batch_id optional, the local service requires a non-blank one."""
    holder = _install(monkeypatch, responses=[_FakeResponse(200, body={"run_id": "r1"})])
    client = LocalClient(config=_config())
    client.run_workflow(customer_phonenumber="13415100087", task_type="customer_profile")
    sent = holder["client"].calls[0].json_body["batch_id"]
    assert sent == effective_batch_id(
        customer_phonenumber="13415100087", task_type="customer_profile", batch_id=None
    )
    assert sent.strip()
    assert len(sent) <= 64
    assert all(ch.isalnum() or ch in "-_" for ch in sent)


def test_13_digit_phone_fails_before_submitting(monkeypatch):
    """The local service answers 422 for anything but 11 digits; fail locally with a reason."""
    holder = _install(monkeypatch, responses=[_FakeResponse(200, body={"run_id": "r1"})])
    client = LocalClient(config=_config())
    with pytest.raises(LocalRequestFailed) as exc:
        client.run_workflow(customer_phonenumber="1341510008700")
    assert "11 digits" in str(exc.value)
    assert holder == {}  # no HTTP call was made


@pytest.mark.parametrize("status_code", [401, 403, 422, 503])
def test_permanent_statuses_fail_without_retry(monkeypatch, status_code):
    holder = _install(
        monkeypatch,
        responses=[_FakeResponse(status_code, text='{"detail":"nope"}')],
    )
    client = LocalClient(config=_config())
    with pytest.raises(LocalRequestFailed) as exc:
        client.run_workflow(customer_phonenumber="13415100087")
    assert f"HTTP {status_code}" in str(exc.value)
    # Exactly one attempt: these statuses are documented as not worth retrying.
    assert len(holder["client"].calls) == 1


def test_429_is_retried_honouring_retry_after_then_succeeds(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("src.infrastructure.local.client.time.sleep", slept.append)
    holder = _install(
        monkeypatch,
        responses=[
            _FakeResponse(429, text="queue full", headers={"Retry-After": "1"}),
            _FakeResponse(200, body={"status": "accepted", "run_id": "r-after-retry"}),
        ],
    )
    client = LocalClient(config=_config())
    run_id = client.run_workflow(customer_phonenumber="13415100087")
    assert run_id == "r-after-retry"
    assert len(holder["client"].calls) == 2
    assert slept == [1.0]


def test_500_retried_then_reported(monkeypatch):
    monkeypatch.setattr("src.infrastructure.local.client.time.sleep", lambda _s: None)
    holder = _install(
        monkeypatch,
        responses=[_FakeResponse(500, text="server exploded")],
    )
    client = LocalClient(config=_config())  # max_retries=2 -> 3 attempts
    with pytest.raises(LocalRequestFailed) as exc:
        client.run_workflow(customer_phonenumber="13415100087")
    assert "HTTP 500" in str(exc.value)
    assert len(holder["client"].calls) == 3


def test_unexpected_status_field_is_rejected(monkeypatch):
    _install(
        monkeypatch,
        responses=[_FakeResponse(200, body={"status": "queued_forever", "run_id": "r1"})],
    )
    client = LocalClient(config=_config())
    with pytest.raises(LocalRequestFailed) as exc:
        client.run_workflow(customer_phonenumber="13415100087")
    assert "unexpected status" in str(exc.value)


def test_accepted_without_run_id_still_counts_as_submitted(monkeypatch):
    """run_id is diagnostic only; the writeback is what completes the task."""
    _install(monkeypatch, responses=[_FakeResponse(200, body={"status": "accepted"})])
    client = LocalClient(config=_config())
    assert client.run_workflow(customer_phonenumber="13415100087") == ""


def test_non_json_body_raises_request_failed(monkeypatch):
    _install(monkeypatch, responses=[_FakeResponse(200, body=None, text="<html>oops</html>")])
    client = LocalClient(config=_config())
    with pytest.raises(LocalRequestFailed) as exc:
        client.run_workflow(customer_phonenumber="13415100087")
    assert "not valid JSON" in str(exc.value)


def test_timeout_raises_local_timeout(monkeypatch):
    import httpx

    def _factory(*args, **kwargs):
        class _Raising:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, *a, **kw):
                raise httpx.ReadTimeout("too slow")

        return _Raising()

    monkeypatch.setattr("src.infrastructure.local.client.httpx.Client", _factory)
    client = LocalClient(config=_config())
    with pytest.raises(LocalTimeout):
        client.run_workflow(customer_phonenumber="13415100087")
