"""Validate Alembic logging config and a single linear migration head."""

from __future__ import annotations

import configparser
from logging.config import fileConfig
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

EXPECTED_REVISIONS = (
    "001_initial",
    "002_ops_ledger",
    "003_production_hardening",
    "004_sportybet_external_ids",
    "005_sportybet_sync_jobs",
    "006_live_sync_job_idx",
    "006b_job_created_idx",
    "007_webhook_events",
    "008_nowpayments",
)


def test_alembic_ini_logger_sections_are_complete():
    parser = configparser.ConfigParser()
    read = parser.read(ALEMBIC_INI)
    assert read, f"Could not read {ALEMBIC_INI}"

    logger_keys = [key.strip() for key in parser.get("loggers", "keys").split(",") if key.strip()]
    assert "root" in logger_keys
    assert "sqlalchemy" in logger_keys
    assert "alembic" in logger_keys
    for key in logger_keys:
        assert parser.has_section(f"logger_{key}"), f"missing [logger_{key}]"

    handler_keys = [key.strip() for key in parser.get("handlers", "keys").split(",") if key.strip()]
    for key in handler_keys:
        assert parser.has_section(f"handler_{key}"), f"missing [handler_{key}]"

    formatter_keys = [
        key.strip() for key in parser.get("formatters", "keys").split(",") if key.strip()
    ]
    for key in formatter_keys:
        assert parser.has_section(f"formatter_{key}"), f"missing [formatter_{key}]"


def test_alembic_fileconfig_does_not_raise():
    fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)


def test_alembic_env_reads_database_url_from_environment():
    source = (BACKEND_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert 'os.environ.get("DATABASE_URL")' in source
    assert "reject_sqlite_if_hosted" in source
    assert "prepare_database" in source
    assert "connection.commit()" in source
    assert "disable_existing_loggers=False" in source


def test_procfiles_use_migrate_entrypoint_not_upgrade_head():
    root = BACKEND_ROOT.parent / "Procfile"
    backend = BACKEND_ROOT / "Procfile"
    root_text = root.read_text(encoding="utf-8")
    backend_text = backend.read_text(encoding="utf-8")
    assert "python -m app.db.migrate" in root_text
    assert "python -m app.db.migrate" in backend_text
    assert "alembic upgrade head" not in root_text
    assert "alembic upgrade head" not in backend_text


def test_alembic_upgrade_head_on_sqlite(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from app.core.config import reset_settings_cache

    db_path = (tmp_path / "alembic_sqlite.db").resolve()
    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("DYNO", raising=False)
    reset_settings_cache()

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert "games" in tables
    assert "payment_intents" in tables
    assert "idempotency_keys" in tables
    assert "rate_limit_hits" in tables
    assert "sportybet_sync_jobs" in tables
    assert "payment_webhook_events" in tables
    job_indexes = {
        idx["name"] for idx in inspect(engine).get_indexes("sportybet_sync_jobs")
    }
    assert "ix_sportybet_sync_jobs_sync_type_created_at" in job_indexes
    game_cols = {col["name"] for col in inspect(engine).get_columns("games")}
    assert "external_event_id" in game_cols
    assert "external_game_id" in game_cols
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    payment_cols = {col["name"] for col in inspect(engine).get_columns("payment_intents")}
    engine.dispose()
    reset_settings_cache()
    assert version == "008_nowpayments"
    assert "pay_address" in payment_cols
    assert "provider_status" in payment_cols
    assert "network" in payment_cols


def test_alembic_has_single_expected_head():
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["008_nowpayments"]

    revisions = list(script.walk_revisions())
    ids = [rev.revision for rev in reversed(revisions)]
    assert ids == list(EXPECTED_REVISIONS)

    current = None
    for revision_id in EXPECTED_REVISIONS:
        rev = script.get_revision(revision_id)
        assert len(revision_id) <= 32, revision_id
        assert rev.down_revision == current
        current = revision_id

    for rev in script.walk_revisions():
        assert len(rev.revision) <= 32, rev.revision