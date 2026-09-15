#!/usr/bin/env python3
"""Simulate legal FPL transfers from an existing 15-player squad.

The simulator reads the vaastav-style local season CSVs produced by
``update_fpl_data.py``.  It uses only completed gameweek rows for its form
estimate, while current prices and availability come from players_raw.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


POSITION_ORDER = ("GKP", "DEF", "MID", "FWD")
SQUAD_COUNTS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
LINEUP_LIMITS = {
    "GKP": (1, 1),
    "DEF": (3, 5),
    "MID": (2, 5),
    "FWD": (1, 3),
}
POSITION_BY_ELEMENT_TYPE = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_SIZE = 15
STARTING_SIZE = 11
CLUB_LIMIT = 3
INITIAL_BUDGET = 1000  # £100.0m in API tenths
MAX_FREE_TRANSFERS = 5
TRANSFER_CAP = 20
TRANSFER_HIT = 4
TRANSFER_CHIPS = {"wildcard", "freehit"}
TEAM_CHIPS = {"benchboost", "triplecaptain"}
CHIP_CHOICES = ("none", "wildcard", "freehit", "benchboost", "triplecaptain")


class SimulationError(RuntimeError):
    """A clear, user-actionable simulator failure."""


@dataclass(frozen=True)
class Player:
    id: int
    name: str
    web_name: str
    position: str
    team_id: int
    team_name: str
    current_price: int
    projection: float
    recent_average: float | None
    ep_next: float | None
    can_select: bool
    can_transact: bool
    status: str


@dataclass(frozen=True)
class OwnedPlayer:
    player: Player
    purchase_price: int
    selling_price: int


@dataclass(frozen=True)
class TeamInput:
    bank: int
    free_transfers: int
    players: tuple[OwnedPlayer, ...]


@dataclass(frozen=True)
class Lineup:
    starters: tuple[Player, ...]
    bench: tuple[Player, ...]
    captain: Player
    vice_captain: Player
    formation: str
    projected_points: float


@dataclass(frozen=True)
class Plan:
    squad: tuple[Player, ...]
    outgoing: tuple[OwnedPlayer, ...]
    incoming: tuple[Player, ...]
    remaining_bank: int
    transfer_cost: int
    lineup: Lineup
    objective: float


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def parse_optional_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def money_to_tenths(value: Any, context: str) -> int:
    """Convert a user-facing £m value such as 5.5 to API tenths."""
    try:
        scaled = Decimal(str(value)) * Decimal("10")
    except (InvalidOperation, ValueError) as exc:
        raise SimulationError(f"{context} must be a number in £m, for example 5.5") from exc
    if scaled != scaled.to_integral_value():
        raise SimulationError(f"{context} must use £0.1m increments, got {value!r}")
    result = int(scaled)
    if result < 0:
        raise SimulationError(f"{context} cannot be negative")
    return result


def format_money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def calculate_selling_price(purchase_price: int, current_price: int) -> int:
    """Apply FPL's 50%-of-profit sell-on rule, rounded down to £0.1m."""
    if current_price <= purchase_price:
        return current_price
    return purchase_price + (current_price - purchase_price) // 2


def require_csv_columns(reader: csv.DictReader, required: Iterable[str], path: Path) -> None:
    fieldnames = set(reader.fieldnames or [])
    missing = sorted(set(required).difference(fieldnames))
    if missing:
        raise SimulationError(f"{path} is missing required column(s): {', '.join(missing)}")


