import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("EVAL_ADMIN_DATABASE_URL")
    if not url:
        pytest.skip("EVAL_ADMIN_DATABASE_URL is not set; no test database available")
    return url


@pytest.fixture(scope="session")
def admin_conn(admin_url):
    # schema.sql creates cluster-wide roles, so it can be loaded only once per
    # server: the database must be fresh (docker compose down -v, then up).
    conn = psycopg.connect(admin_url, autocommit=True)
    conn.execute((ROOT / "schema.sql").read_text())
    conn.execute((ROOT / "evals" / "fixtures" / "menu.sql").read_text())
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def agent_url(admin_url, admin_conn) -> str:
    # Same server and database, but as the login the agent runs under (trust auth).
    parts = urlsplit(admin_url)
    netloc = "ordering_agent@" + parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=netloc))
