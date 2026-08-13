#!/usr/bin/env python3
"""Duke's FF26 market-data updater.

Outputs ff26-market.json with:
- Tier: FantasyPros 2026 PPR overall PRESEASON consensus tier.
- Trend: clean PPR PPG delta. During preseason, 2025 - 2024.
         Once 2026 has games, 2026 - 2025 automatically.

Requires FANTASYPROS_API_KEY for live FantasyPros data.
No third-party Python packages are required.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYERS_PATH = ROOT / "players.json"
MARKET_PATH = ROOT / "ff26-market.json"

FP_BASE = "https://api.fantasypros.com/public/v2/json"
USER_AGENT = "DukesFF26MarketUpdater/1.0 (+GitHub Actions)"
SEASON = 2026

TEAM_ALIASES = {"JAX": "JAC", "WSH": "WAS", "LA": "LAR"}

def norm_team(value: object) -> str:
    team = str(value or "").strip().upper()
    return TEAM_ALIASES.get(team, team)

def norm_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace(".", "")
    text = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", text, flags=re.I)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)

def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))

def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)

def fetch_json(url: str, api_key: str) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "x-api-key": api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f"FantasyPros returned HTTP {response.status}: {url}")
        return json.loads(response.read().decode("utf-8"))

def find_player_list(payload: object) -> list[dict]:
    """Find the FantasyPros players array even if response wrapping evolves."""
    if isinstance(payload, dict):
        direct = payload.get("players")
        if isinstance(direct, list):
            return [x for x in direct if isinstance(x, dict)]
        for value in payload.values():
            found = find_player_list(value)
            if found:
                return found
    elif isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload):
            return payload
        for value in payload:
            found = find_player_list(value)
            if found:
                return found
    return []

def fp_name(rec: dict) -> str:
    return str(rec.get("player_name") or rec.get("name") or "").strip()

def fp_team(rec: dict) -> str:
    return norm_team(rec.get("player_team_id") or rec.get("team_id") or rec.get("team"))

def index_records(records: list[dict]) -> tuple[dict, dict]:
    by_name: dict[str, list[dict]] = {}
    by_name_team: dict[tuple[str, str], dict] = {}
    for rec in records:
        name = norm_name(fp_name(rec))
        if not name:
            continue
        team = fp_team(rec)
        by_name.setdefault(name, []).append(rec)
        if team:
            by_name_team[(name, team)] = rec
    return by_name, by_name_team

def match_record(player: dict, by_name: dict, by_name_team: dict):
    name = norm_name(player.get("name"))
    team = norm_team(player.get("team"))
    if (name, team) in by_name_team:
        return by_name_team[(name, team)], "name+team"
    candidates = by_name.get(name, [])
    if len(candidates) == 1:
        return candidates[0], "name-only"
    return None, "unmatched"

def tier_value(rec: dict) -> int | None:
    for key in ("tier", "rank_tier", "tier_ecr"):
        value = rec.get(key)
        try:
            tier = int(value)
            if 1 <= tier <= 99:
                return tier
        except (TypeError, ValueError):
            pass
    return None

def games_value(rec: dict) -> int:
    try:
        return max(0, int(rec.get("games") or 0))
    except (TypeError, ValueError):
        return 0

def average_value(rec: dict) -> float | None:
    value = rec.get("average")
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None

def get_live_payloads(api_key: str):
    tier_qs = urllib.parse.urlencode({
        "position": "ALL",
        "scoring": "PPR",
        "type": "PRESEASON",
        "week": 0,
    })
    tiers = fetch_json(
        f"{FP_BASE}/nfl/{SEASON}/consensus-rankings?{tier_qs}",
        api_key,
    )
    points_2026 = fetch_json(
        f"{FP_BASE}/nfl/2026/player-points?position=ALL&scoring=PPR&start=1&end=18&min=true",
        api_key,
    )
    points_2025 = fetch_json(
        f"{FP_BASE}/nfl/2025/player-points?position=ALL&scoring=PPR&start=1&end=18&min=true",
        api_key,
    )
    points_2024 = fetch_json(
        f"{FP_BASE}/nfl/2024/player-points?position=ALL&scoring=PPR&start=1&end=18&min=true",
        api_key,
    )
    return tiers, points_2026, points_2025, points_2024

def build_market(players_payload: dict, old_market: dict, payloads: tuple[object, object, object, object]):
    player_list = players_payload.get("players")
    if not isinstance(player_list, list) or len(player_list) < 300:
        raise RuntimeError("players.json is missing the expected FF26 player list.")

    tier_payload, pts26_payload, pts25_payload, pts24_payload = payloads
    tier_records = find_player_list(tier_payload)
    p26_records = find_player_list(pts26_payload)
    p25_records = find_player_list(pts25_payload)
    p24_records = find_player_list(pts24_payload)

    # FantasyPros prototype/free keys may return sample-sized datasets.
    # Treat a non-empty response as usable and safely fall back for unmatched players.
    # An empty response still indicates a real API/schema problem and should stop the update.
    if not tier_records:
        raise RuntimeError("FantasyPros tier response contained no player records.")
    if not p25_records or not p24_records:
        raise RuntimeError("FantasyPros historical player-points response contained no player records.")

    tier_by_name, tier_by_name_team = index_records(tier_records)
    p26_by_name, p26_by_name_team = index_records(p26_records)
    p25_by_name, p25_by_name_team = index_records(p25_records)
    p24_by_name, p24_by_name_team = index_records(p24_records)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_players = {}
    tier_matches = tier_fallbacks = 0
    trend_calc = trend_missing = 0
    current_2026_count = 0

    old_players = old_market.get("players") if isinstance(old_market.get("players"), dict) else {}

    for player in player_list:
        name = str(player.get("name") or "").strip()
        if not name:
            continue

        # TIER
        tier_rec, tier_match_type = match_record(player, tier_by_name, tier_by_name_team)
        live_tier = tier_value(tier_rec or {})
        if live_tier is not None:
            tier = live_tier
            tier_source = "FANTASYPROS_PPR_PRESEASON"
            tier_matches += 1
        else:
            old = old_players.get(name, {}) if isinstance(old_players.get(name), dict) else {}
            fallback = old.get("tier", player.get("tier"))
            tier = int(fallback) if isinstance(fallback, (int, float)) and fallback else None
            tier_source = "FF26_FALLBACK_UNMATCHED_FANTASYPROS"
            tier_fallbacks += 1

        # TREND: use 2026-vs-2025 once the player has a 2026 game;
        # otherwise preserve clean preseason comparison 2025-vs-2024.
        rec26, match26 = match_record(player, p26_by_name, p26_by_name_team)
        rec25, match25 = match_record(player, p25_by_name, p25_by_name_team)
        rec24, match24 = match_record(player, p24_by_name, p24_by_name_team)

        avg26 = average_value(rec26 or {})
        avg25 = average_value(rec25 or {})
        avg24 = average_value(rec24 or {})
        games26 = games_value(rec26 or {})
        games25 = games_value(rec25 or {})
        games24 = games_value(rec24 or {})

        live_trend = False
        if games26 > 0 and avg26 is not None and avg25 is not None:
            current_season, previous_season = 2026, 2025
            current_ppg, previous_ppg = avg26, avg25
            current_games, previous_games = games26, games25
            current_2026_count += 1
            live_trend = True
        elif avg25 is not None and avg24 is not None:
            current_season, previous_season = 2025, 2024
            current_ppg, previous_ppg = avg25, avg24
            current_games, previous_games = games25, games24
            live_trend = True
        else:
            # Limited/sample API responses must not erase valid FF26 baseline data.
            old = old_players.get(name, {}) if isinstance(old_players.get(name), dict) else {}
            fallback_trend = old.get("trend", player.get("trend"))
            fallback_current = old.get("currentPpg", player.get("ppg25"))
            fallback_previous = old.get("previousPpg", player.get("ppg24"))
            fallback_current_season = old.get("currentSeason", 2025 if fallback_current is not None else None)
            fallback_previous_season = old.get("previousSeason", 2024 if fallback_previous is not None else None)
            fallback_current_games = old.get("currentGames", 0)
            fallback_previous_games = old.get("previousGames", 0)

            try:
                trend = round(float(fallback_trend), 1) if fallback_trend is not None else None
            except (TypeError, ValueError):
                trend = None

            current_season = fallback_current_season
            previous_season = fallback_previous_season
            current_ppg = fallback_current
            previous_ppg = fallback_previous
            current_games = fallback_current_games
            previous_games = fallback_previous_games

        if live_trend:
            trend = round(current_ppg - previous_ppg, 1)

        if trend is None:
            trend_missing += 1
            trend_source = "UNAVAILABLE"
        elif live_trend:
            trend_calc += 1
            trend_source = "FANTASYPROS_PPR_PLAYER_POINTS"
        else:
            trend_calc += 1
            trend_source = "FF26_FALLBACK_UNMATCHED_FANTASYPROS"

        out_players[name] = {
            "tier": tier,
            "tierSource": tier_source,
            "tierMatchType": tier_match_type,
            "trend": trend,
            "trendSource": trend_source,
            "currentSeason": current_season,
            "previousSeason": previous_season,
            "currentPpg": round(float(current_ppg), 2) if current_ppg is not None else None,
            "previousPpg": round(float(previous_ppg), 2) if previous_ppg is not None else None,
            "currentGames": int(current_games or 0),
            "previousGames": int(previous_games or 0),
        }

    return {
        "schemaVersion": 1,
        "season": 2026,
        "scoring": "PPR",
        "updatedAt": now,
        "source": {
            "tier": {
                "authority": "FantasyPros",
                "dataset": "2026 PPR PRESEASON overall consensus rankings",
                "endpoint": "/nfl/2026/consensus-rankings",
            },
            "trend": {
                "authority": "FantasyPros",
                "dataset": "NFL PPR player-points",
                "method": "Current PPG minus immediately previous season PPG; preseason uses 2025 minus 2024.",
                "endpoint": "/nfl/{season}/player-points",
            },
        },
        "refreshStats": {
            "draftPlayers": len(player_list),
            "tierMatches": tier_matches,
            "tierFallbacks": tier_fallbacks,
            "trendCalculated": trend_calc,
            "trendUnavailable": trend_missing,
            "playersUsing2026Trend": current_2026_count,
            "fantasyProsTierRecordsReceived": len(tier_records),
            "fantasyPros2026PointRecordsReceived": len(p26_records),
            "fantasyPros2025PointRecordsReceived": len(p25_records),
            "fantasyPros2024PointRecordsReceived": len(p24_records),
        },
        "players": out_players,
    }

def self_test() -> int:
    players_payload = {
        "players": [
            {"name": "Test Runner Jr.", "team": "AAA", "tier": 7},
            {"name": "Test Catcher", "team": "BBB", "tier": 8},
        ] * 150
    }
    # Make names unique enough after first two by constructing a separate test instead.
    players_payload["players"] = [
        {"name": "Test Runner Jr.", "team": "AAA", "tier": 7},
        {"name": "Test Catcher", "team": "BBB", "tier": 8},
    ] + [{"name": f"Depth Player {i}", "team": "FA", "tier": 9} for i in range(298)]

    tiers = {"players": [
        {"player_name": "Test Runner", "player_team_id": "AAA", "tier": 1},
        {"player_name": "Test Catcher", "player_team_id": "BBB", "tier": 2},
    ] + [{"player_name": f"Depth Player {i}", "player_team_id": "FA", "tier": 9} for i in range(298)]}
    p26 = {"players": [
        {"player_name": "Test Runner", "team_id": "AAA", "games": 2, "average": 20.0},
        {"player_name": "Test Catcher", "team_id": "BBB", "games": 0, "average": 0.0},
    ] + [{"player_name": f"Depth Player {i}", "team_id": "FA", "games": 0, "average": 0.0} for i in range(298)]}
    p25 = {"players": [
        {"player_name": "Test Runner", "team_id": "AAA", "games": 17, "average": 15.0},
        {"player_name": "Test Catcher", "team_id": "BBB", "games": 17, "average": 12.0},
    ] + [{"player_name": f"Depth Player {i}", "team_id": "FA", "games": 17, "average": 10.0} for i in range(298)]}
    p24 = {"players": [
        {"player_name": "Test Runner", "team_id": "AAA", "games": 17, "average": 14.0},
        {"player_name": "Test Catcher", "team_id": "BBB", "games": 17, "average": 10.0},
    ] + [{"player_name": f"Depth Player {i}", "team_id": "FA", "games": 17, "average": 9.0} for i in range(298)]}

    out = build_market(players_payload, {}, (tiers, p26, p25, p24))
    assert out["players"]["Test Runner Jr."]["tier"] == 1
    assert out["players"]["Test Runner Jr."]["trend"] == 5.0
    assert out["players"]["Test Runner Jr."]["currentSeason"] == 2026
    assert out["players"]["Test Catcher"]["trend"] == 2.0
    assert out["players"]["Test Catcher"]["currentSeason"] == 2025
    assert out["refreshStats"]["tierMatches"] == 300
    print("FF26 market updater self-test: PASS")
    return 0

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    api_key = os.environ.get("FANTASYPROS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "FANTASYPROS_API_KEY is not configured. Add it as a GitHub Actions repository secret "
            "before enabling the live market workflow."
        )

    players_payload = load_json(PLAYERS_PATH, {})
    old_market = load_json(MARKET_PATH, {})
    payloads = get_live_payloads(api_key)
    out = build_market(players_payload, old_market, payloads)

    with MARKET_PATH.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    stats = out["refreshStats"]
    print(
        f"Updated {MARKET_PATH.name}: "
        f"tiers={stats['tierMatches']} matched/{stats['tierFallbacks']} fallback, "
        f"trend={stats['trendCalculated']} calculated/{stats['trendUnavailable']} unavailable, "
        f"2026trend={stats['playersUsing2026Trend']}"
    )
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FF26 market updater failed: {exc}", file=sys.stderr)
        raise