def load_team_names(path: Path) -> dict[int, str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_csv_columns(reader, ("id", "name"), path)
        result = {int(row["id"]): row["name"] for row in reader}
    if not result:
        raise SimulationError(f"No teams found in {path}")
    return result


def load_completed_history(
    path: Path, target_gameweek: int
) -> tuple[dict[int, dict[int, float]], int]:
    weekly_points: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    latest_gameweek = 0
    if not path.is_file():
        raise SimulationError(
            f"Completed gameweek data is missing: {path}. Run update_fpl_data.py first."
        )
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_csv_columns(reader, ("element", "total_points", "GW"), path)
        for row in reader:
            gameweek = int(row["GW"])
            latest_gameweek = max(latest_gameweek, gameweek)
            if gameweek >= target_gameweek:
                continue
            player_id = int(row["element"])
            weekly_points[player_id][gameweek] += float(row["total_points"])
    return {key: dict(value) for key, value in weekly_points.items()}, latest_gameweek


def player_projection(
    player_id: int,
    weekly_points: Mapping[int, Mapping[int, float]],
    ep_next: float | None,
    window: int,
    forecast_weight: float,
) -> tuple[float, float | None]:
    history = weekly_points.get(player_id, {})
    recent_values = [history[gameweek] for gameweek in sorted(history)[-window:]]
    recent_average = mean(recent_values) if recent_values else None
    if recent_average is None and ep_next is None:
        return 0.0, None
    if recent_average is None:
        return float(ep_next), None
    if ep_next is None:
        return recent_average, recent_average
    projection = (1.0 - forecast_weight) * recent_average + forecast_weight * ep_next
    return projection, recent_average


def load_players(
    players_path: Path,
    teams_path: Path,
    merged_path: Path,
    target_gameweek: int,
    window: int,
    forecast_weight: float,
) -> tuple[dict[int, Player], int]:
    team_names = load_team_names(teams_path)
    weekly_points, latest_gameweek = load_completed_history(merged_path, target_gameweek)
    required = (
        "id",
        "first_name",
        "second_name",
        "web_name",
        "element_type",
        "team",
        "now_cost",
        "ep_next",
        "can_select",
        "can_transact",
        "status",
    )
    players: dict[int, Player] = {}
    with players_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_csv_columns(reader, required, players_path)
        for row in reader:
            player_id = int(row["id"])
            element_type = int(row["element_type"])
            if element_type not in POSITION_BY_ELEMENT_TYPE:
                raise SimulationError(
                    f"Unknown element_type {element_type} for player {player_id}; rules changed"
                )
            team_id = int(row["team"])
            if team_id not in team_names:
                raise SimulationError(f"Unknown team {team_id} for player {player_id}")
            ep_next = parse_optional_float(row["ep_next"])
            projection, recent_average = player_projection(
                player_id,
                weekly_points,
                ep_next,
                window,
                forecast_weight,
            )
            players[player_id] = Player(
                id=player_id,
                name=f"{row['first_name']} {row['second_name']}",
                web_name=row["web_name"],
                position=POSITION_BY_ELEMENT_TYPE[element_type],
                team_id=team_id,
                team_name=team_names[team_id],
                current_price=int(row["now_cost"]),
                projection=projection,
                recent_average=recent_average,
                ep_next=ep_next,
                can_select=parse_bool(row["can_select"]),
                can_transact=parse_bool(row["can_transact"]),
                status=row["status"],
            )
    if not players:
        raise SimulationError(f"No players found in {players_path}")
    return players, latest_gameweek


def load_current_team(path: Path, players: Mapping[int, Player]) -> TeamInput:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise SimulationError(
            f"Team file not found: {path}. Copy current_team.example.json and fill all 15 players."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SimulationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SimulationError(f"{path} must contain one JSON object")
    for field in ("bank", "free_transfers", "players"):
        if field not in data:
            raise SimulationError(f"{path} is missing required field {field!r}")
    bank = money_to_tenths(data["bank"], "bank")
    try:
        free_transfers = int(data["free_transfers"])
    except (TypeError, ValueError) as exc:
        raise SimulationError("free_transfers must be an integer from 0 to 5") from exc
    if not 0 <= free_transfers <= MAX_FREE_TRANSFERS:
        raise SimulationError("free_transfers must be from 0 to 5")
    entries = data["players"]
    if not isinstance(entries, list) or len(entries) != SQUAD_SIZE:
        actual = len(entries) if isinstance(entries, list) else "not a list"
        raise SimulationError(f"Team file must contain exactly 15 players; found {actual}")

    owned: list[OwnedPlayer] = []
    seen: set[int] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SimulationError(f"players[{index}] must be a JSON object")
        if "id" not in entry or "purchase_price" not in entry:
            raise SimulationError(
                f"players[{index}] requires id and purchase_price fields"
            )
        try:
            player_id = int(entry["id"])
        except (TypeError, ValueError) as exc:
            raise SimulationError(f"players[{index}].id must be an integer") from exc
        if player_id in seen:
            raise SimulationError(f"Player ID {player_id} occurs more than once in {path}")
        if player_id not in players:
            raise SimulationError(f"Player ID {player_id} is not present in players_raw.csv")
        seen.add(player_id)
        purchase_price = money_to_tenths(
            entry["purchase_price"], f"players[{index}].purchase_price"
        )
        player = players[player_id]
        owned.append(
            OwnedPlayer(
                player=player,
                purchase_price=purchase_price,
                selling_price=calculate_selling_price(
                    purchase_price, player.current_price
                ),
            )
        )
    errors = squad_rule_errors([item.player for item in owned])
    if errors:
        raise SimulationError("Current team is not a legal FPL squad: " + "; ".join(errors))
    return TeamInput(bank=bank, free_transfers=free_transfers, players=tuple(owned))


def squad_rule_errors(squad: Sequence[Player]) -> list[str]:
    errors: list[str] = []
    ids = [player.id for player in squad]
    if len(squad) != SQUAD_SIZE:
        errors.append(f"requires {SQUAD_SIZE} players, found {len(squad)}")
    if len(set(ids)) != len(ids):
        errors.append("contains duplicate players")
    position_counts = Counter(player.position for player in squad)
    for position, required in SQUAD_COUNTS.items():
        if position_counts[position] != required:
            errors.append(
                f"requires {required} {position}, found {position_counts[position]}"
            )
    team_counts = Counter(player.team_id for player in squad)
    over_limit = [team_id for team_id, count in team_counts.items() if count > CLUB_LIMIT]
    if over_limit:
        errors.append("contains more than three players from one club")
    return errors


def choose_lineup(squad: Sequence[Player], chip: str, transfer_cost: int) -> Lineup:
    by_position: dict[str, list[Player]] = {
        position: sorted(
            (player for player in squad if player.position == position),
            key=lambda player: (-player.projection, player.id),
        )
        for position in POSITION_ORDER
    }
    best_starters: tuple[Player, ...] | None = None
    best_score = -math.inf
    best_formation = ""
    for defenders in range(LINEUP_LIMITS["DEF"][0], LINEUP_LIMITS["DEF"][1] + 1):
        for midfielders in range(LINEUP_LIMITS["MID"][0], LINEUP_LIMITS["MID"][1] + 1):
            forwards = 10 - defenders - midfielders
            if not LINEUP_LIMITS["FWD"][0] <= forwards <= LINEUP_LIMITS["FWD"][1]:
                continue
            starters = tuple(
                by_position["GKP"][:1]
                + by_position["DEF"][:defenders]
                + by_position["MID"][:midfielders]
                + by_position["FWD"][:forwards]
            )
            if len(starters) != STARTING_SIZE:
                continue
            score = sum(player.projection for player in starters)
            if score > best_score:
                best_score = score
                best_starters = starters
                best_formation = f"{defenders}-{midfielders}-{forwards}"
    if best_starters is None:
        raise SimulationError("Could not construct a legal starting XI")
    ranked_starters = sorted(
        best_starters, key=lambda player: (-player.projection, player.id)
    )
    captain = ranked_starters[0]
    vice_captain = ranked_starters[1]
    starter_ids = {player.id for player in best_starters}
    bench = tuple(
        sorted(
            (player for player in squad if player.id not in starter_ids),
            key=lambda player: (
                0 if player.position != "GKP" else 1,
                -player.projection,
                player.id,
            ),
        )
    )
    if chip == "benchboost":
        projected = sum(player.projection for player in squad) + captain.projection
    elif chip == "triplecaptain":
        projected = best_score + 2 * captain.projection
    else:
        projected = best_score + captain.projection
    projected -= transfer_cost
    return Lineup(
        starters=best_starters,
        bench=bench,
        captain=captain,
        vice_captain=vice_captain,
        formation=best_formation,
        projected_points=projected,
    )


def transfer_cost(transfer_count: int, free_transfers: int, chip: str) -> int:
    if chip in TRANSFER_CHIPS:
        return 0
    return max(0, transfer_count - free_transfers) * TRANSFER_HIT


def validate_chip_gameweek(chip: str, gameweek: int) -> None:
    if not 1 <= gameweek <= 38:
        raise SimulationError("gameweek must be from 1 to 38")
    if chip in TRANSFER_CHIPS and gameweek == 1:
        raise SimulationError(f"The {chip} chip cannot be played in GW1")


def make_plan(
    squad: Sequence[Player],
    outgoing: Sequence[OwnedPlayer],
    incoming: Sequence[Player],
    remaining_bank: int,
    free_transfers: int,
    chip: str,
) -> Plan:
    errors = squad_rule_errors(squad)
    if errors:
        raise SimulationError("Generated illegal squad: " + "; ".join(errors))
    cost = transfer_cost(len(outgoing), free_transfers, chip)
    lineup = choose_lineup(squad, chip, cost)
    return Plan(
        squad=tuple(sorted(squad, key=lambda player: player.id)),
        outgoing=tuple(outgoing),
        incoming=tuple(incoming),
        remaining_bank=remaining_bank,
        transfer_cost=cost,
        lineup=lineup,
        objective=lineup.projected_points,
    )


def plan_is_better(candidate: Plan, incumbent: Plan) -> bool:
    candidate_key = (
        round(candidate.objective, 9),
        -candidate.transfer_cost,
        -len(candidate.outgoing),
        candidate.remaining_bank,
    )
    incumbent_key = (
        round(incumbent.objective, 9),
        -incumbent.transfer_cost,
        -len(incumbent.outgoing),
        incumbent.remaining_bank,
    )
    return candidate_key > incumbent_key


def exact_single_transfer_plans(
    team: TeamInput,
    candidates: Sequence[Player],
    chip: str,
) -> Iterable[Plan]:
    current_players = [item.player for item in team.players]
    current_ids = {player.id for player in current_players}
    for outgoing in team.players:
        kept = [player for player in current_players if player.id != outgoing.player.id]
        team_counts = Counter(player.team_id for player in kept)
        cash = team.bank + outgoing.selling_price
        for incoming in candidates:
            if incoming.id in current_ids or incoming.position != outgoing.player.position:
                continue
            if incoming.current_price > cash or team_counts[incoming.team_id] >= CLUB_LIMIT:
                continue
            squad = kept + [incoming]
            yield make_plan(
                squad,
                [outgoing],
                [incoming],
                cash - incoming.current_price,
                team.free_transfers,
                chip,
            )


def random_transfer_plan(
    team: TeamInput,
    candidates_by_position: Mapping[str, Sequence[Player]],
    transfer_count_value: int,
    rng: random.Random,
    chip: str,
) -> Plan | None:
    outgoing = rng.sample(list(team.players), transfer_count_value)
    outgoing_ids = {item.player.id for item in outgoing}
    original_ids = {item.player.id for item in team.players}
    kept = [item.player for item in team.players if item.player.id not in outgoing_ids]
    cash = team.bank + sum(item.selling_price for item in outgoing)
    team_counts = Counter(player.team_id for player in kept)
    selected_ids = {player.id for player in kept}
    incoming: list[Player] = []

    needed_positions = [item.player.position for item in outgoing]
    rng.shuffle(needed_positions)
    for position in needed_positions:
        eligible = [
            player
            for player in candidates_by_position[position]
            if player.id not in original_ids
            and player.id not in selected_ids
            and player.current_price <= cash
            and team_counts[player.team_id] < CLUB_LIMIT
        ]
        if not eligible:
            return None
        eligible.sort(key=lambda player: (-player.projection, player.current_price, player.id))
        shortlist = eligible[: min(40, len(eligible))]
        # Usually sample strong options, while retaining exploration.
        if rng.random() < 0.25:
            chosen = shortlist[0]
        else:
            weights = [max(0.1, player.projection + 1.0) for player in shortlist]
            chosen = rng.choices(shortlist, weights=weights, k=1)[0]
        incoming.append(chosen)
        selected_ids.add(chosen.id)
        team_counts[chosen.team_id] += 1
        cash -= chosen.current_price

    squad = kept + incoming
    if squad_rule_errors(squad):
        return None
    return make_plan(
        squad,
        outgoing,
        incoming,
        cash,
        team.free_transfers,
        chip,
    )


def simulate(
    team: TeamInput,
    all_players: Mapping[int, Player],
    max_transfers: int,
    runs: int,
    seed: int,
    chip: str,
) -> tuple[Plan, Plan, int]:
    current_squad = [item.player for item in team.players]
    baseline = make_plan(
        current_squad, [], [], team.bank, team.free_transfers, chip
    )
    best = baseline
    candidates = [
        player
        for player in all_players.values()
        if player.can_select and player.can_transact
    ]
    candidates_by_position = {
        position: [player for player in candidates if player.position == position]
        for position in POSITION_ORDER
    }
    evaluated = 1
    if max_transfers >= 1:
        for plan in exact_single_transfer_plans(team, candidates, chip):
            evaluated += 1
            if plan_is_better(plan, best):
                best = plan

    if max_transfers >= 2 and runs > 0:
        rng = random.Random(seed)
        max_random_transfers = min(max_transfers, SQUAD_SIZE)
        for _ in range(runs):
            count = rng.randint(2, max_random_transfers)
            plan = random_transfer_plan(
                team, candidates_by_position, count, rng, chip
            )
            if plan is None:
                continue
            evaluated += 1
            if plan_is_better(plan, best):
                best = plan
    return baseline, best, evaluated


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    text_rows = [[str(value) for value in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in text_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    pattern = "  ".join(f"{{:<{width}}}" for width in widths)
    print(pattern.format(*headers))
    print(pattern.format(*("-" * width for width in widths)))
    for row in text_rows:
        print(pattern.format(*row))


def player_row(player: Player, marker: str = "") -> tuple[str, ...]:
    return (
        marker,
        str(player.id),
        player.position,
        player.web_name,
        player.team_name,
        format_money(player.current_price),
        f"{player.projection:.2f}",
    )


def print_plan(team: TeamInput, baseline: Plan, best: Plan, evaluated: int, chip: str) -> None:
    print(f"[CURRENT] Bank: {format_money(team.bank)}; free transfers: {team.free_transfers}")
    print(
        f"[SIMULATION] Evaluated {evaluated:,} legal squads; chip: {chip}; "
        f"baseline={baseline.objective:.2f}, best={best.objective:.2f}"
    )
    print(
        f"[RESULT] {len(best.outgoing)} transfer(s), points cost {best.transfer_cost}, "
        f"remaining bank {format_money(best.remaining_bank)}, "
        f"projected gain {best.objective - baseline.objective:+.2f}"
    )
    if best.outgoing:
        rows: list[tuple[str, ...]] = []
        outgoing_by_position: dict[str, list[OwnedPlayer]] = defaultdict(list)
        incoming_by_position: dict[str, list[Player]] = defaultdict(list)
        for item in best.outgoing:
            outgoing_by_position[item.player.position].append(item)
        for player in best.incoming:
            incoming_by_position[player.position].append(player)
        for position in POSITION_ORDER:
            outs = sorted(outgoing_by_position[position], key=lambda item: item.player.id)
            ins = sorted(
                incoming_by_position[position],
                key=lambda player: (-player.projection, player.id),
            )
            for outgoing, incoming in zip(outs, ins):
                rows.append(
                    (
                        position,
                        outgoing.player.web_name,
                        format_money(outgoing.selling_price),
                        incoming.web_name,
                        format_money(incoming.current_price),
                        f"{outgoing.player.projection:.2f}",
                        f"{incoming.projection:.2f}",
                    )
                )
        print("\nTransfers")
        print_table(
            ("Pos", "OUT", "Sell", "IN", "Buy", "Out xPts", "In xPts"), rows
        )
    else:
        print("[RESULT] Keeping the current squad scores best under these assumptions.")

    print(f"\nStarting XI ({best.lineup.formation})")
    starter_rows = []
    for position in POSITION_ORDER:
        for player in sorted(
            (item for item in best.lineup.starters if item.position == position),
            key=lambda item: (-item.projection, item.id),
        ):
            marker = "C" if player.id == best.lineup.captain.id else (
                "VC" if player.id == best.lineup.vice_captain.id else ""
            )
            starter_rows.append(player_row(player, marker))
    print_table(("Role", "ID", "Pos", "Player", "Club", "Price", "xPts"), starter_rows)

    print("\nBench order")
    print_table(
        ("Role", "ID", "Pos", "Player", "Club", "Price", "xPts"),
        [player_row(player, f"B{index}") for index, player in enumerate(best.lineup.bench, 1)],
    )


def find_players(players_path: Path, teams_path: Path, query: str) -> int:
    team_names = load_team_names(teams_path)
    query_lower = query.casefold()
    matches: list[tuple[str, ...]] = []
    with players_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_csv_columns(
            reader,
            ("id", "first_name", "second_name", "web_name", "element_type", "team", "now_cost"),
            players_path,
        )
        for row in reader:
            full_name = f"{row['first_name']} {row['second_name']}"
            if query_lower not in full_name.casefold() and query_lower not in row["web_name"].casefold():
                continue
            position = POSITION_BY_ELEMENT_TYPE.get(int(row["element_type"]), "?")
            matches.append(
                (
                    row["id"],
                    position,
                    row["web_name"],
                    full_name,
                    team_names.get(int(row["team"]), "?"),
                    format_money(int(row["now_cost"])),
                )
            )
    if not matches:
        print(f"No players matched {query!r}")
        return 1
    print_table(("ID", "Pos", "Web name", "Full name", "Club", "Price"), matches)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate legal FPL transfers from your current 15-player team."
    )
    parser.add_argument("--team-file", default="current_team.json")
    parser.add_argument("--season-dir", default="data/2026-27")
    parser.add_argument("--gameweek", type=int, help="target gameweek for the recommendation")
    parser.add_argument(
        "--max-transfers",
        type=int,
        help="maximum permanent transfers; defaults to the free transfers in the team file",
    )
    parser.add_argument("--runs", type=int, default=25000, help="random multi-transfer simulations")
    parser.add_argument("--seed", type=int, default=202627)
    parser.add_argument("--window", type=int, default=6, help="completed gameweeks in form average")
    parser.add_argument(
        "--forecast-weight",
        type=float,
        default=0.35,
        help="weight given to current ep_next versus recent points (default: 0.35)",
    )
    parser.add_argument("--chip", choices=CHIP_CHOICES, default="none")
    parser.add_argument(
        "--find",
        metavar="NAME",
        help="find player IDs by name, then exit (team file and gameweek not required)",
    )
    return parser


def main() -> int:
    # Windows terminals commonly default to a legacy code page that cannot
    # encode several player-name characters (for example, ć).  UTF-8 keeps
    # console reporting from aborting halfway through an otherwise valid run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parent
    season_dir = Path(args.season_dir)
    if not season_dir.is_absolute():
        season_dir = (repo_root / season_dir).resolve()
    players_path = season_dir / "players_raw.csv"
    teams_path = season_dir / "teams.csv"
    if args.find:
        try:
            return find_players(players_path, teams_path, args.find)
        except SimulationError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
    if args.gameweek is None or args.gameweek < 1:
        parser.error("--gameweek is required and must be at least 1")
    if args.runs < 0:
        parser.error("--runs cannot be negative")
    if args.window < 1:
        parser.error("--window must be at least 1")
    if not 0 <= args.forecast_weight <= 1:
        parser.error("--forecast-weight must be between 0 and 1")

    try:
        print(f"[CHECK] Season data: {season_dir}")
        players, latest_completed = load_players(
            players_path,
            teams_path,
            season_dir / "gws" / "merged_gw.csv",
            args.gameweek,
            args.window,
            args.forecast_weight,
        )
        if latest_completed >= args.gameweek:
            print(
                f"[INFO] Data contains results through GW{latest_completed}; "
                f"only rows before target GW{args.gameweek} are used."
            )
        else:
            print(
                f"[DATA] Completed results through GW{latest_completed}; "
                f"target recommendation GW{args.gameweek}."
            )
        team_path = Path(args.team_file)
        if not team_path.is_absolute():
            team_path = (repo_root / team_path).resolve()
        team = load_current_team(team_path, players)
        validate_chip_gameweek(args.chip, args.gameweek)
        max_transfers = (
            args.max_transfers
            if args.max_transfers is not None
            else (SQUAD_SIZE if args.chip in TRANSFER_CHIPS else team.free_transfers)
        )
        if not 0 <= max_transfers <= min(TRANSFER_CAP, SQUAD_SIZE):
            raise SimulationError(
                f"max_transfers must be from 0 to {min(TRANSFER_CAP, SQUAD_SIZE)}"
            )
        if args.chip == "none" and max_transfers > TRANSFER_CAP:
            raise SimulationError(f"Ordinary transfers are capped at {TRANSFER_CAP}")
        print(
            f"[RULES] 15-player squad; 2 GKP/5 DEF/5 MID/3 FWD; max 3 per club; "
            f"max transfers {max_transfers}."
        )
        if args.chip != "none":
            print(
                f"[CHIP] Scoring {args.chip} for GW{args.gameweek}; the simulator "
                "assumes that chip remains available in your FPL account."
            )
        baseline, best, evaluated = simulate(
            team,
            players,
            max_transfers,
            args.runs,
            args.seed,
            args.chip,
        )
        print_plan(team, baseline, best, evaluated, args.chip)
        return 0
    except (OSError, SimulationError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
