import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import update_fpl_data as updater


def history_row(gameweek, fixture, points):
    return {
        "assists": 0,
        "element": 1,
        "fixture": fixture,
        "minutes": 90,
        "round": gameweek,
        "total_points": points,
        "was_home": True,
    }


class SettlementTests(unittest.TestCase):
    def test_existing_gameweek_numbers_ignores_merged_and_xp_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            gws = Path(temporary)
            for filename in ("gw2.csv", "gw1.csv", "merged_gw.csv", "xP1.csv"):
                (gws / filename).write_text("header\n", encoding="utf-8")
            self.assertEqual(updater.existing_gameweek_numbers(gws), [1, 2])

    def test_started_unsettled_event_is_reported_unsafe(self):
        events = [
            {
                "id": 1,
                "deadline_time": "2026-08-21T17:30:00Z",
                "finished": True,
                "data_checked": True,
            },
            {
                "id": 2,
                "deadline_time": "2026-08-28T17:30:00Z",
                "finished": False,
                "data_checked": False,
            },
        ]
        fixtures = [
            {
                "event": 1,
                "finished": True,
                "finished_provisional": True,
                "team_h_score": 1,
                "team_a_score": 0,
            },
            {
                "event": 2,
                "finished": False,
                "finished_provisional": False,
                "team_h_score": None,
                "team_a_score": None,
            },
        ]
        settled, unsafe = updater.settled_gameweeks(
            events, fixtures, now=datetime(2026, 8, 29, tzinfo=timezone.utc)
        )
        self.assertEqual(settled, [1])
        self.assertEqual(unsafe, [2])

    def test_schema_removal_fails_clearly(self):
        with self.assertRaisesRegex(updater.UpdateError, "total_points"):
            updater.require_keys(
                {"element": 1, "fixture": 1, "round": 1, "was_home": True},
                updater.REQUIRED_GW_KEYS,
                "test history",
            )


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.season = self.root / "data" / "2026-27"
        (self.season / "players").mkdir(parents=True)
        for filename in updater.REQUIRED_SEASON_FILES:
            (self.season / filename).write_text("placeholder\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_stage_backfills_all_missing_weeks_and_merges(self):
        bootstrap = {
            "elements": [
                {
                    "id": 1,
                    "first_name": "Test",
                    "second_name": "Player",
                    "element_type": 3,
                    "team": 1,
                    "ep_this": "2.0",
                    "now_cost": 50,
                    "total_points": 11,
                }
            ],
            "element_types": [{"id": 3, "singular_name_short": "MID"}],
            "teams": [{"id": 1, "name": "Home"}, {"id": 2, "name": "Away"}],
        }
        fixtures = [
            {
                "id": 101,
                "event": 1,
                "finished": True,
                "finished_provisional": True,
                "team_h": 1,
                "team_a": 2,
            },
            {
                "id": 102,
                "event": 2,
                "finished": True,
                "finished_provisional": True,
                "team_h": 1,
                "team_a": 2,
            },
        ]
        summaries = {
            1: {
                "fixtures": [],
                "history": [history_row(1, 101, 5), history_row(2, 102, 6)],
                "history_past": [{"season_name": "2025/26", "total_points": 90}],
            }
        }
        stage = self.root / "stage"
        stage.mkdir()
        created, skipped, merged_rows, merged_columns, directories = updater.stage_update(
            stage,
            self.season,
            bootstrap,
            fixtures,
            summaries,
            latest_gameweek=2,
            force=False,
        )

        self.assertEqual(created, [1, 2])
        self.assertEqual(skipped, [])
        self.assertEqual(merged_rows, 2)
        self.assertGreater(merged_columns, 1)
        self.assertEqual(directories, {1: "Test_Player_1"})

        with (stage / "gws" / "merged_gw.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["GW"] for row in rows], ["1", "2"])
        self.assertEqual([row["xP"] for row in rows], ["0.0", "0.0"])
        self.assertEqual(rows[0]["position"], "MID")
        self.assertEqual(rows[0]["team"], "Home")

    def test_stage_excludes_history_after_latest_settled_week(self):
        bootstrap = {
            "elements": [
                {
                    "id": 1,
                    "first_name": "Test",
                    "second_name": "Player",
                    "element_type": 3,
                    "team": 1,
                    "ep_this": "2.0",
                    "now_cost": 50,
                    "total_points": 11,
                }
            ],
            "element_types": [{"id": 3, "singular_name_short": "MID"}],
            "teams": [{"id": 1, "name": "Home"}, {"id": 2, "name": "Away"}],
        }
        fixtures = [
            {
                "id": 101,
                "event": 1,
                "finished": True,
                "finished_provisional": True,
                "team_h": 1,
                "team_a": 2,
            },
            {
                "id": 102,
                "event": 2,
                "finished": False,
                "finished_provisional": False,
                "team_h": 1,
                "team_a": 2,
            },
        ]
        summaries = {
            1: {
                "fixtures": [],
                "history": [history_row(1, 101, 5), history_row(2, 102, 6)],
                "history_past": [],
            }
        }
        stage = self.root / "stage-filtered"
        stage.mkdir()
        created, _, merged_rows, _, directories = updater.stage_update(
            stage,
            self.season,
            bootstrap,
            fixtures,
            summaries,
            latest_gameweek=1,
            force=False,
        )

        self.assertEqual(created, [1])
        self.assertEqual(merged_rows, 1)
        self.assertFalse((stage / "gws" / "gw2.csv").exists())
        player_gw = stage / "players" / directories[1] / "gw.csv"
        with player_gw.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["round"] for row in rows], ["1"])

    def test_existing_valid_week_is_not_rewritten(self):
        gws = self.season / "gws"
        gws.mkdir()
        updater.write_csv(
            gws / "gw1.csv",
            list(updater.GAMEWEEK_PREFIX_COLUMNS) + list(updater.REQUIRED_GW_KEYS),
            [
                {
                    "name": "Existing Player",
                    "position": "MID",
                    "team": "Home",
                    "xP": 1.2,
                    "element": 1,
                    "fixture": 101,
                    "round": 1,
                    "was_home": True,
                    "total_points": 5,
                }
            ],
        )
        original = (gws / "gw1.csv").read_bytes()

        bootstrap = {
            "elements": [
                {
                    "id": 1,
                    "first_name": "Test",
                    "second_name": "Player",
                    "element_type": 3,
                    "team": 1,
                    "ep_this": "2.0",
                    "now_cost": 50,
                    "total_points": 5,
                }
            ],
            "element_types": [{"id": 3, "singular_name_short": "MID"}],
            "teams": [{"id": 1, "name": "Home"}, {"id": 2, "name": "Away"}],
        }
        fixtures = [
            {
                "id": 101,
                "event": 1,
                "finished": True,
                "finished_provisional": True,
                "team_h": 1,
                "team_a": 2,
            }
        ]
        summaries = {
            1: {
                "fixtures": [],
                "history": [history_row(1, 101, 5)],
                "history_past": [],
            }
        }
        stage = self.root / "stage-existing"
        stage.mkdir()
        created, skipped, _, _, _ = updater.stage_update(
            stage,
            self.season,
            bootstrap,
            fixtures,
            summaries,
            latest_gameweek=1,
            force=False,
        )
        self.assertEqual(created, [])
        self.assertEqual(skipped, [1])
        self.assertEqual((gws / "gw1.csv").read_bytes(), original)

        snapshot_stage = self.root / "stage-xpoints-snapshot"
        snapshot_stage.mkdir()
        created, skipped, _, _, _ = updater.stage_update(
            snapshot_stage,
            self.season,
            bootstrap,
            fixtures,
            summaries,
            latest_gameweek=1,
            force=False,
            xpoints_snapshot_gameweek=1,
        )
        self.assertEqual(created, [1])
        self.assertEqual(skipped, [])
        with (snapshot_stage / "gws" / "xP1.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            xpoints = list(csv.DictReader(handle))
        self.assertEqual(xpoints, [{"id": "1", "xP": "2.0"}])
        with (snapshot_stage / "gws" / "gw1.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            rebuilt = list(csv.DictReader(handle))
        self.assertEqual(rebuilt[0]["xP"], "2.0")


if __name__ == "__main__":
    unittest.main()
