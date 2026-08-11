#!/usr/bin/env python3
"""Duke's FF26 live player-status updater.

Refreshes ff26-status.json from Sleeper's NFL player feed while preserving
existing richer news fields. Also maintains a two-strike player-pool relevance
gate without deleting any player from players.json.

Pool rules:
- Matched current player -> ACTIVE, unmatched streak resets to 0.
- First unmatched daily check -> MONITOR, still draftable.
- Second consecutive unmatched daily check -> INACTIVE, hidden by the app.
- Same-day manual reruns do not advance the streak.
- Team conflicts do not count as misses and reset the unmatched streak.
- A later reliable match automatically restores ACTIVE status.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PLAYERS_PATH = ROOT / "players.json"
STATUS_PATH = ROOT / "ff26-status.json"
SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
USER_AGENT = "DukesFF26StatusUpdater/1.1 (+GitHub Actions)"
POOL_TIMEZONE = ZoneInfo("America/New_York")
INACTIVE_AFTER_DAILY_MISSES = 2

RED_TOKENS = {
    "out", "o", "ir", "injured reserve", "pup", "nfi", "reserve", "suspended",
}
YELLOW_TOKENS = {
    "questionable", "q", "doubtful", "d", "limited", "lp", "did not practice",
    "dnp", "non-participant", "not participating",
}
GREEN_STATUS_TOKENS = {"active"}

TEAM_ALIASES = {
    "JAX": "JAC",
    "WSH": "WAS",
    "LA": "LAR",
}

NAME_ALIASES = {
    "hollywood brown": "marquise brown",
    "bam knight": "zonovan knight",
}

FREE_AGENT_TEAMS = {"", "FA"}


def norm_team(value: object) -> str:
    team = str(value or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def norm_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace(".", "")
    text = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", text, flags=re.I)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().lower()
    normalized = re.sub(r"\s+", " ", text)
    return NAME_ALIASES.get(normalized, normalized)


def sleeper_full_name(record: dict) -> str:
    full = record.get("full_name")
    if full:
        return str(full)
    first = str(record.get("first_name") or "").strip()
    last = str(record.get("last_name") or "").strip()
    return f"{first} {last}".strip()


def fetch_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} from {url}")
        return json.loads(response.read().decode("utf-8"))


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def text_tokens(*values: object) -> str:
    return " ".join(
        str(v or "").strip().lower()
        for v in values
        if v is not None
    )


def classify(record: dict) -> tuple[str, str, str]:
    injury_status = str(record.get("injury_status") or "").strip()
    body_part = str(record.get("injury_body_part") or "").strip()
    practice = str(record.get("practice_participation") or "").strip()
    roster_status = str(record.get("status") or "").strip()
    notes = str(record.get("injury_notes") or "").strip()

    availability = text_tokens(injury_status, roster_status)
    caution = text_tokens(injury_status, practice, notes)

    # RED means explicitly unavailable, not merely "missed practice".
    if any(
        token == availability
        or f" {token} " in f" {availability} "
        for token in RED_TOKENS
    ):
        return (
            "RED",
            "NOT AVAILABLE / OUT",
            (injury_status or roster_status or "UNAVAILABLE").upper(),
        )

    has_injury = bool(injury_status or body_part or notes)
    has_caution = any(
        token == caution
        or f" {token} " in f" {caution} "
        for token in YELLOW_TOKENS
    )

    if has_injury or has_caution:
        return (
            "YELLOW",
            "QUESTIONABLE / CAUTION",
            (body_part or injury_status or "INJURY MONITOR").upper(),
        )

    if roster_status.lower() in GREEN_STATUS_TOKENS or not availability:
        return "GREEN", "GOOD TO GO", "HEALTHY"

    return (
        "YELLOW",
        "STATUS MONITOR",
        roster_status.upper() or "STATUS MONITOR",
    )


def summary_for(record: dict, status: str) -> str:
    body_part = str(record.get("injury_body_part") or "").strip()
    injury_status = str(record.get("injury_status") or "").strip()
    practice = str(record.get("practice_participation") or "").strip()
    notes = str(record.get("injury_notes") or "").strip()

    if notes:
        return notes

    details = []
    if body_part:
        details.append(body_part)
    if injury_status:
        details.append(injury_status)
    if practice:
        details.append(f"practice: {practice}")

    if details:
        return "Current Sleeper availability data: " + "; ".join(details) + "."
    if status == "GREEN":
        return (
            "No active injury or availability concern is listed "
            "in the current Sleeper player feed."
        )
    return "Current availability should be monitored."


def impact_for(status: str, record: dict) -> str:
    practice = str(record.get("practice_participation") or "").strip().lower()

    if status == "RED":
        return (
            "Active availability penalty. Do not treat the player as "
            "currently available until status changes."
        )

    if status == "YELLOW":
        if practice:
            return (
                "Draft caution only; monitor practice participation and updated "
                "availability before making a major ranking move."
            )
        return (
            "Draft caution only; monitor updated availability before making "
            "a major ranking move."
        )

    return "No active availability penalty."


def build_indexes(sleeper_map: dict) -> tuple[dict, dict]:
    by_name = {}
    by_name_team = {}

    for sleeper_id, rec in sleeper_map.items():
        if not isinstance(rec, dict):
            continue

        rec = dict(rec)
        rec.setdefault("player_id", sleeper_id)

        name = norm_name(sleeper_full_name(rec))
        if not name:
            continue

        team = norm_team(rec.get("team"))
        by_name.setdefault(name, []).append(rec)

        if team:
            by_name_team[(name, team)] = rec

    return by_name, by_name_team


def match_player(
    player: dict,
    by_name: dict,
    by_name_team: dict,
) -> tuple[dict | None, str]:
    name = norm_name(player.get("name"))
    team = norm_team(player.get("team"))

    exact = by_name_team.get((name, team))
    if exact:
        return exact, "name+team"

    candidates = by_name.get(name, [])
    if len(candidates) == 1:
        candidate = candidates[0]
        source_team = norm_team(candidate.get("team"))

        # Name-only matching is safe when one side has no active team assignment.
        # If both sides name different real NFL teams, do not silently attach status.
        if team in FREE_AGENT_TEAMS or source_team in FREE_AGENT_TEAMS:
            return candidate, "name-only"

        return None, "team-conflict"

    return None, "unmatched"


def local_date_from_timestamp(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(POOL_TIMEZONE).date().isoformat()


def safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def previous_pool_state(old: dict) -> tuple[int, str | None]:
    """Read current persisted streak, including migration from schema v2."""
    streak = safe_nonnegative_int(old.get("unmatchedStreak"))
    last_unmatched = str(old.get("lastUnmatchedDate") or "").strip() or None

    # Migration from the existing schema: an already-unmatched record represents
    # one known miss. Its old updatedAt timestamp tells us which local day it was.
    if streak == 0 and old.get("matchStatus") == "UNMATCHED_CURRENT_FEED":
        prior_date = (
            last_unmatched
            or local_date_from_timestamp(old.get("updatedAt"))
        )
        if prior_date:
            streak = 1
            last_unmatched = prior_date

    return streak, last_unmatched


def next_unmatched_pool_state(
    old: dict,
    check_date: str,
) -> tuple[int, str]:
    """Advance at most once per local calendar day."""
    old_streak, last_unmatched = previous_pool_state(old)

    if last_unmatched == check_date:
        new_streak = max(1, old_streak)
    else:
        consecutive = False
        if last_unmatched:
            try:
                previous_day = date.fromisoformat(last_unmatched)
                today = date.fromisoformat(check_date)
                consecutive = (today - previous_day).days == 1
            except ValueError:
                consecutive = False

        new_streak = old_streak + 1 if consecutive else 1

    pool_status = (
        "INACTIVE"
        if new_streak >= INACTIVE_AFTER_DAILY_MISSES
        else "MONITOR"
    )
    return new_streak, pool_status


def unmatched_placeholder(old: dict) -> dict:
    """Ensure even a never-before-seen unmatched player gets a status record."""
    if old:
        return dict(old)

    return {
        "status": "UNKNOWN",
        "label": "STATUS NOT LISTED",
        "reasonType": "UNMATCHED CURRENT FEED",
        "summary": (
            "No reliable current player match was found in the availability feed."
        ),
        "fantasyImpact": (
            "No automatic injury penalty. Player-pool relevance is being monitored."
        ),
    }


def main() -> int:
    players_payload = load_json(PLAYERS_PATH, {})
    player_list = (
        players_payload.get("players")
        if isinstance(players_payload, dict)
        else None
    )

    if not isinstance(player_list, list) or len(player_list) < 300:
        raise RuntimeError(
            "players.json is missing or does not contain the expected player list"
        )

    existing = load_json(STATUS_PATH, {})
    if not isinstance(existing, dict):
        existing = {}

    old_players = (
        existing.get("players")
        if isinstance(existing.get("players"), dict)
        else {}
    )
    old_dst = (
        existing.get("dst")
        if isinstance(existing.get("dst"), dict)
        else {}
    )

    sleeper = fetch_json(SLEEPER_URL)
    if not isinstance(sleeper, dict) or len(sleeper) < 1000:
        raise RuntimeError(
            "Sleeper player feed returned an unexpectedly small payload"
        )

    by_name, by_name_team = build_indexes(sleeper)

    refreshed = {}
    matched = 0
    name_only = 0
    team_conflicts = []
    unmatched = []
    changed_statuses = 0

    pool_counts = {
        "ACTIVE": 0,
        "MONITOR": 0,
        "INACTIVE": 0,
    }

    now_dt = datetime.now(timezone.utc).astimezone(POOL_TIMEZONE)
    now = now_dt.isoformat(timespec="seconds")
    check_date = now_dt.date().isoformat()

    for player in player_list:
        name = str(player.get("name") or "").strip()

        if not name or str(player.get("pos") or "").upper() == "DEF":
            continue

        rec, match_type = match_player(
            player,
            by_name,
            by_name_team,
        )

        old = (
            old_players.get(name, {})
            if isinstance(old_players.get(name), dict)
            else {}
        )

        if not rec:
            kept = unmatched_placeholder(old)
            kept["poolCheckDate"] = check_date

            if match_type == "team-conflict":
                team_conflicts.append(name)

                # A source/team disagreement is not an unmatched strike.
                # Reset the consecutive-miss streak and keep the player visible.
                kept["matchStatus"] = "TEAM_CONFLICT_CURRENT_FEED"
                kept["unmatchedStreak"] = 0
                kept["lastUnmatchedDate"] = None
                kept["poolStatus"] = "MONITOR"
            else:
                unmatched.append(name)

                new_streak, pool_status = next_unmatched_pool_state(
                    old,
                    check_date,
                )

                kept["matchStatus"] = "UNMATCHED_CURRENT_FEED"
                kept["unmatchedStreak"] = new_streak
                kept["lastUnmatchedDate"] = check_date
                kept["poolStatus"] = pool_status

            pool_counts[kept["poolStatus"]] += 1
            refreshed[name] = kept
            continue

        matched += 1
        if match_type == "name-only":
            name_only += 1

        status, label, reason = classify(rec)

        if old.get("status") and old.get("status") != status:
            changed_statuses += 1

        new_record = {
            "status": status,
            "label": label,
            "reasonType": reason,
            "summary": summary_for(rec, status),
            "fantasyImpact": impact_for(status, rec),
            "updatedAt": now,
            "sourceUrl": "https://docs.sleeper.com/",
            "sourceType": "SLEEPER_PLAYER_FEED",
            "sourcePlayerId": rec.get("player_id"),
            "sourceTeam": norm_team(rec.get("team")),
            "matchType": match_type,

            # Reliable current match = automatic restoration.
            "matchStatus": "MATCHED_CURRENT_FEED",
            "unmatchedStreak": 0,
            "lastUnmatchedDate": None,
            "poolCheckDate": check_date,
            "poolStatus": "ACTIVE",
        }

        # Preserve richer researched/news fields.
        for key in (
            "recentNews",
            "trainingCampUpdate",
            "notes",
            "newsSourceUrl",
            "newsPublishedAt",
        ):
            if old.get(key):
                new_record[key] = old[key]

        pool_counts["ACTIVE"] += 1
        refreshed[name] = new_record

    out = {
        "schemaVersion": 3,
        "season": 2026,
        "scoring": "PPR",
        "updatedAt": now,
        "source": {
            "availability": "Sleeper NFL player feed",
            "availabilityUrl": SLEEPER_URL,
            "refreshPolicy": (
                "Daily scheduled refresh; manual workflow dispatch available"
            ),
        },
        "statusRules": {
            "GREEN": "Good to go / no active availability concern",
            "YELLOW": (
                "Questionable, limited, injury monitor, "
                "or uncertain availability"
            ),
            "RED": (
                "Explicitly unavailable: out, IR, PUP/NFI, "
                "suspended, or equivalent"
            ),
            "UNKNOWN": (
                "No reliable current player match; do not infer health"
            ),
        },
        "poolRules": {
            "ACTIVE": "Reliable current match; visible and draftable",
            "MONITOR": (
                "First daily unmatched check or team conflict; "
                "visible and draftable"
            ),
            "INACTIVE": (
                f"{INACTIVE_AFTER_DAILY_MISSES} consecutive daily unmatched "
                "checks; retained in database but hidden from draft pool"
            ),
            "automaticRestore": (
                "Any later reliable current match resets the streak "
                "and restores ACTIVE"
            ),
        },
        "refreshStats": {
            "draftPlayers": len(player_list),
            "matchedPlayers": matched,
            "nameOnlyMatches": name_only,
            "teamConflicts": len(team_conflicts),
            "unmatchedPlayers": len(unmatched),
            "statusChanges": changed_statuses,
            "activePlayers": pool_counts["ACTIVE"],
            "monitorPlayers": pool_counts["MONITOR"],
            "inactivePlayers": pool_counts["INACTIVE"],
        },
        "players": refreshed,
        "dst": old_dst,
    }

    with STATUS_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        f"Updated {STATUS_PATH.name}: "
        f"matched={matched}, "
        f"name_only={name_only}, "
        f"team_conflicts={len(team_conflicts)}, "
        f"unmatched={len(unmatched)}, "
        f"status_changes={changed_statuses}, "
        f"active={pool_counts['ACTIVE']}, "
        f"monitor={pool_counts['MONITOR']}, "
        f"inactive={pool_counts['INACTIVE']}"
    )

    if team_conflicts:
        print(
            "Team-conflict sample:",
            ", ".join(team_conflicts[:20]),
        )

    if unmatched:
        print(
            "Unmatched sample:",
            ", ".join(unmatched[:20]),
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"FF26 updater failed: {exc}",
            file=sys.stderr,
        )
        raise
