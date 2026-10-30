"""Fixtures every app's tests share: one throwaway Postgres for the run, a database per test."""

from collections.abc import Callable, Generator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer

# The image Compose runs, so tests meet the same server.
POSTGRES_IMAGE = "postgres:18.6"


@pytest.fixture(scope="session")
def postgres_server() -> Generator[str]:
    """The admin connection string of a Postgres that lives for the whole run."""
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as server:
        yield server.get_connection_url()


@pytest.fixture(scope="session")
def make_database(postgres_server: str) -> Callable[[], str]:
    """Creates an empty database and returns its connection string."""

    def make() -> str:
        name = f"test_{uuid4().hex}"
        with psycopg.connect(postgres_server, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        return make_conninfo(postgres_server, dbname=name)

    return make


@pytest.fixture
def database(make_database: Callable[[], str]) -> str:
    """The connection string of an empty database of this test's own."""
    return make_database()
