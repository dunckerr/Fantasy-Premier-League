#!/usr/bin/env python3
"""Refresh current and selling prices in a local FPL team JSON file."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
USER_AGENT = "vaastav-local-player-value-updater/1.0"
SQUAD_SIZE = 15


class ValueUpdateError(RuntimeError):
    """A clear, user-actionable valuation update failure."""


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def money_to_tenths(value: Any, context: str) -> int:
    try:
        amount = Decimal(str(value)) * 10
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueUpdateError(f"{context} must be a number in millions") from exc
    if amount != amount.to_integral_value():
        raise ValueUpdateError(f"{context} must use £0.1m increments")
    tenths = int(amount)
    if tenths < 0:
        raise ValueUpdateError(f"{context} cannot be negative")
    return tenths


def format_money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def selling_price(purchase_price: int, current_price: int) -> int:
    """Apply FPL's half-profit rule, rounded down to the nearest £0.1m."""
    if current_price <= purchase_price:
        return current_price
    return purchase_price + (current_price - purchase_price) // 2


def download_bootstrap(url: str, timeout: float, retries: int) -> Mapping[str, Any]:
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise ValueUpdateError(
                        f"GET {url} returned HTTP {getattr(response, 'status', '?')}"
                    )
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise ValueUpdateError("bootstrap-static response is not an object")
            return payload
        except HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    raise ValueUpdateError(
        f"Could not download {url} after {retries} attempt(s): {last_error}"
    )


def validate_bootstrap(
    bootstrap: Mapping[str, Any],
) -> tuple[dict[int, Mapping[str, Any]], dict[int, str], dict[int, str]]:
    elements = bootstrap.get("elements")
    teams = bootstrap.get("teams")
    element_types = bootstrap.get("element_types")
    if not isinstance(elements, list) or not elements:
        raise ValueUpdateError("bootstrap-static has no player list")
    if not isinstance(teams, list) or not teams:
        raise ValueUpdateError("bootstrap-static has no team list")
    if not isinstance(element_types, list) or not element_types:
        raise ValueUpdateError("bootstrap-static has no position list")

    players: dict[int, Mapping[str, Any]] = {}
    for player in elements:
        if not isinstance(player, dict):
            raise ValueUpdateError("bootstrap-static contains a non-object player")
        missing = {"id", "web_name", "team", "element_type", "now_cost"} - set(player)
        if missing:
            raise ValueUpdateError(
                "bootstrap player is missing field(s): " + ", ".join(sorted(missing))
            )
        player_id = int(player["id"])
        if player_id in players:
            raise ValueUpdateError(f"bootstrap-static has duplicate player ID {player_id}")
        players[player_id] = player

    team_names = {int(team["id"]): str(team["name"]) for team in teams}
    position_names = {
        int(item["id"]): str(item["singular_name_short"])
        for item in element_types
    }
    return players, team_names, position_names


def load_team_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            team = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueUpdateError(f"Team file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueUpdateError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(team, dict):
        raise ValueUpdateError(f"Team file must contain one JSON object: {path}")
    entries = team.get("players")
    if not isinstance(entries, list) or len(entries) != SQUAD_SIZE:
        count = len(entries) if isinstance(entries, list) else 0
        raise ValueUpdateError(
            f"Team file must contain exactly {SQUAD_SIZE} players; found {count}"
        )
    money_to_tenths(team.get("bank"), "bank")
    return team


def refresh_values(
    team: dict[str, Any],
    players: Mapping[int, Mapping[str, Any]],
    team_names: Mapping[int, str],
    position_names: Mapping[int, str],
) -> tuple[list[list[str]], int, int, int, int]:
    entries = team["players"]
    seen: set[int] = set()
    display_rows: list[list[str]] = []
    changed = 0
    purchase_total = 0
    current_total = 0
    selling_total = 0

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueUpdateError(f"players[{index}] must be a JSON object")
        if "id" not in entry or "purchase_price" not in entry:
            raise ValueUpdateError(
                f"players[{index}] requires id and purchase_price fields"
            )
        try:
            player_id = int(entry["id"])
        except (TypeError, ValueError) as exc:
            raise ValueUpdateError(f"players[{index}].id must be an integer") from exc
        if player_id in seen:
            raise ValueUpdateError(f"Duplicate player ID in team file: {player_id}")
        seen.add(player_id)
        player = players.get(player_id)
        if player is None:
            raise ValueUpdateError(
                f"Player ID {player_id} is not present in the current FPL API"
            )

        purchase = money_to_tenths(
            entry["purchase_price"], f"players[{index}].purchase_price"
        )
        current = int(player["now_cost"])
        sell = selling_price(purchase, current)
        old_current = entry.get("current_price")
        old_sell = entry.get("selling_price")
        new_current = current / 10
        new_sell = sell / 10
        if old_current != new_current or old_sell != new_sell:
            changed += 1
        entry["current_price"] = new_current
        entry["selling_price"] = new_sell

        purchase_total += purchase
        current_total += current
        selling_total += sell
        delta = current - purchase
        display_rows.append(
            [
                str(player_id),
                str(player["web_name"]),
                position_names.get(int(player["element_type"]), "?"),
                team_names.get(int(player["team"]), "?"),
                format_money(purchase),
                format_money(current),
                format_money(sell),
                f"{delta / 10:+.1f}",
            ]
        )

    return display_rows, changed, purchase_total, current_total, selling_total


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    template = "  ".join(f"{{:<{width}}}" for width in widths)
    print(template.format(*headers))
    print(template.format(*(separator * width for separator, width in zip("-" * len(widths), widths))))
    for row in rows:
        print(template.format(*row))


def write_team_file(path: Path, team: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(team, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh derived current and selling prices in a current_team JSON file. "
            "Stored purchase prices are never changed."
        )
    )
    parser.add_argument("team_file", help="current team JSON file to update")
    parser.add_argument(
        "--dry-run", action="store_true", help="check and display values without writing"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4, choices=range(1, 11))
    parser.add_argument("--api-url", default=API_URL, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    path = Path(args.team_file).resolve()
    try:
        print(f"[CHECK] Team file: {path}")
        team = load_team_file(path)
        print("[DOWNLOAD] bootstrap-static/")
        bootstrap = download_bootstrap(args.api_url, args.timeout, args.retries)
        players, team_names, position_names = validate_bootstrap(bootstrap)
        print(f"[OK] API schema: {len(players)} players, {len(team_names)} teams")
        rows, changed, purchase_total, current_total, selling_total = refresh_values(
            team, players, team_names, position_names
        )
        print()
        print_table(
            ("ID", "Player", "Pos", "Club", "Bought", "Current", "Sell", "Δ"),
            rows,
        )
        bank = money_to_tenths(team["bank"], "bank")
        print()
        print(
            f"[TOTAL] Bought {format_money(purchase_total)}; "
            f"current market {format_money(current_total)}; "
            f"sale value {format_money(selling_total)}; bank {format_money(bank)}; "
            f"sale value + bank {format_money(selling_total + bank)}"
        )
        print(
            "[NOTE] purchase_price was not changed: the public FPL API does not "
            "expose the price your team paid."
        )
        if args.dry_run:
            print(f"[DRY RUN] {changed} player valuation(s) would be refreshed.")
        elif changed:
            write_team_file(path, team)
            print(
                f"[WRITE] Updated current_price and selling_price for "
                f"{changed} player(s) in {path.name}"
            )
        else:
            print("[OK] Stored current and selling prices are already up to date.")
        return 0
    except (OSError, ValueUpdateError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
