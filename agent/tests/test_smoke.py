import psycopg


def test_agent_login_is_not_superuser(agent_url):
    with psycopg.connect(agent_url) as conn:
        row = conn.execute(
            "select session_user, rolsuper, rolbypassrls from pg_roles where rolname = session_user"
        ).fetchone()
    assert row == ("ordering_agent", False, False)


def test_menu_seeded(admin_conn):
    n = admin_conn.execute("select count(*) from app.menu_items").fetchone()[0]
    assert n == 12
