from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from src.config.settings import LocalConfig
from src.domain.task.phone import describe_phone_mismatch, is_local_supported_phone
from src.infrastructure.local.batch_id import effective_batch_id
from src.infrastructure.local.errors import LocalRequestFailed, LocalTimeout

_log = logging.getLogger(__name__)

# Statuses the local service documents as worth retrying; everything else is permanent
# for this request (401 / 403 / 422 / 503 in the interface doc).
_RETRYABLE_STATUSES = frozenset({429, 500})
_MAX_RETRY_AFTER_SECONDS = 120.0

# The local service is finished when the writeback arrives, not when the POST returns, so
# `run_workflow` only submits. WorkerExecutor reads this off the client instance and leaves
# the row in `processing` instead of parsing an output that does not exist yet.
DEFERS_RESULT = True


@dataclass
class LocalClient:
    """Submits a customer-profile task to the local Python profile service.

    Contract (docs/对外任务接口对接文档.md): POST {base_url}{task_path} with an
    `X-API-Key` header returns 200 as soon as the task is persisted. The finished profile
    is delivered separately to our writeback endpoint ~21 minutes later, so this client
    returns the local `run_id` and does not wait for a result.
    """

    config: LocalConfig

    # WorkerExecutor probes this on the instance, so it must live on the class.
    DEFERS_RESULT = DEFERS_RESULT

    def run_workflow(
        self,
        *,
        customer_phonenumber: str,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
    ) -> str:
        if not is_local_supported_phone(customer_phonenumber):
            # Fail fast with a precise reason: the local service would only answer a bare 422.
            raise LocalRequestFailed(
                f"local service rejected input before submit: "
                f"{describe_phone_mismatch(customer_phonenumber)}"
            )

        resolved_batch_id = effective_batch_id(
            customer_phonenumber=customer_phonenumber,
            task_type=task_type,
            batch_id=batch_id,
        )
        payload: dict[str, Any] = {
            "phone_number": customer_phonenumber,
            "batch_id": resolved_batch_id,
        }
        headers = {
            "X-API-Key": self.config.api_key,
            "Content-Type": "application/json",
        }
        url = f"{self.config.base_url.rstrip('/')}{self.config.task_path}"
        timeout = httpx.Timeout(
            connect=20.0,
            read=max(1.0, self.config.submit_timeout_ms / 1000),
            write=30.0,
            pool=20.0,
        )
        parts = urlparse(url)
        _log.info(
            "local_run_workflow_start netloc=%s path=%s task_type=%s work_env=%s batch_id=%s",
            parts.netloc or "",
            parts.path or "",
            task_type,
            work_env,
            resolved_batch_id,
        )

        attempts = max(1, self.config.max_retries + 1)
        try:
            with httpx.Client(timeout=None) as client:
                for attempt in range(1, attempts + 1):
                    started = time.monotonic()
                    try:
                        response = client.post(url, json=payload, headers=headers, timeout=timeout)
                    except httpx.TimeoutException as exc:
                        _log.warning(
                            "local_submit_timeout attempt=%s elapsed_ms=%s timeout_s=%s",
                            attempt,
                            int((time.monotonic() - started) * 1000),
                            self.config.submit_timeout_ms / 1000,
                        )
                        if attempt < attempts:
                            _log.warning("local_submit_timeout_retrying attempt=%s", attempt)
                            continue
                        raise LocalTimeout("local task submit timed out") from exc
                    except httpx.HTTPError as exc:
                        # Connection refused / DNS failure / TLS error. Without this line the
                        # whole failure surfaces only as a task row that never leaves processing.
                        _log.warning(
                            "local_submit_transport_error attempt=%s netloc=%s elapsed_ms=%s exc_type=%s detail=%s",
                            attempt,
                            parts.netloc or "",
                            int((time.monotonic() - started) * 1000),
                            type(exc).__name__,
                            str(exc)[:400],
                        )
                        raise

                    _log.info(
                        "local_submit_response attempt=%s status=%s elapsed_ms=%s",
                        attempt,
                        response.status_code,
                        int((time.monotonic() - started) * 1000),
                    )

                    if response.status_code in _RETRYABLE_STATUSES and attempt < attempts:
                        delay = self._retry_delay(response)
                        _log.warning(
                            "local_submit_retryable_status status=%s attempt=%s delay_seconds=%s",
                            response.status_code,
                            attempt,
                            delay,
                        )
                        time.sleep(delay)
                        continue

                    if response.status_code != 200:
                        raise LocalRequestFailed(
                            f"local submit HTTP {response.status_code}: {self._body_preview(response)}"
                        )
                    return self._extract_run_id(response)

            # Unreachable: every loop branch either returns or raises.
            raise LocalRequestFailed("local submit exhausted retries")
        except httpx.HTTPError as exc:
            _log.warning(
                "local_submit_request_failed netloc=%s exc_type=%s detail=%s",
                parts.netloc or "",
                type(exc).__name__,
                str(exc)[:400],
            )
            raise LocalRequestFailed(f"local submit request failed: {exc}") from exc

    def _retry_delay(self, response: httpx.Response) -> float:
        raw = (response.headers.get("Retry-After") or "").strip()
        try:
            delay = float(raw)
        except ValueError:
            delay = float(self.config.retry_after_default_seconds)
        return max(0.0, min(delay, _MAX_RETRY_AFTER_SECONDS))

    @staticmethod
    def _body_preview(response: httpx.Response) -> str:
        try:
            return (response.text or "")[:800]
        except Exception:  # pragma: no cover - defensive
            return ""

    @staticmethod
    def _extract_run_id(response: httpx.Response) -> str:
        """Return the local run_id. It is diagnostic only — the result comes by writeback."""
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LocalRequestFailed("local submit response is not valid JSON") from exc
        if not isinstance(body, dict):
            raise LocalRequestFailed("local submit response is not a JSON object")
        status = body.get("status")
        if isinstance(status, str) and status.strip() and status.strip() != "accepted":
            raise LocalRequestFailed(f"local submit returned unexpected status={status!r}")
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            # Accepted without a run_id is still a usable outcome: the task is persisted and
            # the writeback is what completes it. Log instead of failing the whole task.
            _log.warning(
                "local_submit_missing_run_id body_keys=%s", sorted(body.keys()),
            )
            return ""
        return run_id.strip()
