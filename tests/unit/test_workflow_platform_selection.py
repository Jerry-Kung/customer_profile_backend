from __future__ import annotations

from src.app.bootstrap import _build_workflow_client, _platform_for_task_type
from src.config.settings import AppConfig, DifyConfig, LocalConfig, MigrationConfig, MySQLConfig, QingTianConfig, TosConfig, UploadConfig, WorkerConfig
from src.infrastructure.dify.client import DifyClient
from src.infrastructure.local.client import LocalClient
from src.infrastructure.qingtian.client import QingTianClient

import logging
from pathlib import Path


def _config(platform: str, invitation_platform: str | None = None) -> AppConfig:
    return AppConfig(
        app_env="local",
        log_level="INFO",
        workflow_platform=platform,
        invitation_strategy_platform=invitation_platform or platform,
        dify=DifyConfig(
            base_url="http://dify.example",
            run_url="http://dify.example/v1/workflows/run",
            api_key_test="dify-test",
            api_key_production="dify-prod",
            invitation_strategy_api_key="dify-inv",
            user_id="klh",
            image_mode="file_id",
            stream_init_timeout_ms=30000,
            poll_interval_ms=2000,
            poll_timeout_ms=2400000,
        ),
        qingtian=QingTianConfig(
            base_url="https://qingtian.example/api/proxy/api/v1",
            api_key_test="qt-test",
            api_key_production="qt-prod",
            invitation_strategy_api_key="qt-inv",
            user_id="klh",
            poll_interval_ms=2000,
            poll_timeout_ms=2400000,
        ),
        local=LocalConfig(
            base_url="http://local-profile.example:8099",
            task_path="/api/v1/tasks",
            api_key="local-key",
            callback_api_key="local-callback-key",
            callback_path="/api/v1/remote/callback/update-profile",
            submit_timeout_ms=10000,
            max_retries=3,
            retry_after_default_seconds=30,
        ),
        mysql=MySQLConfig(host="h", port=3306, user="u", password="p", database="d"),
        upload=UploadConfig(base_dir=Path("."), max_bytes_per_file=1024),
        worker=WorkerConfig(concurrency=1, poll_interval_seconds=1.0),
        tos=TosConfig(
            access_key_id="",
            access_key_secret="",
            region="",
            endpoint="",
            bucket="",
            presigned_url_expires_seconds=3600,
        ),
        migration=MigrationConfig(db_wait_max_tries=1, db_wait_interval_seconds=0.1),
    )


def test_dify_platform_returns_dify_client():
    client = _build_workflow_client(_config("dify"), logging.getLogger("test"))
    assert isinstance(client, DifyClient)


def test_qingtian_platform_returns_qingtian_client():
    client = _build_workflow_client(_config("qingtian"), logging.getLogger("test"))
    assert isinstance(client, QingTianClient)


def test_local_platform_returns_local_client():
    client = _build_workflow_client(_config("local"), logging.getLogger("test"))
    assert isinstance(client, LocalClient)
    assert client.config.base_url == "http://local-profile.example:8099"


def test_local_platform_keeps_invitation_strategy_on_its_own_platform():
    """invitation_strategy is not migrated: the customer_profile pool moves to the local
    service while the invitation_strategy pool stays on the Agent platform."""
    config = _config("local", invitation_platform="qingtian")
    assert _platform_for_task_type(config, "customer_profile") == "local"
    assert _platform_for_task_type(config, "invitation_strategy") == "qingtian"

    cp_client = _build_workflow_client(config, logging.getLogger("test"), task_type="customer_profile")
    inv_client = _build_workflow_client(config, logging.getLogger("test"), task_type="invitation_strategy")
    assert isinstance(cp_client, LocalClient)
    assert isinstance(inv_client, QingTianClient)


def test_invitation_strategy_platform_defaults_to_workflow_platform():
    config = _config("dify")
    assert _platform_for_task_type(config, "invitation_strategy") == "dify"
    assert _platform_for_task_type(config, "customer_profile") == "dify"
