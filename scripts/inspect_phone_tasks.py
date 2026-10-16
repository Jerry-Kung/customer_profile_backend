"""Quick inspection script for the duplicate-execution investigation.

Connection settings and the target phone number are read from the environment
(same variable names as the app config), so no credentials are stored in the
file. Load the project .env first, then run:

    python scripts/inspect_phone_tasks.py

Environment:
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
    TARGET_PHONE  (required)  the customer phone number to inspect
    TARGET_IDS    (optional)  comma-separated task ids, e.g. "928,942"

Outputs:
  - SQL 1: rows whose id is in TARGET_IDS, or whose phone matches TARGET_PHONE
  - SQL 2: all rows for TARGET_PHONE
"""

from __future__ import annotations

import os

import pymysql


HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MYSQL_PORT", "3306"))
USER = os.environ["MYSQL_USER"]
PASSWORD = os.environ["MYSQL_PASSWORD"]
DATABASE = os.environ.get("MYSQL_DATABASE", "customer_profile")

TARGET_IDS = tuple(
    int(part) for part in os.environ.get("TARGET_IDS", "").split(",") if part.strip()
)
TARGET_PHONE = os.environ["TARGET_PHONE"]


SQL_1 = """
SELECT id, customer_phonenumber, task_type, status, error_message,
       created_at, updated_at
FROM customer_profile_tasks
WHERE id IN ({id_placeholders})
   OR customer_phonenumber = %s
ORDER BY id
"""

SQL_1_BY_PHONE_ONLY = """
SELECT id, customer_phonenumber, task_type, status, error_message,
       created_at, updated_at
FROM customer_profile_tasks
WHERE customer_phonenumber = %s
ORDER BY id
"""

SQL_2 = """
SELECT id, customer_phonenumber, status,
       LEFT(error_message, 80) AS err,
       updated_at
FROM customer_profile_tasks
WHERE customer_phonenumber = %s
"""


def _print_rows(title: str, columns: list[str], rows: list[tuple]) -> None:
    print(f"\n===== {title} =====")
    if not rows:
        print("(no rows)")
        return
    widths = [len(c) for c in columns]
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep = "-+-".join("-" * w for w in widths)
    print(header)
    print(sep)
    for row in str_rows:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> int:
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        connect_timeout=5,
    )
    try:
        cursor = conn.cursor()

        if TARGET_IDS:
            placeholders = ", ".join(["%s"] * len(TARGET_IDS))
            cursor.execute(
                SQL_1.format(id_placeholders=placeholders),
                (*TARGET_IDS, TARGET_PHONE),
            )
        else:
            cursor.execute(SQL_1_BY_PHONE_ONLY, (TARGET_PHONE,))
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        _print_rows(
            f"SQL 1: id IN {TARGET_IDS} OR customer_phonenumber = {TARGET_PHONE!r}",
            columns,
            rows,
        )

        cursor.execute(SQL_2, (TARGET_PHONE,))
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        _print_rows(
            f"SQL 2: all rows for customer_phonenumber = {TARGET_PHONE!r}",
            columns,
            rows,
        )

        cursor.close()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
