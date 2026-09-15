import unittest

import update_player_values as updater


class PlayerValueTests(unittest.TestCase):
    def test_selling_price_uses_half_profit_rounded_down(self):
        self.assertEqual(updater.selling_price(50, 54), 52)
        self.assertEqual(updater.selling_price(50, 53), 51)

    def test_selling_price_absorbs_full_loss(self):
        self.assertEqual(updater.selling_price(50, 47), 47)

    def test_refresh_adds_derived_values_without_changing_purchase_price(self):
        team = {
            "bank": 1.0,
            "players": [
                {"id": player_id, "purchase_price": 5.0}
                for player_id in range(1, 16)
            ],
        }
        players = {
            player_id: {
                "id": player_id,
                "web_name": f"Player {player_id}",
                "team": 1,
                "element_type": 3,
                "now_cost": 54 if player_id == 1 else 47,
            }
            for player_id in range(1, 16)
        }
        rows, changed, purchase, current, selling = updater.refresh_values(
            team, players, {1: "Test FC"}, {3: "MID"}
        )

        self.assertEqual(len(rows), 15)
        self.assertEqual(changed, 15)
        self.assertEqual(team["players"][0]["purchase_price"], 5.0)
        self.assertEqual(team["players"][0]["current_price"], 5.4)
        self.assertEqual(team["players"][0]["selling_price"], 5.2)
        self.assertEqual(team["players"][1]["selling_price"], 4.7)
        self.assertEqual(purchase, 750)
        self.assertEqual(current, 712)
        self.assertEqual(selling, 710)


if __name__ == "__main__":
    unittest.main()
