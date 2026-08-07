#!/usr/bin/env python3
"""Apply SQL migrations in filename order, once each."""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).parent.resolve()
ROOT = HERE.parent

# `python3 db/migrate.py` puts db/ on sys.path, not the repo root, so
# `from db import ...` (and anything importing this module as a script)
# can't resolve -- Python never adds the parent of the script's own
# directory. `python3 -m db.migrate` doesn't have this problem, but the
# direct form is what people reach for by habit, so make both work.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg

MIGRATIONS = HERE / "migrations"
ENV_FILE = pathlib.Path("/root/.pennyrun-db.env")


def db_url():
    url = os.environ.get("PENNYRUN_DB_URL")
    if url:
        return url
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("PENNYRUN_DB_URL="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no PENNYRUN_DB_URL in env or /root/.pennyrun-db.env")


def connect(url=None):
    return psycopg.connect(url or db_url())


def applied(conn):
    with conn.cursor() as cur:
        cur.execute("create table if not exists schema_migration ("
                    "name text primary key, applied_at timestamptz not null default now())")
        conn.commit()
        cur.execute("select name from schema_migration")
        return {r[0] for r in cur.fetchall()}


def apply(conn):
    done = applied(conn)
    ran = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute("insert into schema_migration (name) values (%s)", (path.name,))
        conn.commit()
        ran.append(path.name)
    return ran


if __name__ == "__main__":
    with connect() as c:
        for name in apply(c) or ["(nothing new)"]:
            print("applied", name)
