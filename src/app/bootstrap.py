from __future__ import annotations

import logging

from src.api.app import create_api_app
from src.config.logging_utils import configure_root_logging, get_logger, log_config_summary
from src.config.settings import AppConfig, load_config
from src.domain.task.service import TaskService
from src.domain.task.task_types import SUPPORTED_TASK_TYPES
from src.contracts.registry import default_contract_paths
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.db.task_repository import MySQLTaskRepository
from src.infrastructure.dify.client import DifyClient
from src.infrastructure.local.client import LocalClient
from src.infrastructure.qingtian.client import QingTianClient
from src.infrastructure.storage.task_upload_store import TaskUploadStore
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.infrastructure.workflow.protocol import WorkflowClient
from src.worker.executor import WorkerExecutor


def bootstrap() -> tuple[AppConfig, MySQLClient, TaskService, list[WorkerExecutor], TaskUploadStore]:
    config = load_config()
    # Configure root logging FIRST so any logging.getLogger(__name__) call in dify/tos/output_parser
    # modules emits to stderr instead of being swallowed by a handler-less root logger.
    configure_root_logging()
    logger = get_logger("bootstrap")
    log_config_summary(logger, config)
    _log_process_targets(config, logger)
    db_client = MySQLClient(config.mysql)
    repository = MySQLTaskRepository(db_client, queue_max_waiting=config.task_queue_max_waiting)
    _warn_if_task_type_column_missing(logger, db_client, repository)
    task_service = TaskService(
        repository=repository,
        logger=get_logger("task_service"),
    )
    upload_store = TaskUploadStore(config.upload)
    contracts = default_contract_paths()
    # One dedicated consumption pool per task type so a slow/stuck type cannot starve
    # the others. Each executor claims only its own type; the runner gives each its own
    # WORKER_CONCURRENCY threads.
    executors = [
        WorkerExecutor(
            task_service=task_service,
            workflow_client=_build_workflow_client(config, logger, task_type=task_type),
            output_parser=WorkflowOutputParser(schema_path=contracts.output_schema_path),
            logger=get_logger("worker_executor"),
            platform_name=_platform_for_task_type(config, task_type),
            task_type=task_type,
        )
        for task_type in SUPPORTED_TASK_TYPES
    ]
    return config, db_client, task_service, executors, upload_store


def _log_process_targets(config: AppConfig, logger: logging.Logger) -> None:
    """Print the endpoints this process actually talks to, as resolved config.

    The API and the Worker are separate processes that share a queue through MySQL, so when a
    submitted task is never consumed, the first thing to rule out is that the two are pointed
    at the same database. Config is frozen and never re-read, so this line is the whole truth
    for the lifetime of the process.
    """
    logger.info(
        "process_targets db=%s:%s/%s workflow_platform=%s invitation_strategy_platform=%s "
        "local_base_url=%s worker_pool_task_types=%s",
        config.mysql.host,
        config.mysql.port,
        config.mysql.database,
        config.workflow_platform,
        config.invitation_strategy_platform,
        config.local.base_url or "<unset>",
        list(SUPPORTED_TASK_TYPES),
    )


def _platform_for_task_type(config: AppConfig, task_type: str) -> str:
    """Which platform serves this task type.

    Only customer_profile moved to the local service; invitation_strategy keeps the platform
    it had, which is why the two are configured separately.
    """
    if task_type == "invitation_strategy":
        return config.invitation_strategy_platform
    return config.workflow_platform


def _build_workflow_client(
    config: AppConfig,
    logger: logging.Logger,
    *,
    task_type: str = "customer_profile",
) -> WorkflowClient:
    platform = _platform_for_task_type(config, task_type)
    if platform == "local":
        logger.info("workflow_client_selected platform=local task_type=%s", task_type)
        return LocalClient(config=config.local)
    if platform == "qingtian":
        logger.info("workflow_client_selected platform=qingtian task_type=%s", task_type)
        return QingTianClient(config=config.qingtian)
    logger.info("workflow_client_selected platform=dify task_type=%s", task_type)
    return DifyClient(config=config.dify)


def _warn_if_task_type_column_missing(logger, db_client, repository) -> None:
    """Probe the task_type column at boot so ops gets a loud warning if migration 003 wasn't applied.

    Without this, invitation_strategy submissions silently run as customer_profile.
    Probe is best-effort: a DB failure here must not block startup.
    """
    try:
        conn = db_client.connect()
        try:
            if not repository._has_task_type_column(conn):
                logger.warning(
                    "task_type_column_missing migration=003_add_task_type.sql "
                    "effect=multi-type submissions will be miscategorized as customer_profile"
                )
        finally:
            conn.close()
    except Exception:
        logger.exception("task_type_column_probe_failed")


def create_application():
    config, db_client, task_service, _, upload_store = bootstrap()
    return create_api_app(
        db_client=db_client,
        task_service=task_service,
        upload_store=upload_store,
        local_config=config.local,
        output_parser=WorkflowOutputParser(
            schema_path=default_contract_paths().output_schema_path
        ),
    )
