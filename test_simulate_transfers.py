import json
import tempfile
import unittest
from pathlib import Path

import simulate_transfers as simulator


def make_player(
    player_id,
    position,
    team_id,
    projection,
    price=50,
    can_select=True,
):
    return simulator.Player(
        id=player_id,
        name=f"Player {player_id}",
        web_name=f"P{player_id}",
        position=position,
        team_id=team_id,
        team_name=f"Team {team_id}",
        current_price=price,
        projection=projection,
        recent_average=projection,
        ep_next=projection,
        can_select=can_select,
        can_transact=can_select,
        status="a",
    )


def legal_squad():
    positions = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    return [
        make_player(index, position, ((index - 1) % 8) + 1, float(index), 50)
        for index, position in enumerate(positions, start=1)
    ]


class MoneyRuleTests(unittest.TestCase):
    def test_selling_price_keeps_half_profit_rounded_down(self):
        self.assertEqual(simulator.calculate_selling_price(50, 54), 52)
        self.assertEqual(simulator.calculate_selling_price(50, 53), 51)

    def test_selling_price_absorbs_full_loss(self):
        self.assertEqual(simulator.calculate_selling_price(50, 47), 47)

    def test_transfer_hits_respect_free_transfers_and_chips(self):
        self.assertEqual(simulator.transfer_cost(3, 1, "none"), 8)
        self.assertEqual(simulator.transfer_cost(3, 1, "wildcard"), 0)
        self.assertEqual(simulator.transfer_cost(3, 1, "freehit"), 0)

    def test_transfer_chips_are_not_allowed_in_gameweek_one(self):
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_chip_gameweek("freehit", 1)
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_chip_gameweek("wildcard", 1)
        simulator.validate_chip_gameweek("benchboost", 1)


class SquadRuleTests(unittest.TestCase):
    def test_legal_squad_and_lineup(self):
        squad = legal_squad()
        self.assertEqual(simulator.squad_rule_errors(squad), [])
        lineup = simulator.choose_lineup(squad, "none", transfer_cost=0)
        self.assertEqual(len(lineup.starters), 11)
        self.assertEqual(len(lineup.bench), 4)
        counts = {position: 0 for position in simulator.POSITION_ORDER}
        for player in lineup.starters:
            counts[player.position] += 1
        self.assertEqual(counts["GKP"], 1)
        self.assertGreaterEqual(counts["DEF"], 3)
        self.assertGreaterEqual(counts["MID"], 2)
        self.assertGreaterEqual(counts["FWD"], 1)
        self.assertIn(lineup.captain, lineup.starters)
        self.assertIn(lineup.vice_captain, lineup.starters)

    def test_more_than_three_from_one_club_is_illegal(self):
        squad = legal_squad()
        squad[0] = make_player(100, "GKP", 99, 1)
        squad[1] = make_player(101, "GKP", 99, 1)
        squad[2] = make_player(102, "DEF", 99, 1)
        squad[3] = make_player(103, "DEF", 99, 1)
        self.assertIn(
            "contains more than three players from one club",
            simulator.squad_rule_errors(squad),
        )

    def test_team_json_requires_exact_purchase_prices(self):
        players = {player.id: player for player in legal_squad()}
        data = {
            "bank": 0.5,
            "free_transfers": 2,
            "players": [
                {"id": player.id, "purchase_price": 5.0}
                for player in players.values()
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "team.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            team = simulator.load_current_team(path, players)
        self.assertEqual(team.bank, 5)
        self.assertEqual(team.free_transfers, 2)
        self.assertEqual(len(team.players), 15)


class SimulationTests(unittest.TestCase):
    def test_simulator_finds_affordable_legal_upgrade(self):
        squad = legal_squad()
        # Replace the weakest goalkeeper with a strong affordable candidate.
        squad[0] = make_player(1, "GKP", 1, 0.0, 45)
        candidate = make_player(100, "GKP", 9, 20.0, 45)
        players = {player.id: player for player in squad}
        players[candidate.id] = candidate
        owned = tuple(
            simulator.OwnedPlayer(
                player=player,
                purchase_price=player.current_price,
                selling_price=player.current_price,
            )
            for player in squad
        )
        team = simulator.TeamInput(bank=0, free_transfers=1, players=owned)
        baseline, best, evaluated = simulator.simulate(
            team,
            players,
            max_transfers=1,
            runs=0,
            seed=1,
            chip="none",
        )
        self.assertGreater(evaluated, 1)
        self.assertGreater(best.objective, baseline.objective)
        self.assertEqual([item.player.position for item in best.outgoing], ["GKP"])
        self.assertEqual([player.id for player in best.incoming], [100])
        self.assertEqual(best.transfer_cost, 0)
        self.assertGreaterEqual(best.remaining_bank, 0)


if __name__ == "__main__":
    unittest.main()
