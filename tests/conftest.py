import os

import pytest
from db import migrate

# `pennyrun_test` is a shared, persistent database -- migrations recorded in
# `public.schema_migration` by one test run stay there for the next one, so
# `apply()` only looks idempotent-from-empty the very first time anybody
# ever runs this suite. Give each test session its own schema instead: a
# deterministic name (the pid, not a uuid) so a crashed run's leftovers are
# still findable and droppable, created fresh and dropped CASCADE at
# teardown so `apply()` genuinely starts from nothing every run. The
# isolation lives here in the fixture, not in db/migrate.py -- production
# code stays exactly as it runs on the droplet, with no test-only knobs.
#
# Shared across test modules (test_migrate.py, test_seed.py, ...) so every
# module's tests land in the same isolated schema for a given pytest
# session, rather than each module getting its own.
TEST_SCHEMA = f"pennyrun_test_{os.getpid()}"


def _create_schema(name):
    c = migrate.connect()
    try:
        with c.cursor() as cur:
            cur.execute(f"drop schema if exists {name} cascade")
            cur.execute(f"create schema {name}")
        c.commit()
    finally:
        c.close()


def _drop_schema(name):
    c = migrate.connect()
    try:
        with c.cursor() as cur:
            cur.execute(f"drop schema if exists {name} cascade")
        c.commit()
    finally:
        c.close()


def _connect_in(name):
    c = migrate.connect()
    with c.cursor() as cur:
        cur.execute(f"set search_path to {name}")
    c.commit()
    return c


@pytest.fixture(scope="session")
def test_schema():
    _create_schema(TEST_SCHEMA)
    try:
        yield TEST_SCHEMA
    finally:
        _drop_schema(TEST_SCHEMA)


@pytest.fixture
def conn(test_schema):
    """A plain migrate.connect() connection, pointed at the session's
    shared scratch schema via search_path. Unqualified DDL/DML on it,
    including the schema_migration table that applied() creates itself,
    lands in that schema instead of `public`. Several tests share this
    schema; none of them assert on apply()'s return value, only on side
    effects (constraint rejection, trigger rejection, generated-column
    value), so which order they run in doesn't matter."""
    c = migrate.connect()
    with c.cursor() as cur:
        cur.execute(f"set search_path to {test_schema}")
    c.commit()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fresh_conn():
    """Its own schema, created and dropped just for this one test -- not
    the shared session schema. apply()'s first-call/second-call return
    values are asserted here, so this needs a schema that is *actually*
    untouched by any other test, regardless of what order tests run in or
    whether a test earlier in the file (or a random-order plugin) already
    called apply() against the shared schema."""
    name = f"{TEST_SCHEMA}_fresh"
    _create_schema(name)
    c = _connect_in(name)
    try:
        yield c
    finally:
        c.close()
        _drop_schema(name)
