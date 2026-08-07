"""Database access for the read API.

Two deliberate departures from a naive port of `db.migrate`:

- `db_url()` is called lazily, inside `rows()`, not at import time. Calling
  it at module import (`_pool_url = db_url()`) makes `api.main` unimportable
  whenever `PENNYRUN_DB_URL` is unset and `/root/.pennyrun-db.env` doesn't
  exist -- which is every CI run and most local test setups. Deferring it
  into the context manager keeps the same public interface (`rows()` as a
  context manager yielding a cursor) while making the module importable
  under `pytest --collect-only` and any other situation that loads
  `api.main` without a live database configured.

- The cursor's row factory stringifies `Decimal` values instead of handing
  them to FastAPI as-is. FastAPI's `jsonable_encoder` turns any `Decimal`
  it sees into `int`/`float` on the way out (see `fastapi.encoders`), so a
  `numeric(10,2)` clearance price of `1.20` would silently become the JSON
  number `1.2` -- correct in value, wrong in the one way a phone showing a
  price actually cares about (trailing zero). Money is `numeric(10,2)` in
  Postgres and `Decimal` in Python everywhere else in this codebase; the
  read API's job is to hand that value to a phone without ever routing it
  through a float, so it converts to the exact same text Postgres would
  print, at the row-fetch boundary, once, for every endpoint at once.

`PENNYRUN_DB_SCHEMA` is not part of the production interface -- it exists
so tests running inside an isolated Postgres schema (see
`tests/conftest.py`) can make the API's own connections see that schema
too, without leaking a test-only knob into `api/main.py`'s endpoint code.
"""
from contextlib import contextmanager
from decimal import Decimal
import os

import psycopg
from psycopg.rows import dict_row

from db.migrate import db_url


def _decimal_safe_dict_row(cursor):
    make_dict = dict_row(cursor)

    def make_row(values):
        row = make_dict(values)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}

    return make_row


@contextmanager
def rows():
    with psycopg.connect(db_url(), row_factory=_decimal_safe_dict_row) as conn:
        with conn.cursor() as cur:
            schema = os.environ.get("PENNYRUN_DB_SCHEMA")
            if schema:
                cur.execute(f"set search_path to {schema}")
            yield cur
