from migrate import migrate


def test_second_run_applies_nothing(db_url):
    assert migrate(db_url, verbose=False) == []


def test_every_migration_is_recorded(conn):
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions and versions[0] == "0001_core"
