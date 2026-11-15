"""Remember which archive the scanner was working on, so one that kills it gets skipped.

A hostile or huge archive can push the scanner past its container memory limit. The
kernel then SIGKILLs it: no exception, nothing recorded, and the next run would parse
the same archive first and die again, forever. So the attempt is committed before the
work starts and removed when it ends, however it ends. An attempt still on disk at the
next run means the process died during it.
"""

import sqlite3

# Two, not one: a single `docker stop` mid-archive must not condemn a good archive.
MAX_ATTEMPTS = 2


def begin(conn: sqlite3.Connection, file_id: int, stage: str) -> bool:
    """Record an attempt. False if earlier attempts already died MAX_ATTEMPTS times."""
    row = conn.execute(
        "SELECT attempts FROM scan_attempts WHERE file_id = ? AND stage = ?", (file_id, stage)
    ).fetchone()
    if row is not None and row[0] >= MAX_ATTEMPTS:
        return False
    with conn:
        conn.execute(
            """
            INSERT INTO scan_attempts (file_id, stage, attempts) VALUES (?, ?, 1)
            ON CONFLICT (file_id, stage) DO UPDATE SET attempts = attempts + 1
            """,
            (file_id, stage),
        )
    return True


def finish(conn: sqlite3.Connection, file_id: int, stage: str) -> None:
    with conn:
        conn.execute("DELETE FROM scan_attempts WHERE file_id = ? AND stage = ?", (file_id, stage))


def clear(conn: sqlite3.Connection) -> None:
    """Give every archive a fresh chance (e.g. after raising the memory limit)."""
    with conn:
        conn.execute("DELETE FROM scan_attempts")
