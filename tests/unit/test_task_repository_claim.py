from __future__ import annotations

from src.infrastructure.db.task_repository import MySQLTaskRepository

_ALL_COLUMNS = {"work_env", "batch_id", "task_type"}

_QUEUED_ROW = {
    "id": 7,
    "customer_phonenumber": "13800138000",
    "status": "queued",
    "updated_at": None,
    "output": None,
    "card_id": None,
    "nickname": None,
    "error_message": None,
    "work_env": "production",
    "batch_id": None,
    "task_type": "invitation_strategy",
}


class _FakeCursor:
    def __init__(self, conn: "_FakeConn", dictionary: bool):
        self._conn = conn
        self._dictionary = dictionary
        self._result: dict | None = None
        self.rowcount = -1

    def execute(self, sql: str, params=None) -> None:
        self._conn.executed.append((sql, params))
        normalized = " ".join(sql.split())
        if "information_schema.columns" in normalized:
            column = params[0]
            self._result = {"1": 1} if column in self._conn.columns else None
        elif normalized.startswith("SELECT") and "WHERE status = 'queued'" in normalized:
            self._result = dict(self._conn.queued_row) if self._conn.queued_row else None
        elif normalized.startswith("UPDATE customer_profile_tasks"):
            self.rowcount = 1
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, columns: set[str], queued_row: dict | None):
        self.columns = columns
        self.queued_row = queued_row
        self.executed: list[tuple[str, object]] = []

    def cursor(self, dictionary: bool = False):
        return _FakeCursor(self, dictionary)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeDbClient:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def connect(self):
        return self._conn


def _claim_selects(conn: _FakeConn) -> list[tuple[str, object]]:
    return [
        (" ".join(sql.split()), params)
        for sql, params in conn.executed
        if "WHERE status = 'queued'" in " ".join(sql.split())
    ]


def test_claim_filters_by_task_type_when_column_exists():
    conn = _FakeConn(columns=set(_ALL_COLUMNS), queued_row=dict(_QUEUED_ROW))
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    record = repo.claim_next_queued(task_type="invitation_strategy")

    assert record is not None
    assert record.task_type == "invitation_strategy"
    assert record.status == "processing"
    selects = _claim_selects(conn)
    assert len(selects) == 1
    sql, params = selects[0]
    assert "task_type = %s" in sql
    assert params == ("invitation_strategy",)


def test_claim_without_task_type_stays_unfiltered():
    conn = _FakeConn(columns=set(_ALL_COLUMNS), queued_row=dict(_QUEUED_ROW))
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    record = repo.claim_next_queued()

    assert record is not None
    selects = _claim_selects(conn)
    assert len(selects) == 1
    sql, _params = selects[0]
    assert "task_type = %s" not in sql


def test_claim_non_default_type_returns_none_when_column_missing():
    """Without the task_type column every row reads as customer_profile, so a
    type-specific pool for any other type must not claim anything."""
    conn = _FakeConn(columns={"work_env", "batch_id"}, queued_row=dict(_QUEUED_ROW))
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    record = repo.claim_next_queued(task_type="invitation_strategy")

    assert record is None
    assert _claim_selects(conn) == []


def test_claim_default_type_claims_unfiltered_when_column_missing():
    conn = _FakeConn(columns={"work_env", "batch_id"}, queued_row=dict(_QUEUED_ROW))
    repo = MySQLTaskRepository(_FakeDbClient(conn))

    record = repo.claim_next_queued(task_type="customer_profile")

    assert record is not None
    selects = _claim_selects(conn)
    assert len(selects) == 1
    sql, _params = selects[0]
    assert "task_type = %s" not in sql
