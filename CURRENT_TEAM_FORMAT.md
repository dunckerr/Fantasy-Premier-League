# Current team input

Copy `current_team.example.json` to `current_team.json`, then replace the 15
example player IDs and prices with your actual FPL squad.

```json
{
  "bank": 0.5,
  "free_transfers": 2,
  "players": [
    {"id": 1, "purchase_price": 5.5}
  ]
}
```

- `bank` is the money shown in your FPL bank, in millions.
- `free_transfers` is the number currently remaining on your Transfers page
  (0 to 5).
- `players` must contain exactly 15 unique entries.
- `id` is the FPL element/player ID from `data/2026-27/players_raw.csv`.
- `purchase_price` is the PP value shown in the List view of the FPL Transfers
  page, in millions. It is required so the simulator can calculate the correct
  selling price after FPL's 50% sell-on fee.

Find an ID by player name:

```bash
python simulate_transfers.py --find Salah
```

Run a simulation for GW3, considering at most two transfers:

```bash
python simulate_transfers.py --team-file current_team.json --gameweek 3 --max-transfers 2 --runs 50000
```

The output recommends transfers, a legal starting formation, captain,
vice-captain and bench order. Extra transfers beyond `free_transfers` are
charged four projected points each.

The script does not log in to, or modify, your official FPL account. Apply any
recommended transfers and lineup changes yourself. When using `--chip`, it
assumes that the selected chip is still available in your account.
