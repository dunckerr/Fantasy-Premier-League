"""Legacy full-squad sampler from the original ArcticDB walkthrough.

Use simulate_transfers.py for rule-aware changes to an existing FPL team.
"""

import os
import pandas as pd
import unicodedata
import arcticdb as adb
import random
from collections import Counter
from tabulate import tabulate

def normalize_name(name):
    """Normalize the name to remove non-ASCII characters."""
    return unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('ASCII')
 
def ingest_player_data(players_dir, lib):
    """Ingest player game week data into ArcticDB."""
    ingested_symbols = set()
    for player_folder in os.scandir(players_dir):
        if player_folder.is_dir():
            player_name, player_id = player_folder.name.rsplit('_', 1)
            csv_file_path = os.path.join(player_folder, 'gw.csv')
            if not os.path.exists(csv_file_path):
                # A new season has history.csv files before it has any gw.csv files.
                continue
            symbol_name = f"{normalize_name(player_name)}_{player_id}"
            lib.write(symbol_name, pd.read_csv(csv_file_path))
            ingested_symbols.add(symbol_name)
    return ingested_symbols


def build_preseason_player_pool(raw_data_path):
    """Build a GW1 player pool from FPL's current prices and next-GW forecast."""
    raw = pd.read_csv(raw_data_path)
    required_columns = {
        'element_type', 'team', 'second_name', 'first_name', 'id',
        'now_cost', 'ep_next', 'event_points', 'can_select', 'status'
    }
    missing_columns = required_columns.difference(raw.columns)
    if missing_columns:
        raise ValueError(
            "players_raw.csv is missing preseason columns: "
            + ", ".join(sorted(missing_columns))
        )

    # Do not offer unavailable or non-selectable players to the sampler.
    raw = raw[(raw['can_select'] == True) & (raw['status'] == 'a')].copy()
    raw['average_total_points'] = pd.to_numeric(raw['ep_next'], errors='coerce')
    raw = raw.dropna(subset=['average_total_points'])
    raw['value'] = raw['now_cost']
    raw['total_points'] = raw['event_points']
    raw['Game_Week'] = 0
    raw['element_type'] = raw['element_type'].map(
        {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
    )
    return raw[[
        'element_type', 'team', 'second_name', 'first_name', 'id',
        'total_points', 'value', 'Game_Week', 'average_total_points'
    ]]

def merge_player_data(lib, ingested_symbols):
    """Read raw player stats and merge with game week data."""
    raw_stats_df = lib.read('players_raw', columns=['element_type', 'team', 'second_name', 'first_name', 'id']).data
 
    df = pd.DataFrame()
    for _, row in raw_stats_df.iterrows():
        player_id = row['id']
        player_name = f"{row['first_name']}_{row['second_name']}_{player_id}"
        symbol_name = normalize_name(player_name)
        if symbol_name not in ingested_symbols:
            continue
        player_gw_data = lib.read(
            symbol_name, columns=['total_points', 'value', 'round']
        ).data
        player_gw_data['id'] = player_id
        for col in row.index:
            player_gw_data[col] = row[col]
        df = pd.concat([df, player_gw_data], ignore_index=True)
 
    df = df[[
        'element_type', 'team', 'second_name', 'first_name', 'id',
        'total_points', 'value', 'round'
    ]]
    df = df.rename(columns={'round': 'Game_Week'})
    df["element_type"] = df["element_type"].map({1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'})
    return df

def select_position(position, count, max_players_per_team, max_spend, lib, current_players, current_spend, current_teams):
    """Select players for a specific position until the required count is reached."""
    q = adb.QueryBuilder()
    q = q[(q["element_type"] == position)]
    players_df = lib.read('game_week_filter', query_builder=q).data
    current_player_ids = [player['id'] for player in current_players]
    current_teams = current_teams.copy()
 
    selected_players = []
    if players_df.empty:
        return selected_players
 
    while count > 0:
        eligible = players_df[
            (~players_df['id'].isin(current_player_ids))
            & (players_df['team'].map(current_teams).fillna(0) < max_players_per_team)
            & (players_df['value'] <= max_spend - current_spend)
        ]
        if eligible.empty:
            # The random choices made so far cannot produce a valid squad.
            break

        player = eligible.sample().iloc[0]
        team_id = player['team']
        player_value = player['value']
 
        # if we haven't selected player already
        # and we haven't selected our max from each team
        # and we haven't spent too much
        if (player['id'] not in current_player_ids) and \
           (current_teams.get(team_id, 0) < max_players_per_team) and \
           (current_spend + player_value <= max_spend):
            # then add to roster
            selected_players.append(player)
            current_spend += player_value
            current_teams[team_id] += 1
            current_player_ids.append(player['id'])
            count -= 1
 
    return selected_players
 
def select_random_team(team_structure, max_players_per_team, max_spend, lib):
    """Select a random team of players based on position, budget, and team constraints."""
    total_spend = 0
    team_counts = Counter()
    selected_players = []
    keys = list(team_structure.keys())
    random.shuffle(keys)
    randomized_team_structure = {key: team_structure[key] for key in keys}
   
    for position, count in randomized_team_structure.items():
        players = select_position(position, count, max_players_per_team, max_spend, lib, selected_players, total_spend, team_counts)
        for player in players:
            total_spend+=player['value']
            team_counts[player['team']] += 1
            selected_players.append(player)
   
    return pd.DataFrame(selected_players) # Define the team structure with required player counts for each position


# Constants
PLAYERS_DIR = './data/2023-24/players/'
RAW_DATA_PATH = './data/2023-24/players_raw.csv'
GAME_WEEK = 38
MAX_PLAYERS_PER_TEAM = 3
MAX_SPEND = 1000
RUNS = 100000

# DK
GAME_WEEK = 1
PLAYERS_DIR = './data/2026-27/players/'
RAW_DATA_PATH = './data/2026-27/players_raw.csv'
MAX_SPEND = 100000000
RUNS=500000

# Connect to ArcticDB
arctic = adb.Arctic("lmdb://fantasy_football")
library = arctic.get_library('players', create_if_missing=True)
 
# Main execution
library.write('players_raw', pd.read_csv(RAW_DATA_PATH))

if GAME_WEEK == 1:
    # There are no current-season gameweek results before GW1. FPL's ep_next is
    # the forecast that is actually available at this point in the season.
    library.write('game_week_filter', build_preseason_player_pool(RAW_DATA_PATH))
else:
    ingested_symbols = ingest_player_data(PLAYERS_DIR, library)
    library.write('all_data', merge_player_data(library, ingested_symbols))

    # Average only completed GWs in the six-GW window. The upper bound avoids
    # leaking future results when running against an end-of-season data dump.
    q2 = adb.QueryBuilder()
    q2 = q2[
        (q2["Game_Week"] >= max(1, GAME_WEEK - 6))
        & (q2["Game_Week"] <= GAME_WEEK - 1)
    ].groupby("id").agg({"total_points": "mean"})
    new_total_points = library.read("all_data", query_builder=q2).data.reset_index()

    # Use the previous GW row for current price and player metadata.
    q3 = adb.QueryBuilder()
    q3 = q3[(q3["Game_Week"] == GAME_WEEK - 1)]
    game_week_filter = library.read("all_data", query_builder=q3).data

    merged_df = game_week_filter.merge(
        new_total_points, on='id', how='left', suffixes=('', '_new')
    )
    merged_df['average_total_points'] = merged_df['total_points_new'].fillna(
        merged_df['total_points']
    )
    merged_df = merged_df.drop(columns=['total_points_new'])
    library.write('game_week_filter', merged_df)
print('22222')
 
# Team Selection Simulation
team_structure = {'GK': 2, 'DEF': 5, 'MID': 5, 'FWD': 3}
all_teams = []
 
print(type(RUNS))
for run_id in range(RUNS):
    print(f"runs {run_id}")
    team_df = select_random_team(team_structure, MAX_PLAYERS_PER_TEAM, MAX_SPEND, library)
    if len(team_df) != sum(team_structure.values()):
        # Skip a random attempt that painted itself into a budget/club-limit corner.
        continue
    team_df['run_ID'] = run_id
    all_teams.append(team_df)

if not all_teams:
    raise RuntimeError("No valid squad was generated; increase RUNS or check the constraints")

all_teams_df = pd.concat(all_teams, ignore_index=True)
total_points_per_run = all_teams_df.groupby('run_ID')['average_total_points'].sum()
best_run_id = total_points_per_run.idxmax()
best_team_df = all_teams_df[all_teams_df['run_ID'] == best_run_id]
total_spend_best_team = best_team_df['value'].sum()
 
# Display the best team
print("\nBest Team (Run ID with highest total points):")
print(tabulate(best_team_df, headers='keys', tablefmt='fancy_grid'))
print("\nTotal Points of Best Team:", best_team_df['average_total_points'].sum())
print("Total Spend on Best Team:", total_spend_best_team)
