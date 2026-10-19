from __future__ import annotations

import threading
import time
from collections.abc import Sequence

from src.config.logging_utils import get_logger
from src.worker.executor import WorkerExecutor


def _executor_label(executor: WorkerExecutor) -> str:
    return getattr(executor, "task_type", None) or "any"


class WorkerRunner:
    """Drives one consumption pool per executor.

    Each executor gets its own `concurrency` threads, so with two task-type
    executors and WORKER_CONCURRENCY=N the process runs 2*N worker threads and
    a slow/stuck task type can never starve the other one.
    """

    def __init__(
        self,
        executors: Sequence[WorkerExecutor],
        interval_seconds: float = 2.0,
        *,
        concurrency: int = 1,
        supervisor_interval_seconds: float = 60.0,
    ):
        if not executors:
            raise ValueError("at least one executor is required")
        self._executors = list(executors)
        self._interval_seconds = interval_seconds
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._concurrency = concurrency
        self._supervisor_interval_seconds = supervisor_interval_seconds
        self._stop_event = threading.Event()
        self._logger = get_logger("worker")
        # One slot per (executor, index) pair; parallel to self._threads.
        self._slots: list[tuple[WorkerExecutor, str]] = [
            (executor, f"worker-{_executor_label(executor)}-{i}")
            for executor in self._executors
            for i in range(self._concurrency)
        ]
        self._threads: list[threading.Thread] = []
        self._heartbeats: dict[str, float] = {}
        self._heartbeats_lock = threading.Lock()
        self._supervisor_thread: threading.Thread | None = None

    def run_once(self, executor: WorkerExecutor) -> None:
        status = executor.execute_once()
        # Avoid INFO spam every poll interval when there is no queued work.
        if status == "idle":
            self._logger.debug("worker_tick status=idle task_type=%s", _executor_label(executor))
        else:
            self._logger.info("worker_tick status=%s task_type=%s", status, _executor_label(executor))

    def _loop(self, executor: WorkerExecutor, thread_name: str) -> None:
        while not self._stop_event.is_set():
            with self._heartbeats_lock:
                self._heartbeats[thread_name] = time.monotonic()
            try:
                self.run_once(executor)
            except Exception:
                # P0 outer guard: catches anything that escaped execute_once's own handlers
                # (including failures inside those handlers themselves). The thread MUST survive.
                self._logger.exception(
                    "worker_loop_unexpected_error thread=%s", thread_name,
                )
            self._stop_event.wait(self._interval_seconds)

    def _spawn_worker(self, slot_index: int) -> threading.Thread:
        executor, name = self._slots[slot_index]
        with self._heartbeats_lock:
            self._heartbeats[name] = time.monotonic()
        t = threading.Thread(target=self._loop, name=name, args=(executor, name), daemon=False)
        t.start()
        return t

    def _queue_depth_snapshot(self) -> dict[str, int | None]:
        """Queued depth per pool, for the heartbeat.

        A worker whose pools all report 0 is polling an empty queue; that is a completely
        different situation from a pool that keeps seeing work and never claims it, and
        without this both look like silence at INFO level.
        """
        snapshot: dict[str, int | None] = {}
        for executor in self._executors:
            label = _executor_label(executor)
            probe = getattr(executor, "queue_depth", None)
            if probe is None:
                snapshot[label] = None
                continue
            try:
                snapshot[label] = probe()
            except Exception:
                # The probe is diagnostic; a failure must never break the supervisor.
                snapshot[label] = None
        return snapshot

    def _supervisor_loop(self) -> None:
        while True:
            # wait() returns True iff stop_event was set; that is our exit signal.
            if self._stop_event.wait(self._supervisor_interval_seconds):
                return
            try:
                self._check_and_respawn()
            except Exception:
                self._logger.exception("worker_supervisor_check_failed")
            try:
                self._logger.info(
                    "worker_queue_depth pools=%s",
                    self._queue_depth_snapshot(),
                )
            except Exception:
                self._logger.exception("worker_queue_depth_log_failed")

    def _check_and_respawn(self) -> None:
        alive_after = 0
        respawned: list[str] = []
        for i in range(len(self._threads)):
            t = self._threads[i]
            if t.is_alive():
                alive_after += 1
                continue
            old_name = t.name
            self._logger.warning(
                "worker_thread_respawned name=%s reason=died_silently",
                old_name,
            )
            new_t = self._spawn_worker(i)
            self._threads[i] = new_t
            respawned.append(old_name)
            alive_after += 1
        now = time.monotonic()
        with self._heartbeats_lock:
            heartbeats_snapshot = dict(self._heartbeats)
        stale = [
            name
            for name, ts in heartbeats_snapshot.items()
            if now - ts > self._supervisor_interval_seconds * 5
        ]
        # Demote the steady-state "all alive, nothing stale" heartbeat to DEBUG so it does not
        # flood production INFO logs every supervisor tick. Anything actionable stays at INFO.
        expected = len(self._slots)
        if respawned or stale or alive_after != expected:
            self._logger.info(
                "worker_heartbeat alive=%s/%s respawned=%s stale=%s",
                alive_after,
                expected,
                respawned,
                stale,
            )
        else:
            self._logger.debug(
                "worker_heartbeat alive=%s/%s respawned=%s stale=%s",
                alive_after,
                expected,
                respawned,
                stale,
            )

    def run_forever(self) -> None:
        self._logger.info(
            "worker_started concurrency=%s pools=%s total_threads=%s",
            self._concurrency,
            [_executor_label(e) for e in self._executors],
            len(self._slots),
        )
        self._threads = [self._spawn_worker(i) for i in range(len(self._slots))]
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="worker-supervisor",
            daemon=False,
        )
        self._supervisor_thread.start()
        try:
            # Park the main thread on stop_event; supervisor keeps workers alive in between.
            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)
        except KeyboardInterrupt:
            self._logger.info("worker_shutdown_requested")
        finally:
            self._stop_event.set()
            # supervisor will observe stop_event and exit; it will not respawn anything from here on.
            for t in list(self._threads):
                t.join(timeout=30.0)
            if self._supervisor_thread is not None:
                self._supervisor_thread.join(timeout=10.0)
            self._logger.info("worker_stopped")

    def stop(self) -> None:
        self._stop_event.set()
