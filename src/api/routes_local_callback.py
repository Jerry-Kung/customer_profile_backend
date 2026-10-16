from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from src.api.schemas import ProfileWritebackRequest
from src.config.settings import LocalConfig
from src.domain.task.service import TaskService
from src.infrastructure.local.batch_id import effective_batch_id
from src.infrastructure.workflow.errors import WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch
from src.infrastructure.workflow.output_parser import WorkflowOutputParser

# The local profile service POSTs the finished profile here. The path is the one documented
# in docs/对外任务接口对接文档.md §6 and is configurable via LOCAL_CALLBACK_PATH, because the
# service is not necessarily deployed with this project.
DEFAULT_CALLBACK_PATH = "/api/v1/remote/callback/update-profile"

_TASK_TYPE = "customer_profile"


def build_callback_router(
    *,
    task_service: TaskService,
    output_parser: WorkflowOutputParser,
    local_config: LocalConfig,
    logger: logging.Logger,
) -> APIRouter:
    router = APIRouter(tags=["LocalProfileWriteback"])

    @router.post(local_config.callback_path)
    async def receive_profile_writeback(request: Request, payload: ProfileWritebackRequest):
        configured_key = (local_config.callback_api_key or "").strip()
        if not configured_key:
            # Mirrors the local service's own behaviour: an unconfigured key closes the
            # endpoint outright rather than admitting unauthenticated callers.
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "writeback endpoint is not configured on this server"},
            )

        provided_key = (request.headers.get("X-API-Key") or "").strip()
        if not provided_key:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "missing X-API-Key header"},
            )
        if not hmac.compare_digest(provided_key, configured_key):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "invalid X-API-Key"},
            )

        phone = payload.phone.strip()
        logger.info(
            "local_writeback_received customer_phonenumber=%s batch_id=%s",
            phone,
            payload.batch_id,
        )

        task = task_service.get_task_by_phone(phone, task_type=_TASK_TYPE)
        if task is None:
            logger.warning("local_writeback_unknown_task customer_phonenumber=%s", phone)
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "no task for this phone number"},
            )

        expected_batch_id = effective_batch_id(
            customer_phonenumber=task.customer_phonenumber,
            task_type=task.task_type,
            batch_id=task.batch_id,
        )
        if payload.batch_id.strip() != expected_batch_id:
            # A stale writeback for an earlier submission of the same phone must not
            # overwrite the run currently in flight.
            logger.warning(
                "local_writeback_batch_id_mismatch customer_phonenumber=%s expected=%s received=%s",
                phone,
                expected_batch_id,
                payload.batch_id,
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "batch_id does not match the in-flight task"},
            )

        if task.status == "succeeded":
            # Re-delivery of a writeback we already applied: idempotent success.
            logger.info("local_writeback_duplicate customer_phonenumber=%s", phone)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "already_applied", "phone_number": phone},
            )
        if task.status != "processing" or task.id is None:
            logger.warning(
                "local_writeback_unexpected_status customer_phonenumber=%s status=%s",
                phone,
                task.status,
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": f"task is in status={task.status}, expected processing"},
            )

        try:
            parsed = output_parser.parse(_dump_analysis_result(payload.analysis_result))
        except (WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch) as exc:
            # Same validation the Dify path applies, so the result query stays consistent.
            logger.warning(
                "local_writeback_invalid_result customer_phonenumber=%s error_code=%s detail=%s",
                phone,
                exc.code,
                str(exc)[:400],
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"detail": f"{exc.code}: {exc}"},
            )

        task_service.repository.mark_succeeded(
            task_id=task.id,
            output=parsed.output,
            card_id=parsed.card_id,
            nickname=parsed.nickname,
        )
        logger.info(
            "local_writeback_applied customer_phonenumber=%s task_id=%s card_id=%s",
            phone,
            task.id,
            parsed.card_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "applied", "phone_number": phone},
        )

    return router


def _dump_analysis_result(analysis_result: dict) -> str:
    """Re-encode the writeback payload so the shared parser validates it identically."""
    return json.dumps(analysis_result, ensure_ascii=False)
