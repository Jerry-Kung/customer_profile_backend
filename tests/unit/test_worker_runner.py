from __future__ import annotations

import logging
import threading
import time

from src.worker.runner import WorkerRunner


class _Executor:
    def __init__(self, status: str, task_type: str | None = None):
        self.status = status
        self.task_type = task_type
        self.calls = 0
        self._lock = threading.Lock()

    def execute_once(self) -> str:
        with self._lock:
            self.calls += 1
        return self.status


def test_worker_run_once_idle_logged_at_debug_only(caplog):
    runner = WorkerRunner(executors=[_Executor("idle")], interval_seconds=0.01)
    with caplog.at_level(logging.DEBUG, logger="worker"):
        runner.run_once(_Executor("idle"))
    assert any("worker_tick status=idle" in record.getMessage() for record in caplog.records)


def test_worker_run_once_claimed_logs(caplog):
    runner = WorkerRunner(executors=[_Executor("succeeded")], interval_seconds=0.01)
    runner.run_once(_Executor("succeeded"))
    assert any("worker_tick status=succeeded" in record.getMessage() for record in caplog.records)


def test_worker_can_start_and_stop():
    runner = WorkerRunner(executors=[_Executor("idle")], interval_seconds=0.02)
    t = threading.Thread(target=runner.run_forever, daemon=True)
    t.start()
    time.sleep(0.06)
    runner.stop()
    t.join(timeout=1.0)
    assert not t.is_alive()


def test_runner_drives_every_executor():
    """Each task-type pool must actually run; a pool that is never invoked would
    silently starve that task type — the bug we are fixing."""
    cp = _Executor("idle", task_type="customer_profile")
    inv = _Executor("idle", task_type="invitation_strategy")
    runner = WorkerRunner(executors=[cp, inv], interval_seconds=0.01, concurrency=2)
    t = threading.Thread(target=runner.run_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    runner.stop()
    t.join(timeout=1.0)
    assert cp.calls > 0
    assert inv.calls > 0


def test_runner_spawns_concurrency_threads_per_executor():
    cp = _Executor("idle", task_type="customer_profile")
    inv = _Executor("idle", task_type="invitation_strategy")
    runner = WorkerRunner(executors=[cp, inv], interval_seconds=5.0, concurrency=3)
    t = threading.Thread(target=runner.run_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    worker_threads = [
        th
        for th in threading.enumerate()
        if th.name.startswith("worker-") and th.name != "worker-supervisor"
    ]
    runner.stop()
    t.join(timeout=1.0)
    # 3 threads per executor * 2 executors = 6 worker threads.
    assert len(worker_threads) == 6


