from __future__ import annotations

import os

import pytest

from src.config.settings import load_config


REQUIRED_ENV = {
    "DIFY_BASE_URL": "http://118.145.238.55",
    "DIFY_RUN_URL": "http://118.145.238.55/v1/workflows/run",
    "DIFY_API_KEY": "app-BARxxxxxxxx",
    "DIFY_API_KEY_PRODUCTION": "app-PROD-xxxx",
    "DIFY_INVITATION_STRATEGY_API_KEY": "app-INV-xxxx",
    "DIFY_USER_ID": "klh",
    "MYSQL_HOST": "118.145.238.55",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "root",
    "MYSQL_PASSWORD": "123456xxxx",
    "MYSQL_DATABASE": "customer_profile",
}


def _load_with_env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None):
    for key in list(os.environ.keys()):
        if key.startswith(("DIFY_", "MYSQL_", "LOCAL_")) or key in {
            "APP_ENV",
            "LOG_LEVEL",
            "WORKFLOW_PLATFORM",
            "INVITATION_STRATEGY_PLATFORM",
        }:
            monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    if extra:
        for key, value in extra.items():
            monkeypatch.setenv(key, value)
    return load_config()


def test_load_config_success_defaults(monkeypatch: pytest.MonkeyPatch):
    config = _load_with_env(monkeypatch)
    assert config.app_env == "local"
    assert config.log_level == "INFO"
    assert config.dify.image_mode == "file_id"
    assert config.dify.stream_init_timeout_ms == 30000
    assert config.dify.poll_interval_ms == 2000
    # poll_timeout_ms may be overridden by a present .env; only verify it is a positive int
    assert isinstance(config.dify.poll_timeout_ms, int) and config.dify.poll_timeout_ms > 0
    assert config.mysql.port == 3306
    assert config.worker.concurrency >= 1
    assert config.dify.invitation_strategy_api_key == "app-INV-xxxx"


def test_load_config_overrides(monkeypatch: pytest.MonkeyPatch):
    config = _load_with_env(
        monkeypatch,
        {
            "APP_ENV": "test",
            "LOG_LEVEL": "DEBUG",
            "DIFY_IMAGE_MODE": "multipart",
            "DIFY_STREAM_INIT_TIMEOUT_MS": "45000",
            "DIFY_POLL_INTERVAL_MS": "3000",
            "DIFY_POLL_TIMEOUT_MS": "600000",
            "MYSQL_PORT": "3307",
        },
    )
    assert config.app_env == "test"
    assert config.log_level == "DEBUG"
    assert config.dify.image_mode == "multipart"
    assert config.dify.stream_init_timeout_ms == 45000
    assert config.dify.poll_interval_ms == 3000
    assert config.dify.poll_timeout_ms == 600000
    assert config.mysql.port == 3307


def test_load_config_missing_required(monkeypatch: pytest.MonkeyPatch):
    _load_with_env(monkeypatch)
    monkeypatch.delenv("DIFY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="missing required environment variable: DIFY_API_KEY"):
        load_config()


def _load_local_with_env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None):
    for key in list(os.environ.keys()):
        if key.startswith(("DIFY_", "MYSQL_", "LOCAL_", "INVITATION_STRATEGY_")) or key in {
            "APP_ENV",
            "LOG_LEVEL",
            "WORKFLOW_PLATFORM",
        }:
            monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WORKFLOW_PLATFORM", "local")
    local_env = {
        "LOCAL_BASE_URL": "http://10.81.67.210:8099",
        "LOCAL_API_KEY": "inbound-key",
        "LOCAL_CALLBACK_API_KEY": "callback-key",
        "INVITATION_STRATEGY_PLATFORM": "qingtian",
    }
    local_env.update(extra or {})
    for key, value in local_env.items():
        monkeypatch.setenv(key, value)
    return load_config()


def test_local_platform_reads_local_config(monkeypatch: pytest.MonkeyPatch):
    config = _load_local_with_env(monkeypatch)
    assert config.workflow_platform == "local"
    assert config.local.base_url == "http://10.81.67.210:8099"
    assert config.local.task_path == "/api/v1/tasks"
    assert config.local.callback_path == "/api/v1/remote/callback/update-profile"
    assert config.local.submit_timeout_ms == 10000
    assert config.local.max_retries == 3
    # invitation_strategy stays where it was.
    assert config.invitation_strategy_platform == "qingtian"


def test_local_platform_missing_base_url_fails(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ.keys()):
        if key == "LOCAL_BASE_URL":
            monkeypatch.delenv(key, raising=False)
    # Re-run the standard local setup minus LOCAL_BASE_URL.
    with pytest.raises(ValueError, match="missing required environment variable: LOCAL_BASE_URL"):
        for key in list(os.environ.keys()):
            if key.startswith(("DIFY_", "MYSQL_", "LOCAL_", "INVITATION_STRATEGY_")) or key in {
                "APP_ENV",
                "LOG_LEVEL",
                "WORKFLOW_PLATFORM",
            }:
                monkeypatch.delenv(key, raising=False)
        for key, value in REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("WORKFLOW_PLATFORM", "local")
        monkeypatch.setenv("LOCAL_API_KEY", "inbound-key")
        monkeypatch.setenv("LOCAL_CALLBACK_API_KEY", "callback-key")
        monkeypatch.setenv("INVITATION_STRATEGY_PLATFORM", "qingtian")
        load_config()


def test_local_platform_config_overrides(monkeypatch: pytest.MonkeyPatch):
    config = _load_local_with_env(
        monkeypatch,
        {
            "LOCAL_TASK_PATH": "/custom/tasks",
            "LOCAL_CALLBACK_PATH": "/custom/callback",
            "LOCAL_SUBMIT_TIMEOUT_MS": "2500",
            "LOCAL_SUBMIT_MAX_RETRIES": "0",
            "LOCAL_RETRY_AFTER_DEFAULT_SECONDS": "5",
        },
    )
    assert config.local.task_path == "/custom/tasks"
    assert config.local.callback_path == "/custom/callback"
    assert config.local.submit_timeout_ms == 2500
    assert config.local.max_retries == 0
    assert config.local.retry_after_default_seconds == 5


def test_local_platform_requires_explicit_invitation_strategy_platform(
    monkeypatch: pytest.MonkeyPatch,
):
    """invitation_strategy is not migrated, so it cannot silently follow `local`."""
    with pytest.raises(ValueError, match="INVITATION_STRATEGY_PLATFORM must be set"):
        _load_local_with_env(monkeypatch, {"INVITATION_STRATEGY_PLATFORM": ""})


def test_invitation_strategy_platform_rejects_local(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="cannot be 'local'"):
        _load_local_with_env(monkeypatch, {"INVITATION_STRATEGY_PLATFORM": "local"})


def test_default_platform_still_requires_dify(monkeypatch: pytest.MonkeyPatch):
    config = _load_with_env(monkeypatch)
    assert config.workflow_platform == "dify"
    assert config.invitation_strategy_platform == "dify"
    # With the default platform, LOCAL_* stays optional.
    assert config.local.base_url == ""


def test_load_config_invalid_int(monkeypatch: pytest.MonkeyPatch):
    _load_with_env(monkeypatch)
    monkeypatch.setenv("MYSQL_PORT", "invalid")
    with pytest.raises(ValueError, match="invalid integer value for MYSQL_PORT"):
        load_config()
