"""Tests for SQLite persistence layer."""

import staff_duty.database as db


class TestConfigCRUD:
    """Config save/get/list/delete operations."""

    def test_save_and_get_config(self, tmp_db):
        """Save a config, retrieve it, verify match."""
        config_data = {"start": "2026-07-01", "dirs": [{"name": "G1", "eligible": 5}]}
        config_id = db.save_config("Q3 2026", config_data)

        result = db.get_config(config_id)
        assert result is not None
        assert result["name"] == "Q3 2026"
        assert result["config"] == config_data
        assert result["id"] == config_id

    def test_list_configs(self, tmp_db):
        """Save 3 configs, list returns 3."""
        for i in range(3):
            db.save_config(f"Config {i}", {"index": i})

        configs = db.list_configs()
        assert len(configs) == 3

    def test_delete_config(self, tmp_db):
        """Save, delete, get returns None."""
        config_id = db.save_config("Temp", {"temp": True})
        assert db.get_config(config_id) is not None

        deleted = db.delete_config(config_id)
        assert deleted is True
        assert db.get_config(config_id) is None


class TestRosterCRUD:
    """Roster save/get/list/update operations."""

    def _save_sample_roster(self):
        """Helper: save a roster and return its id."""
        return db.save_roster(
            config_id=None,
            quarter="Q3 2026",
            solver_status="Optimal",
            gini_sdnco=0.05,
            gini_runner=0.06,
            roster_json={"2026-07-01": "G1", "2026-07-02": "G2"},
            config_json={"dirs": ["G1", "G2"]},
        )

    def test_save_and_get_roster(self, tmp_db):
        """Save roster with metadata, retrieve and verify."""
        roster_id = self._save_sample_roster()

        result = db.get_roster(roster_id)
        assert result is not None
        assert result["quarter"] == "Q3 2026"
        assert result["solver_status"] == "Optimal"
        assert result["gini_sdnco"] == 0.05
        assert result["gini_runner"] == 0.06
        assert result["roster"]["2026-07-01"] == "G1"

    def test_list_rosters_pagination(self, tmp_db):
        """Save 25 rosters, page 1 has 20, page 2 has 5."""
        for i in range(25):
            db.save_roster(
                config_id=None,
                quarter=f"Q{i}",
                solver_status="Optimal",
                gini_sdnco=0.0,
                gini_runner=0.0,
                roster_json={},
                config_json={},
            )

        page1, total = db.list_rosters(page=1, per_page=20)
        assert total == 25
        assert len(page1) == 20

        page2, total2 = db.list_rosters(page=2, per_page=20)
        assert total2 == 25
        assert len(page2) == 5

    def test_update_swaps(self, tmp_db):
        """Save roster, update swaps, verify persisted."""
        roster_id = self._save_sample_roster()

        swaps = [{"from": "2026-07-01", "to": "2026-07-03", "dir": "G1"}]
        updated = db.update_roster_swaps(roster_id, swaps)
        assert updated is True

        result = db.get_roster(roster_id)
        assert result["swaps"] == swaps

    def test_update_soldiers(self, tmp_db):
        """Save roster, update soldiers, verify persisted."""
        roster_id = self._save_sample_roster()

        soldiers = [
            {"date": "2026-07-01", "role": "SDNCO", "name": "SGT Smith"},
            {"date": "2026-07-02", "role": "SDNCO", "name": "SSG Jones"},
        ]
        updated = db.update_roster_soldiers(roster_id, soldiers)
        assert updated is True

        result = db.get_roster(roster_id)
        assert result["soldier_assignments"] == soldiers
