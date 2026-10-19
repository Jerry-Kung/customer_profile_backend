from __future__ import annotations

import logging
import threading
import time

from src.domain.task.models import TaskRecord
from src.domain.task.service import TaskService
from src.infrastructure.db.task_repository import MySQLTaskRepository
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.worker.executor import WorkerExecutor
from src.worker.runner import WorkerRunner
from pathlib import Path

_ALL_COLUMNS = {"work_env", "batch_id", "task_type"}


class _FakeCursor:
    """Answers the three query shapes the queue-depth probe can emit."""

    def __init__(self, conn: "_FakeConn", dictionary: bool):
        self._conn = conn
        self._result = None
        self.rowcount = -1

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        if "information_schema.columns" in normalized:
            column = params[0]
            self._result = {"1": 1} if column in self._conn.columns else None
        elif normalized.startswith("SELECT COUNT(1) AS total"):
            self._result = {"total": self._conn.queued_total}
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, columns: set[str], queued_total: int):
        self.columns = columns
        self.queued_total = queued_total
        self.executed: list[tuple[str, object]] = []

    def cursor(self, dictionary: bool = False):
        return _FakeCursor(self, dictionary)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeDbClient:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def connect(self):
        return self._conn


def _count_selects(conn: _FakeConn) -> list[tuple[str, object]]:
    return [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("SELECT COUNT(1) AS total")
    ]


def test_count_queued_by_type_filters_when_column_exists():
    conn = _FakeConn(columns=set(_ALL_COLUMNS), queued_total=3)
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    assert repo.count_queued_by_type(task_type="customer_profile") == 3

    selects = _count_selects(conn)
    assert len(selects) == 1
    sql, params = selects[0]
    assert "task_type = %s" in sql
    assert params == ("customer_profile",)


def test_count_queued_by_type_without_column_counts_all_for_default_type():
    """Without the column every queued row reads as customer_profile, so the default
    pool's depth is the whole queue."""
    conn = _FakeConn(columns={"work_env", "batch_id"}, queued_total=5)
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    assert repo.count_queued_by_type(task_type="customer_profile") == 5
    sql, params = _count_selects(conn)[0]
    assert "task_type = %s" not in sql
    assert params is None


def test_count_queued_by_type_without_column_is_zero_for_other_type():
    """A pool for any other type can never claim anything when the column is missing,
    so reporting a nonzero depth would be the misleading answer."""
    conn = _FakeConn(columns={"work_env", "batch_id"}, queued_total=5)
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    assert repo.count_queued_by_type(task_type="invitation_strategy") == 0
    assert _count_selects(conn) == []


def test_executor_queue_depth_uses_repository_probe():
    class _Repo:
        def count_queued_by_type(self, *, task_type: str) -> int:
            return 7 if task_type == "customer_profile" else 0

    executor = WorkerExecutor(
        task_service=TaskService(repository=_Repo(), logger=logging.getLogger("t")),
        workflow_client=object(),
        output_parser=WorkflowOutputParser(
            schema_path=Path(__file__).resolve().parents[2]
            / "contracts"
            / "customer-profile-output.schema.json"
        ),
        logger=logging.getLogger("t"),
        task_type="customer_profile",
    )
    assert executor.queue_depth() == 7


def test_executor_queue_depth_degrades_without_probe_or_task_type():
    """The probe is diagnostic; a repository without it, or a shared pool, must not raise."""
    bare_executor = WorkerExecutor(
        task_service=TaskService(repository=object(), logger=logging.getLogger("t")),
        workflow_client=object(),
        output_parser=WorkflowOutputParser(
            schema_path=Path(__file__).resolve().parents[2]
            / "contracts"
            / "customer-profile-output.schema.json"
        ),
        logger=logging.getLogger("t"),
        task_type="customer_profile",
    )
    assert bare_executor.queue_depth() is None

    shared_pool = WorkerExecutor(
        task_service=TaskService(repository=object(), logger=logging.getLogger("t")),
        workflow_client=object(),
        output_parser=WorkflowOutputParser(
            schema_path=Path(__file__).resolve().parents[2]
            / "contracts"
            / "customer-profile-output.schema.json"
        ),
        logger=logging.getLogger("t"),
        task_type=None,
    )
    assert shared_pool.queue_depth() is None


def test_executor_queue_depth_probe_failure_is_swallowed():
    class _BrokenRepo:
        def count_queued_by_type(self, *, task_type: str) -> int:
            raise RuntimeError("db down")

    logger = logging.getLogger("test_queue_depth_broken")
    executor = WorkerExecutor(
        task_service=TaskService(repository=_BrokenRepo(), logger=logger),
        workflow_client=object(),
        output_parser=WorkflowOutputParser(
            schema_path=Path(__file__).resolve().parents[2]
            / "contracts"
            / "customer-profile-output.schema.json"
        ),
        logger=logger,
        task_type="customer_profile",
    )
    assert executor.queue_depth() is None


def test_runner_heartbeat_logs_queue_depth(caplog):
    """An empty queue and an unclaimable queue both used to look like silence."""

    class _Executor:
        task_type = "customer_profile"

        def execute_once(self) -> str:
            return "idle"

        def queue_depth(self) -> int:
            return 0

    runner = WorkerRunner(
        executors=[_Executor()],
        interval_seconds=0.01,
        supervisor_interval_seconds=0.02,
    )
    t = threading.Thread(target=runner.run_forever, daemon=True)
    with caplog.at_level(logging.INFO, logger="worker"):
        t.start()
        time.sleep(0.15)
        runner.stop()
        t.join(timeout=1.0)
    messages = [r.getMessage() for r in caplog.records]
    assert any("worker_queue_depth" in m and "customer_profile" in m for m in messages)


def test_runner_heartbeat_depth_survives_a_broken_executor(caplog):
    """A queue-depth failure must not stop the supervisor from beating.

    The supervisor still runs: the depth line keeps appearing with None for the pool, which is
    exactly how a probe failure is supposed to look.
    """

    class _Executor:
        task_type = "customer_profile"

        def execute_once(self) -> str:
            return "idle"

        def queue_depth(self) -> int:
            raise RuntimeError("db down")

    runner = WorkerRunner(
        executors=[_Executor()],
        interval_seconds=0.01,
        supervisor_interval_seconds=0.02,
    )
    t = threading.Thread(target=runner.run_forever, daemon=True)
    with caplog.at_level(logging.INFO, logger="worker"):
        t.start()
        time.sleep(0.15)
        runner.stop()
        t.join(timeout=1.0)
    depth_lines = [r.getMessage() for r in caplog.records if "worker_queue_depth" in r.getMessage()]
    assert depth_lines, "supervisor stopped beating after a probe failure"
    assert all("'customer_profile': None" in line for line in depth_lines)


def test_queue_depth_snapshot_reports_none_without_probe():
    class _Executor:
        task_type = "customer_profile"

        def execute_once(self) -> str:
            return "idle"

    runner = WorkerRunner(executors=[_Executor()], interval_seconds=0.01)
    assert runner._queue_depth_snapshot() == {"customer_profile": None}
