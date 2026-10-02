"""Choose a stampable Alembic target when multiple heads exist."""

from __future__ import annotations

from alembic.script import ScriptDirectory

from app.db.migrate import VERSION_NUM_MAX, alembic_config, choose_upgrade_target


class _Rev:
    def __init__(self, revision: str, down_revision: str | None):
        self.revision = revision
        self.down_revision = down_revision


class _Script:
    def __init__(self, heads: list[str], revs: dict[str, _Rev]):
        self._heads = heads
        self._revs = revs

    def get_heads(self):
        return list(self._heads)

    def get_revision(self, revision_id: str):
        return self._revs.get(revision_id)


def test_single_stampable_head_uses_head():
    script = _Script(
        ["006_live_sync_job_idx"],
        {"006_live_sync_job_idx": _Rev("006_live_sync_job_idx", "005_sportybet_sync_jobs")},
    )
    assert choose_upgrade_target(script) == "head"


def test_ignores_revision_id_longer_than_version_num():
    long_id = "006_sportybet_sync_jobs_created_at_index"
    assert len(long_id) > VERSION_NUM_MAX
    script = _Script(
        [long_id, "006_live_sync_job_idx"],
        {
            long_id: _Rev(long_id, "005_sportybet_sync_jobs"),
            "006_live_sync_job_idx": _Rev("006_live_sync_job_idx", "005_sportybet_sync_jobs"),
        },
    )
    assert choose_upgrade_target(script) == "006_live_sync_job_idx"


def test_prefers_deeper_stampable_head():
    script = _Script(
        ["006_live_sync_job_idx", "006b_job_created_idx"],
        {
            "006_live_sync_job_idx": _Rev("006_live_sync_job_idx", "005_sportybet_sync_jobs"),
            "006b_job_created_idx": _Rev("006b_job_created_idx", "006_live_sync_job_idx"),
        },
    )
    assert choose_upgrade_target(script) == "006b_job_created_idx"


def test_real_scripts_have_one_stampable_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert choose_upgrade_target(script) == "head"
    assert script.get_heads() == ["008_nowpayments"]
