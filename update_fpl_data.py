#!/usr/bin/env python3
"""Safely update one vaastav-style Fantasy Premier League season.

The script intentionally uses only the Python standard library.  It downloads
the public FPL bootstrap, fixture and element-summary endpoints, and writes
gameweek/player-history rows only through the latest fully settled gameweek.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = "https://fantasy.premierleague.com/api"
DEFAULT_SEASON = "2026-27"
USER_AGENT = "vaastav-local-fpl-updater/1.0"

REQUIRED_REPO_FILES = ("global_scraper.py", "collector.py", "getters.py")
REQUIRED_SEASON_FILES = ("players_raw.csv", "teams.csv", "fixtures.csv")
REQUIRED_BOOTSTRAP_KEYS = ("elements", "element_types", "events", "teams")
REQUIRED_EVENT_KEYS = (
    "id",
    "deadline_time",
    "finished",
    "data_checked",
    "is_current",
)
REQUIRED_PLAYER_KEYS = (
    "id",
    "first_name",
    "second_name",
    "element_type",
    "team",
    "ep_this",
    "now_cost",
    "total_points",
)
REQUIRED_TEAM_KEYS = ("id", "name")
REQUIRED_ELEMENT_TYPE_KEYS = ("id", "singular_name_short")
REQUIRED_FIXTURE_KEYS = (
    "id",
    "event",
    "finished",
    "finished_provisional",
    "team_h",
    "team_a",
    "team_h_score",
    "team_a_score",
)
REQUIRED_SUMMARY_KEYS = ("fixtures", "history", "history_past")
REQUIRED_GW_KEYS = (
    "element",
    "fixture",
    "round",
    "was_home",
    "total_points",
)
GAMEWEEK_PREFIX_COLUMNS = ("name", "position", "team", "xP")
CLEANED_PLAYER_COLUMNS = (
    "first_name",
    "second_name",
    "goals_scored",
    "assists",
    "total_points",
    "minutes",
    "goals_conceded",
    "creativity",
    "influence",
    "threat",
    "bonus",
    "bps",
    "ict_index",
    "clean_sheets",
    "red_cards",
    "yellow_cards",
    "selected_by_percent",
    "now_cost",
    "element_type",
    "value_per_m",
)


class UpdateError(RuntimeError):
    """An expected, user-actionable update failure."""


def require_keys(
    value: Mapping[str, Any], required: Iterable[str], context: str
) -> None:
    missing = sorted(set(required).difference(value))
    if missing:
        raise UpdateError(f"{context} is missing required field(s): {', '.join(missing)}")


def parse_api_datetime(value: str, context: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise UpdateError(f"{context} has an invalid API timestamp: {value!r}") from exc


def union_columns(records: Sequence[Mapping[str, Any]], context: str) -> list[str]:
    if not records:
        return []
    columns = set(records[0])
    for record in records[1:]:
        columns.update(record)
    if not columns:
        raise UpdateError(f"{context} has no columns")
    return sorted(columns)


class FplApi:
    def __init__(self, base_url: str, timeout: float, retries: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()

    def get_json(self, endpoint: str) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        last_error: BaseException | None = None
        for attempt in range(1, self.retries + 1):
            request = Request(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                    if status != 200:
                        raise UpdateError(f"GET {url} returned HTTP {status}")
                    body = response.read()
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise UpdateError(f"GET {url} returned invalid JSON") from exc
            except HTTPError as exc:
                last_error = exc
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt < self.retries:
                time.sleep(min(2 ** (attempt - 1), 8))

        detail = str(last_error) if last_error else "unknown network error"
        raise UpdateError(
            f"GET {url} failed after {self.retries} attempt(s): {detail}"
        )


def validate_repo(repo_root: Path, season_dir: Path) -> None:
    missing_repo = [name for name in REQUIRED_REPO_FILES if not (repo_root / name).is_file()]
    if missing_repo:
        raise UpdateError(
            f"{repo_root} does not look like the expected vaastav repository; "
            f"missing: {', '.join(missing_repo)}"
        )
    if not season_dir.is_dir():
        raise UpdateError(
            f"Season directory does not exist: {season_dir}. "
            "Pass its actual location with --season-dir."
        )
    missing_season = [
        name for name in REQUIRED_SEASON_FILES if not (season_dir / name).is_file()
    ]
    if missing_season:
        raise UpdateError(
            f"Season directory {season_dir} is missing expected file(s): "
            f"{', '.join(missing_season)}"
        )
    if not (season_dir / "players").is_dir():
        raise UpdateError(f"Expected player directory is missing: {season_dir / 'players'}")


def validate_bootstrap(bootstrap: Any) -> None:
    if not isinstance(bootstrap, dict):
        raise UpdateError("bootstrap-static response is not a JSON object")
    require_keys(bootstrap, REQUIRED_BOOTSTRAP_KEYS, "bootstrap-static response")
    for key in REQUIRED_BOOTSTRAP_KEYS:
        if not isinstance(bootstrap[key], list) or not bootstrap[key]:
            raise UpdateError(f"bootstrap-static field {key!r} is empty or is not a list")
    for event in bootstrap["events"]:
        require_keys(event, REQUIRED_EVENT_KEYS, "bootstrap event")
    for player in bootstrap["elements"]:
        require_keys(player, REQUIRED_PLAYER_KEYS, "bootstrap player")
    for team in bootstrap["teams"]:
        require_keys(team, REQUIRED_TEAM_KEYS, "bootstrap team")
    for element_type in bootstrap["element_types"]:
        require_keys(
            element_type, REQUIRED_ELEMENT_TYPE_KEYS, "bootstrap element type"
        )


def validate_fixtures(fixtures: Any) -> None:
    if not isinstance(fixtures, list) or not fixtures:
        raise UpdateError("fixtures response is empty or is not a JSON list")
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise UpdateError("fixtures response contains a non-object item")
        require_keys(fixture, REQUIRED_FIXTURE_KEYS, "fixture")


def fixture_is_settled(fixture: Mapping[str, Any]) -> bool:
    return bool(
        fixture["finished"]
        and fixture["finished_provisional"]
        and fixture["team_h_score"] is not None
        and fixture["team_a_score"] is not None
    )


def existing_gameweek_numbers(gws_dir: Path) -> list[int]:
    """Return the gameweek numbers represented by gwN.csv files."""
    if not gws_dir.is_dir():
        return []
    gameweeks: list[int] = []
    for path in gws_dir.iterdir():
        match = re.fullmatch(r"gw(\d+)\.csv", path.name)
        if path.is_file() and match:
            gameweeks.append(int(match.group(1)))
    return sorted(gameweeks)


def log_gameweek_statuses(
    events: Sequence[Mapping[str, Any]],
    fixtures: Sequence[Mapping[str, Any]],
    now: datetime,
) -> None:
    """Print the API evidence used by the settlement safety gate."""
    by_event: dict[int, list[Mapping[str, Any]]] = {}
    for fixture in fixtures:
        event_id = fixture.get("event")
        if event_id is not None:
            by_event.setdefault(int(event_id), []).append(fixture)

    for event in sorted(events, key=lambda item: int(item["id"])):
        event_id = int(event["id"])
        deadline = parse_api_datetime(event["deadline_time"], f"GW{event_id}")
        if not (
            deadline <= now
            or event.get("finished")
            or event.get("is_previous")
            or event.get("is_current")
        ):
            continue
        event_fixtures = by_event.get(event_id, [])
        final_count = sum(fixture_is_settled(item) for item in event_fixtures)
        finished_count = sum(bool(item["finished"]) for item in event_fixtures)
        provisional_count = sum(
            bool(item["finished_provisional"]) for item in event_fixtures
        )
        is_settled = bool(
            event["finished"]
            and event["data_checked"]
            and event_fixtures
            and final_count == len(event_fixtures)
        )
        if is_settled:
            label = "SETTLED"
        elif deadline <= now:
            label = "UNSETTLED/BLOCKING"
        else:
            label = "NOT STARTED"
        print(
            f"[STATUS] GW{event_id}: {label}; "
            f"event.finished={event['finished']}, "
            f"data_checked={event['data_checked']}, "
            f"fixtures final={final_count}/{len(event_fixtures)} "
            f"(finished={finished_count}, provisional={provisional_count})"
        )


def settled_gameweeks(
    events: Sequence[Mapping[str, Any]],
    fixtures: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
) -> tuple[list[int], list[int]]:
    """Return (settled gameweeks, started-but-unsettled gameweeks)."""
    now = now or datetime.now(timezone.utc)
    by_event: dict[int, list[Mapping[str, Any]]] = {}
    for fixture in fixtures:
        event_id = fixture.get("event")
        if event_id is not None:
            by_event.setdefault(int(event_id), []).append(fixture)

    settled: list[int] = []
    unsafe: list[int] = []
    for event in sorted(events, key=lambda item: int(item["id"])):
        event_id = int(event["id"])
        event_fixtures = by_event.get(event_id, [])
        deadline = parse_api_datetime(event["deadline_time"], f"GW{event_id}")
        event_settled = bool(
            event["finished"]
            and event["data_checked"]
            and event_fixtures
            and all(fixture_is_settled(item) for item in event_fixtures)
        )
        if event_settled:
            settled.append(event_id)
        elif deadline <= now:
            unsafe.append(event_id)

    if settled:
        expected = list(range(1, max(settled) + 1))
        if settled != expected:
            raise UpdateError(
                "The API reports non-contiguous settled gameweeks: "
                + ", ".join(str(item) for item in settled)
            )
    return settled, unsafe


def validate_summary(summary: Any, player_id: int) -> None:
    if not isinstance(summary, dict):
        raise UpdateError(f"element-summary/{player_id} is not a JSON object")
    require_keys(summary, REQUIRED_SUMMARY_KEYS, f"element-summary/{player_id}")
    for key in REQUIRED_SUMMARY_KEYS:
        if not isinstance(summary[key], list):
            raise UpdateError(f"element-summary/{player_id} field {key!r} is not a list")
    for row in summary["history"]:
        require_keys(row, REQUIRED_GW_KEYS, f"element-summary/{player_id} history row")
        if int(row["element"]) != player_id:
            raise UpdateError(
                f"element-summary/{player_id} returned history for element {row['element']}"
            )


def fetch_summaries(
    api: FplApi, players: Sequence[Mapping[str, Any]], workers: int
) -> dict[int, Mapping[str, Any]]:
    print(f"[DOWNLOAD] Player histories: 0/{len(players)}", flush=True)
    summaries: dict[int, Mapping[str, Any]] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {
            executor.submit(api.get_json, f"element-summary/{int(player['id'])}/"): int(
                player["id"]
            )
            for player in players
        }
        try:
            for future in as_completed(future_to_id):
                player_id = future_to_id[future]
                summary = future.result()
                validate_summary(summary, player_id)
                summaries[player_id] = summary
                completed += 1
                if completed % 50 == 0 or completed == len(players):
                    print(
                        f"[DOWNLOAD] Player histories: {completed}/{len(players)}",
                        flush=True,
                    )
        except BaseException:
            for future in future_to_id:
                future.cancel()
            raise
    return summaries


def write_csv(
    path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    stringify_values: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            if stringify_values:
                writer.writerow({key: str(row.get(key)) for key in fieldnames})
            else:
                writer.writerow(row)


def build_cleaned_players(players: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    position_names = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD", 5: "AM"}
    cleaned: list[dict[str, Any]] = []
    for player in players:
        row = {key: player.get(key, "") for key in CLEANED_PLAYER_COLUMNS}
        element_type = int(player["element_type"])
        if element_type not in position_names:
            raise UpdateError(f"Unknown element_type {element_type} for player {player['id']}")
        row["element_type"] = position_names[element_type]
        try:
            cost = float(player["now_cost"]) / 10.0
            points = float(player["total_points"])
            row["value_per_m"] = round(points / cost, 1) if cost > 0 else ""
        except (KeyError, TypeError, ValueError):
            row["value_per_m"] = ""
        cleaned.append(row)
    return cleaned


def existing_player_directories(players_dir: Path) -> dict[int, str]:
    result: dict[int, str] = {}
    for path in players_dir.iterdir():
        if not path.is_dir():
            continue
        match = re.search(r"_(\d+)$", path.name)
        if not match:
            raise UpdateError(f"Unexpected player directory name (missing _ID suffix): {path}")
        player_id = int(match.group(1))
        if player_id in result:
            raise UpdateError(f"Duplicate player directories found for element {player_id}")
        result[player_id] = path.name
    return result


def new_player_directory_name(player: Mapping[str, Any]) -> str:
    name = f"{player['first_name']}_{player['second_name']}_{player['id']}"
    invalid = '<>:"/\\|?*'
    found = sorted({character for character in name if character in invalid})
    if found:
        raise UpdateError(
            f"Player {player['id']} name contains Windows path character(s) "
            f"{''.join(found)!r}; cannot create a compatible player directory"
        )
    return name


def read_xpoints(gws_dir: Path, gameweek: int) -> dict[int, Any]:
    path = gws_dir / f"xP{gameweek}.csv"
    if not path.is_file():
        return {}
    result: dict[int, Any] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"id", "xP"}.issubset(reader.fieldnames):
            raise UpdateError(f"Malformed expected-points file: {path}")
        for row in reader:
            result[int(row["id"])] = row["xP"]
    return result


def settled_xpoints_snapshot_gameweek(
    events: Sequence[Mapping[str, Any]], settled: Sequence[int]
) -> int | None:
    """Return the settled current GW whose ep_this values are safe to snapshot."""
    current = [int(event["id"]) for event in events if event["is_current"]]
    if len(current) > 1:
        raise UpdateError(
            "bootstrap-static reports more than one current gameweek: "
            + ", ".join(f"GW{gameweek}" for gameweek in current)
        )
    if current and current[0] in settled:
        return current[0]
    return None


def build_gameweek_rows(
    gameweek: int,
    players_by_id: Mapping[int, Mapping[str, Any]],
    summaries: Mapping[int, Mapping[str, Any]],
    fixtures_by_id: Mapping[int, Mapping[str, Any]],
    team_names: Mapping[int, str],
    position_names: Mapping[int, str],
    xpoints: Mapping[int, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    raw_rows: list[tuple[int, dict[str, Any]]] = []
    for player_id, summary in summaries.items():
        for history in summary["history"]:
            if int(history["round"]) == gameweek:
                raw_rows.append((player_id, dict(history)))
    if not raw_rows:
        raise UpdateError(f"The API returned no player-history rows for settled GW{gameweek}")

    history_columns = union_columns([row for _, row in raw_rows], f"GW{gameweek}")
    require_keys(
        {column: None for column in history_columns},
        REQUIRED_GW_KEYS,
        f"GW{gameweek} history schema",
    )
    rows: list[dict[str, Any]] = []
    fixture_ids_seen: set[int] = set()
    for player_id, history in sorted(
        raw_rows, key=lambda item: (item[0], int(item[1]["fixture"]))
    ):
        player = players_by_id[player_id]
        fixture_id = int(history["fixture"])
        fixture = fixtures_by_id.get(fixture_id)
        if fixture is None:
            raise UpdateError(f"GW{gameweek} history refers to unknown fixture {fixture_id}")
        if int(fixture["event"]) != gameweek:
            raise UpdateError(
                f"GW{gameweek} history fixture {fixture_id} belongs to event "
                f"{fixture['event']}"
            )
        was_home = history["was_home"] is True or history["was_home"] == "True"
        team_id = int(fixture["team_h"] if was_home else fixture["team_a"])
        element_type = int(player["element_type"])
        if team_id not in team_names:
            raise UpdateError(f"Fixture {fixture_id} refers to unknown team {team_id}")
        if element_type not in position_names:
            raise UpdateError(
                f"Player {player_id} refers to unknown element_type {element_type}"
            )
        row = {
            "name": f"{player['first_name']} {player['second_name']}",
            "position": position_names[element_type],
            "team": team_names[team_id],
            "xP": xpoints.get(player_id, 0.0),
        }
        row.update(history)
        rows.append(row)
        fixture_ids_seen.add(fixture_id)

    expected_fixture_ids = {
        fixture_id
        for fixture_id, fixture in fixtures_by_id.items()
        if fixture.get("event") is not None and int(fixture["event"]) == gameweek
    }
    missing_fixtures = sorted(expected_fixture_ids.difference(fixture_ids_seen))
    if missing_fixtures:
        raise UpdateError(
            f"GW{gameweek} has no player-history rows for fixture(s): "
            + ", ".join(str(item) for item in missing_fixtures)
        )
    return list(GAMEWEEK_PREFIX_COLUMNS) + history_columns, rows


def validate_gameweek_file(path: Path, expected_gameweek: int) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise UpdateError(f"Gameweek file has no header: {path}")
        require_keys(
            {name: None for name in reader.fieldnames},
            GAMEWEEK_PREFIX_COLUMNS + REQUIRED_GW_KEYS,
            str(path),
        )
        count = 0
        for row in reader:
            count += 1
            try:
                actual_gameweek = int(row["round"])
            except (TypeError, ValueError) as exc:
                raise UpdateError(f"Invalid round value in {path}: {row['round']!r}") from exc
            if actual_gameweek != expected_gameweek:
                raise UpdateError(
                    f"{path} contains round {actual_gameweek}, expected {expected_gameweek}"
                )
        if count == 0:
            raise UpdateError(f"Gameweek file contains no rows: {path}")
        return list(reader.fieldnames)


def rebuild_merged_gameweeks(
    output_path: Path, gameweek_paths: Sequence[tuple[int, Path]]
) -> tuple[int, int]:
    if not gameweek_paths:
        raise UpdateError("No gameweek files are available for merged_gw.csv")

    fieldnames: list[str] = []
    for gameweek, path in gameweek_paths:
        columns = validate_gameweek_file(path, gameweek)
        for column in columns:
            if column != "GW" and column not in fieldnames:
                fieldnames.append(column)
    fieldnames.append("GW")

    row_count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for gameweek, path in gameweek_paths:
            with path.open("r", encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                for row in reader:
                    row["GW"] = gameweek
                    writer.writerow({name: row.get(name, "") for name in fieldnames})
                    row_count += 1
    return row_count, len(fieldnames)


def copy_staged_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def stage_update(
    stage_dir: Path,
    season_dir: Path,
    bootstrap: Mapping[str, Any],
    fixtures: Sequence[Mapping[str, Any]],
    summaries: Mapping[int, Mapping[str, Any]],
    latest_gameweek: int,
    force: bool,
    xpoints_snapshot_gameweek: int | None = None,
) -> tuple[list[int], list[int], int, int, dict[int, str]]:
    players = bootstrap["elements"]
    teams = bootstrap["teams"]
    players_by_id = {int(player["id"]): player for player in players}
    team_names = {int(team["id"]): str(team["name"]) for team in teams}
    fixtures_by_id = {int(fixture["id"]): fixture for fixture in fixtures}
    position_names = {
        int(item["id"]): str(item["singular_name_short"])
        for item in bootstrap["element_types"]
    }
    if len(players_by_id) != len(players):
        raise UpdateError("bootstrap-static contains duplicate player IDs")
    if len(team_names) != len(teams):
        raise UpdateError("bootstrap-static contains duplicate team IDs")
    if len(fixtures_by_id) != len(fixtures):
        raise UpdateError("fixtures response contains duplicate fixture IDs")

    write_csv(
        stage_dir / "players_raw.csv",
        union_columns(players, "players_raw.csv"),
        players,
        stringify_values=True,
    )
    write_csv(stage_dir / "teams.csv", list(teams[0]), teams)
    write_csv(stage_dir / "fixtures.csv", list(fixtures[0]), fixtures)
    write_csv(
        stage_dir / "cleaned_players.csv",
        CLEANED_PLAYER_COLUMNS,
        build_cleaned_players(players),
    )
    write_csv(
        stage_dir / "player_idlist.csv",
        ("first_name", "second_name", "id"),
        players,
    )

    existing_directories = existing_player_directories(season_dir / "players")
    destination_directories: dict[int, str] = {}
    for player in players:
        player_id = int(player["id"])
        directory_name = existing_directories.get(player_id) or new_player_directory_name(player)
        destination_directories[player_id] = directory_name
        summary = summaries[player_id]
        player_stage = stage_dir / "players" / directory_name

        past = summary["history_past"]
        if past:
            write_csv(
                player_stage / "history.csv",
                union_columns(past, f"player {player_id} history_past"),
                past,
            )
        completed_history = [
            row for row in summary["history"] if int(row["round"]) <= latest_gameweek
        ]
        if completed_history:
            write_csv(
                player_stage / "gw.csv",
                union_columns(completed_history, f"player {player_id} history"),
                completed_history,
            )

    target_gws = season_dir / "gws"
    stage_gws = stage_dir / "gws"
    staged_xpoints: dict[int, Any] = {}
    if xpoints_snapshot_gameweek is not None:
        staged_xpoints = {
            int(player["id"]): player["ep_this"] for player in players
        }
        write_csv(
            stage_gws / f"xP{xpoints_snapshot_gameweek}.csv",
            ("id", "xP"),
            (
                {"id": player_id, "xP": expected_points}
                for player_id, expected_points in staged_xpoints.items()
            ),
        )

    created: list[int] = []
    skipped: list[int] = []
    selected_paths: list[tuple[int, Path]] = []
    for gameweek in range(1, latest_gameweek + 1):
        target_path = target_gws / f"gw{gameweek}.csv"
        stage_path = stage_gws / f"gw{gameweek}.csv"
        rebuild_for_snapshot = gameweek == xpoints_snapshot_gameweek
        if target_path.is_file() and not force and not rebuild_for_snapshot:
            validate_gameweek_file(target_path, gameweek)
            skipped.append(gameweek)
            selected_paths.append((gameweek, target_path))
            continue
        xpoints = (
            staged_xpoints
            if rebuild_for_snapshot
            else read_xpoints(target_gws, gameweek)
        )
        if not xpoints:
            print(
                f"[INFO] GW{gameweek}: no saved xP snapshot; using 0.0 for the "
                "compatibility column"
            )
        fieldnames, rows = build_gameweek_rows(
            gameweek,
            players_by_id,
            summaries,
            fixtures_by_id,
            team_names,
            position_names,
            xpoints,
        )
        write_csv(stage_path, fieldnames, rows)
        validate_gameweek_file(stage_path, gameweek)
        created.append(gameweek)
        selected_paths.append((gameweek, stage_path))

    merged_rows, merged_columns = rebuild_merged_gameweeks(
        stage_gws / "merged_gw.csv", selected_paths
    )
    return created, skipped, merged_rows, merged_columns, destination_directories


def commit_update(
    stage_dir: Path,
    season_dir: Path,
    destination_directories: Mapping[int, str],
    created_gameweeks: Sequence[int],
    xpoints_snapshot_gameweek: int | None = None,
) -> None:
    for filename in (
        "players_raw.csv",
        "teams.csv",
        "fixtures.csv",
        "cleaned_players.csv",
        "player_idlist.csv",
    ):
        copy_staged_file(stage_dir / filename, season_dir / filename)

    for player_id, directory_name in destination_directories.items():
        source_dir = stage_dir / "players" / directory_name
        if not source_dir.is_dir():
            continue
        for filename in ("history.csv", "gw.csv"):
            source = source_dir / filename
            if source.is_file():
                copy_staged_file(
                    source, season_dir / "players" / directory_name / filename
                )

    for gameweek in created_gameweeks:
        copy_staged_file(
            stage_dir / "gws" / f"gw{gameweek}.csv",
            season_dir / "gws" / f"gw{gameweek}.csv",
        )
    if xpoints_snapshot_gameweek is not None:
        copy_staged_file(
            stage_dir / "gws" / f"xP{xpoints_snapshot_gameweek}.csv",
            season_dir / "gws" / f"xP{xpoints_snapshot_gameweek}.csv",
        )
    copy_staged_file(
        stage_dir / "gws" / "merged_gw.csv",
        season_dir / "gws" / "merged_gw.csv",
    )


def run_update(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent
    season_dir = Path(args.season_dir)
    if not season_dir.is_absolute():
        season_dir = (repo_root / season_dir).resolve()
    else:
        season_dir = season_dir.resolve()

    print(f"[CHECK] Repository: {repo_root}")
    print(f"[CHECK] Season data: {season_dir}")
    validate_repo(repo_root, season_dir)
    gws_dir = season_dir / "gws"
    existing_gameweeks = existing_gameweek_numbers(gws_dir)
    if existing_gameweeks:
        print(
            "[STATE] Existing gameweek files: "
            + ", ".join(f"GW{gameweek}" for gameweek in existing_gameweeks)
        )
    else:
        print(f"[STATE] No gwN.csv files currently exist in {gws_dir}")
    print(
        "[STATE] merged_gw.csv: "
        + ("present" if (gws_dir / "merged_gw.csv").is_file() else "missing")
    )

    api = FplApi(args.api_base_url, args.timeout, args.retries)
    print("[DOWNLOAD] bootstrap-static/")
    bootstrap = api.get_json("bootstrap-static/")
    validate_bootstrap(bootstrap)
    print("[DOWNLOAD] fixtures/")
    fixtures = api.get_json("fixtures/")
    validate_fixtures(fixtures)
    print(
        f"[OK] API schema: {len(bootstrap['elements'])} players, "
        f"{len(bootstrap['teams'])} teams, {len(fixtures)} fixtures"
    )

    now = datetime.now(timezone.utc)
    log_gameweek_statuses(bootstrap["events"], fixtures, now)
    settled, unsafe = settled_gameweeks(bootstrap["events"], fixtures, now=now)
    latest = max(settled, default=0)
    missing_settled = [
        gameweek for gameweek in settled if gameweek not in existing_gameweeks
    ]
    if missing_settled:
        print(
            "[PLAN] Missing settled gameweek files: "
            + ", ".join(f"GW{gameweek}" for gameweek in missing_settled)
        )
    elif settled:
        print("[PLAN] No settled gameweek files are missing")
    if unsafe:
        first_unsafe = min(unsafe)
    if latest == 0:
        print("[SKIP] No fully settled gameweek is available. No files were changed.")
        return 0
    if unsafe:
        first_unsafe = min(unsafe)
        print(
            f"[SAFE MODE] GW{first_unsafe} is active or unsettled. It will not get "
            f"a gw{first_unsafe}.csv, and player gw.csv files will be filtered "
            f"through GW{latest}."
        )
        print(
            "[INFO] Top-level players/teams/fixtures snapshots represent the API "
            "at download time and may contain live aggregate values."
        )
        if args.dry_run:
            print("[DRY RUN] Pending settled files will be validated but not committed.")
    xpoints_snapshot_gameweek = settled_xpoints_snapshot_gameweek(
        bootstrap["events"], settled
    )
    if xpoints_snapshot_gameweek is not None and (
        gws_dir / f"xP{xpoints_snapshot_gameweek}.csv"
    ).is_file():
        xpoints_snapshot_gameweek = None
    if xpoints_snapshot_gameweek is not None:
        print(
            f"[PLAN] Save xP{xpoints_snapshot_gameweek}.csv from the current "
            "settled API ep_this values and rebuild that gameweek with them"
        )
    print(f"[OK] Latest fully settled gameweek: GW{latest}")
    print(
        "[PLAN] Refresh season snapshots and completed player histories; "
        + (
            "rebuild " + ", ".join(f"GW{gameweek}" for gameweek in settled)
            if args.force
            else (
                "create "
                + ", ".join(f"GW{gameweek}" for gameweek in missing_settled)
                if missing_settled
                else (
                    f"rebuild GW{xpoints_snapshot_gameweek} with its new xP snapshot"
                    if xpoints_snapshot_gameweek is not None
                    else "keep all existing gwN.csv files"
                )
            )
        )
        + "; rebuild merged_gw.csv"
    )

    summaries = fetch_summaries(api, bootstrap["elements"], args.workers)
    print("[STAGE] Validating and preparing CSV files")
    temporary_parent = season_dir.parent
    stage_path = Path(
        tempfile.mkdtemp(prefix=f".{args.season}-fpl-update-", dir=temporary_parent)
    )
    try:
        created, skipped, merged_rows, merged_columns, destination_directories = stage_update(
            stage_path,
            season_dir,
            bootstrap,
            fixtures,
            summaries,
            latest,
            args.force,
            xpoints_snapshot_gameweek,
        )
        if args.dry_run:
            print("[DRY RUN] Validation passed; no files were changed.")
        else:
            print("[WRITE] Committing validated files")
            commit_update(
                stage_path,
                season_dir,
                destination_directories,
                created,
                xpoints_snapshot_gameweek,
            )
    finally:
        shutil.rmtree(stage_path, ignore_errors=True)

    created_new = [gameweek for gameweek in created if gameweek not in existing_gameweeks]
    rebuilt = [gameweek for gameweek in created if gameweek in existing_gameweeks]
    if created_new:
        print(
            "[OK] Gameweek files created: "
            + ", ".join(f"GW{gameweek}" for gameweek in created_new)
        )
    if rebuilt:
        print(
            "[OK] Gameweek files rebuilt: "
            + ", ".join(f"GW{gameweek}" for gameweek in rebuilt)
        )
    if xpoints_snapshot_gameweek is not None:
        print(
            f"[OK] xP{xpoints_snapshot_gameweek}.csv: "
            f"{len(bootstrap['elements'])} players"
            + (" (validated only)" if args.dry_run else "")
        )
    if skipped:
        print("[SKIP] Existing settled gameweek files: " + ", ".join(f"GW{gw}" for gw in skipped))
    print(
        f"[OK] merged_gw.csv: {merged_rows} rows, {merged_columns} columns"
        + (" (validated only)" if args.dry_run else "")
    )
    print("[DONE] Update completed successfully" + (" (dry run)" if args.dry_run else ""))
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Update a local vaastav-style FPL season only after the API reports "
            "a fully settled gameweek."
        )
    )
    parser.add_argument(
        "--season",
        default=DEFAULT_SEASON,
        help=f"season label used for temporary files (default: {DEFAULT_SEASON})",
    )
    parser.add_argument(
        "--season-dir",
        default=f"data/{DEFAULT_SEASON}",
        help=(
            "season directory, absolute or relative to this script "
            f"(default: data/{DEFAULT_SEASON})"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        choices=range(1, 17),
        metavar="1..16",
        help="parallel player-history downloads (default: 6)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        choices=range(1, 11),
        metavar="1..10",
        help="maximum request attempts (default: 4)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild existing settled gwN.csv files as well as missing files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download and fully validate without committing files",
    )
    parser.add_argument(
        "--api-base-url",
        default=API_BASE_URL,
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        return run_update(args)
    except KeyboardInterrupt:
        print("\n[ERROR] Update cancelled; staged files were not committed.", file=sys.stderr)
        return 130
    except UpdateError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[ERROR] Unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
