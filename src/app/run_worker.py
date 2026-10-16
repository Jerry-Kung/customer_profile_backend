from __future__ import annotations

from src.app.bootstrap import bootstrap
from src.worker.runner import WorkerRunner


def main() -> int:
    config, _, _, executors, _ = bootstrap()
    runner = WorkerRunner(
        executors=executors,
        interval_seconds=config.worker.poll_interval_seconds,
        concurrency=config.worker.concurrency,
    )
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

