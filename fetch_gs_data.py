#!/usr/bin/env python3
"""
Grand Slam Data Fetcher
=======================
Fetches grand slam + bases-loaded PA data from Baseball Savant (Statcast)
and generates pre-baked JSON files for the grand slam research tool.

Usage:
    python3 fetch_gs_data.py              # fetches 2024 + 2025
    python3 fetch_gs_data.py --year 2024  # single year
    python3 fetch_gs_data.py --year 2025
    python3 fetch_gs_data.py --year 2026
    python3 fetch_gs_data.py --combine    # merges existing JSONs into combined

Output files (place in same folder as grand-slam-research.html):
    gs-data-2024.json
    gs-data-2025.json
    gs-data-2026.json
    gs-data-combined.json
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import date, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Season date ranges ────────────────────────────────────────────────
SEASONS = {
    "2024": {"start": "2024-03-20", "end": "2024-09-29"},
    "2025": {"start": "2025-03-27", "end": "2025-09-28"},
    "2026": {"start": "2026-03-27", "end": "2026-10-04"},
}

# ── Statcast URL builders ─────────────────────────────────────────────
def hr_url(start, end):
    """HR-only pull — for grand slam detection + HR context."""
    y = start[:4]
    return (
        f"https://baseballsavant.mlb.com/statcast_search/csv?all=true"
        f"&hfPT=&hfAB=home_run%7C&hfGT=R%7C&hfBBT=&hfPR=&hfZ=&stadium="
        f"&hfBBL=&hfNewZones=&hfC=&hfSea={y}%7C&hfSit=&player_type=batter"
        f"&hfOuts=&opponent=&pitcher_throws=&batter_stands=&hfSA="
        f"&game_date_gt={start}&game_date_lt={end}"
        f"&hfInfield=&team=&position=&hfOutfield=&hfRO=&home_road="
        f"&hfFlag=&hfPull=&metric_1=&hfInn=&min_pitches=0&min_results=0"
        f"&group_by=name&sort_col=pitches&player_event_sort=h_launch_speed"
        f"&sort_order=desc&min_abs=0&type=details"
    )

def bl_url(start, end):
    """All PA pull — filter client-side for bases-loaded situations."""
    y = start[:4]
    return (
        f"https://baseballsavant.mlb.com/statcast_search/csv?all=true"
        f"&hfPT=&hfAB=&hfGT=R%7C&hfBBT=&hfPR=&hfZ=&stadium="
        f"&hfBBL=&hfNewZones=&hfC=&hfSea={y}%7C&hfSit=&player_type=batter"
        f"&hfOuts=&opponent=&pitcher_throws=&batter_stands=&hfSA="
        f"&game_date_gt={start}&game_date_lt={end}"
        f"&hfInfield=&team=&position=&hfOutfield=&hfRO=&home_road="
        f"&hfFlag=&hfPull=&metric_1=&hfInn=&min_pitches=0&min_results=0"
        f"&group_by=name&sort_order=desc&min_abs=0&type=details"
    )

# ── Chunker ───────────────────────────────────────────────────────────
def chunks(start_str, end_str, days=30):
    chunks_out = []
    cur = date.fromisoformat(start_str)
    last = date.fromisoformat(end_str)
    while cur <= last:
        ce = min(cur + timedelta(days=days - 1), last)
        chunks_out.append((str(cur), str(ce)))
        cur = ce + timedelta(days=1)
    return chunks_out

# ── Fetch with retry ──────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://baseballsavant.mlb.com/statcast_search",
}

def fetch_csv(url, retries=3, delay=2):
    for attempt in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            if not raw or raw.strip()[0] == "<":
                print(f"    HTML response (not CSV), skipping")
                return []
            reader = csv.DictReader(io.StringIO(raw))
            return list(reader)
        except HTTPError as e:
            print(f"    HTTP {e.code} — attempt {attempt+1}/{retries}")
        except URLError as e:
            print(f"    URL error: {e.reason} — attempt {attempt+1}/{retries}")
        except Exception as e:
            print(f"    Error: {e} — attempt {attempt+1}/{retries}")
        if attempt < retries - 1:
            wait = delay * (attempt + 1)
            print(f"    Waiting {wait}s before retry…")
            time.sleep(wait)
    return []

# ── Helpers ───────────────────────────────────────────────────────────
def is_loaded(row):
    return bool(
        row.get("on_1b", "").strip()
        and row.get("on_2b", "").strip()
        and row.get("on_3b", "").strip()
    )

def team_for(row):
    if row.get("inning_topbot") == "Top":
        return row.get("away_team", "?")
    return row.get("home_team", "?")

def safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

# ── Aggregate ─────────────────────────────────────────────────────────
def aggregate(hr_rows, bl_rows):
    days = {}
    players = {}
    teams = {}
    gs_events = []

    print(f"  Aggregating {len(hr_rows)} HR rows…")
    for r in hr_rows:
        date_str = r.get("game_date", "")
        if not date_str:
            continue
        pid = r.get("batter", "")
        if not pid:
            continue
        dist = safe_int(r.get("hit_distance_sc"))
        gs = is_loaded(r)
        team = team_for(r) or "?"
        inning = safe_int(r.get("inning"))
        topbot = r.get("inning_topbot", "")
        outs = safe_int(r.get("outs_when_up"))
        name = r.get("player_name", "?")

        # Days
        if date_str not in days:
            days[date_str] = {"grandSlams": [], "allHRs": 0}
        days[date_str]["allHRs"] += 1
        if gs:
            ev = {
                "player": name,
                "pid": pid,
                "team": team,
                "dist": dist,
                "inning": inning,
                "topbot": topbot,
                "outs": outs,
            }
            days[date_str]["grandSlams"].append(ev)
            gs_events.append({**ev, "date": date_str})

        # Players
        if pid not in players:
            players[pid] = {
                "pid": pid,
                "name": name,
                "team": team,
                "grandSlams": 0,
                "blHR": 0,
                "hrTotal": 0,
                "blPA": 0,
            }
        p = players[pid]
        if team != "?":
            p["team"] = team
        p["hrTotal"] += 1
        if is_loaded(r):
            p["blHR"] += 1
        if gs:
            p["grandSlams"] += 1

        # Teams
        if team not in teams:
            teams[team] = {
                "team": team,
                "grandSlams": 0,
                "hrTotal": 0,
                "blPA": 0,
                "blHR": 0,
                "games": set(),
            }
        teams[team]["hrTotal"] += 1
        teams[team]["games"].add(f"{date_str}_{team}")
        if is_loaded(r):
            teams[team]["blHR"] += 1
        if gs:
            teams[team]["grandSlams"] += 1

    print(f"  Aggregating {len(bl_rows)} BL PA rows (deduplicating to unique plate appearances)…")
    # Deduplicate: one BL PA per unique (pid, game_pk, at_bat_number)
    # Statcast returns one row per PITCH, not per plate appearance
    seen_bl_pa = set()  # (pid, game_pk, at_bat_number)
    seen_bl_pa_team = set()  # (team, game_pk, at_bat_number)
    for r in bl_rows:
        if not is_loaded(r):
            continue
        pid = r.get("batter", "")
        if not pid:
            continue
        date_str = r.get("game_date", "")
        team = team_for(r) or "?"
        game_pk = r.get("game_pk", "")
        at_bat_num = r.get("at_bat_number", "")
        name = r.get("player_name", "?")

        # Deduplicate at player level
        pa_key = (pid, game_pk, at_bat_num)
        if pa_key not in seen_bl_pa:
            seen_bl_pa.add(pa_key)
            if pid not in players:
                players[pid] = {
                    "pid": pid, "name": name, "team": team,
                    "grandSlams": 0, "blHR": 0, "hrTotal": 0, "blPA": 0,
                }
            players[pid]["blPA"] += 1

        # Deduplicate at team level
        team_pa_key = (team, game_pk, at_bat_num)
        if team_pa_key not in seen_bl_pa_team:
            seen_bl_pa_team.add(team_pa_key)
            if team not in teams:
                teams[team] = {
                    "team": team, "grandSlams": 0, "hrTotal": 0,
                    "blPA": 0, "blHR": 0, "games": set(),
                }
            teams[team]["blPA"] += 1
            if date_str:
                teams[team]["games"].add(f"{date_str}_{team}")

    # Finalize teams — convert sets to counts
    for t in teams.values():
        t["games"] = len(t["games"])

    total_gs = sum(len(d["grandSlams"]) for d in days.values())
    gs_days = sum(1 for d in days.values() if d["grandSlams"])
    zero_days = len(days) - gs_days
    print(f"  → {len(days)} days · {total_gs} grand slams · {gs_days} GS days · {zero_days} zero days")
    print(f"  → {len(players)} players · {len(teams)} teams")

    return {
        "days": days,
        "players": list(players.values()),
        "teams": list(teams.values()),
        "gs_events": gs_events,
    }

# ── Fetch one season ──────────────────────────────────────────────────
def fetch_season(year):
    if year not in SEASONS:
        print(f"Unknown year: {year}. Supported: {list(SEASONS.keys())}")
        return None

    season = SEASONS[year]
    start, end = season["start"], season["end"]
    print(f"\n{'='*60}")
    print(f"Fetching {year} season ({start} → {end})")
    print(f"{'='*60}")

    # Phase 1: HR data (30-day chunks)
    cs = chunks(start, end, 30)
    print(f"\nPhase 1/2 — HR data ({len(cs)} chunks, ~{len(cs)*5}s estimated)")
    hr_rows = []
    for i, (s, e) in enumerate(cs):
        print(f"  [{i+1}/{len(cs)}] {s} → {e}", end=" ", flush=True)
        rows = fetch_csv(hr_url(s, e))
        hr_rows_chunk = [r for r in rows if r.get("events") == "home_run" and r.get("game_date")]
        print(f"→ {len(hr_rows_chunk)} HRs")
        hr_rows.extend(hr_rows_chunk)
        time.sleep(1.0)

    # Phase 2: BL PA data (15-day chunks — larger dataset)
    cs2 = chunks(start, end, 15)
    print(f"\nPhase 2/2 — Bases-loaded PA data ({len(cs2)} chunks, ~{len(cs2)*10}s estimated)")
    bl_rows = []
    for i, (s, e) in enumerate(cs2):
        print(f"  [{i+1}/{len(cs2)}] {s} → {e}", end=" ", flush=True)
        rows = fetch_csv(bl_url(s, e))
        # Filter to BL only client-side to reduce memory
        bl_chunk = [r for r in rows if r.get("game_date") and is_loaded(r)]
        print(f"→ {len(bl_chunk)} BL PAs (from {len(rows)} total PAs)")
        bl_rows.extend(bl_chunk)
        time.sleep(1.5)  # slightly longer delay for larger pulls

    print(f"\nTotal: {len(hr_rows)} HR rows, {len(bl_rows)} BL PA rows")

    # Aggregate
    print("\nAggregating…")
    agg = aggregate(hr_rows, bl_rows)

    total_gs = len(agg["gs_events"])
    gs_days = sum(1 for d in agg["days"].values() if d["grandSlams"])

    output = {
        "meta": {
            "season": year,
            "start": start,
            "end": end,
            "generated": str(date.today()),
            "total_gs": total_gs,
            "total_days": len(agg["days"]),
            "gs_days": gs_days,
            "zero_days": len(agg["days"]) - gs_days,
            "total_players": len(agg["players"]),
            "total_bl_pa": sum(p["blPA"] for p in agg["players"]),
        },
        "players": agg["players"],
        "teams": agg["teams"],
        "days": agg["days"],
    }

    filename = f"gs-data-{year}.json"
    with open(filename, "w") as f:
        json.dump(output, f, separators=(",", ":"))  # compact — no whitespace
    size_kb = os.path.getsize(filename) // 1024
    print(f"\n✓ Saved {filename} ({size_kb}KB)")
    return output

# ── Combine seasons ───────────────────────────────────────────────────
def combine_seasons(years):
    print(f"\n{'='*60}")
    print(f"Combining seasons: {', '.join(years)}")
    print(f"{'='*60}")

    all_players = {}  # pid -> merged player
    all_teams = {}    # team -> merged team
    all_days = {}     # date -> day

    for year in years:
        filename = f"gs-data-{year}.json"
        if not os.path.exists(filename):
            print(f"  ✗ {filename} not found — run fetch for {year} first")
            continue
        with open(filename) as f:
            d = json.load(f)
        print(f"  Loading {filename}: {d['meta']['total_gs']} GS over {d['meta']['total_days']} days")

        # Merge players
        for p in d["players"]:
            pid = p["pid"]
            if pid not in all_players:
                all_players[pid] = {**p}
            else:
                all_players[pid]["grandSlams"] += p["grandSlams"]
                all_players[pid]["blHR"] += p["blHR"]
                all_players[pid]["hrTotal"] += p["hrTotal"]
                all_players[pid]["blPA"] += p["blPA"]
                # Use most recent team
                all_players[pid]["team"] = p["team"]

        # Merge teams
        for t in d["teams"]:
            team = t["team"]
            if team not in all_teams:
                all_teams[team] = {**t}
            else:
                all_teams[team]["grandSlams"] += t["grandSlams"]
                all_teams[team]["hrTotal"] += t["hrTotal"]
                all_teams[team]["blPA"] += t["blPA"]
                all_teams[team]["blHR"] += t["blHR"]
                all_teams[team]["games"] += t["games"]

        # Merge days (dates are unique per season)
        for date_str, day in d["days"].items():
            all_days[date_str] = day

    total_gs = sum(p["grandSlams"] for p in all_players.values())
    gs_days = sum(1 for d in all_days.values() if d["grandSlams"])

    output = {
        "meta": {
            "seasons": years,
            "generated": str(date.today()),
            "total_gs": total_gs,
            "total_days": len(all_days),
            "gs_days": gs_days,
            "zero_days": len(all_days) - gs_days,
            "total_players": len(all_players),
            "total_bl_pa": sum(p["blPA"] for p in all_players.values()),
        },
        "players": list(all_players.values()),
        "teams": list(all_teams.values()),
        "days": all_days,
    }

    filename = "gs-data-combined.json"
    with open(filename, "w") as f:
        json.dump(output, f, separators=(",", ":"))
    size_kb = os.path.getsize(filename) // 1024
    print(f"\n✓ Saved {filename} ({size_kb}KB)")
    print(f"  {total_gs} total grand slams · {len(all_players)} players · {len(all_days)} days")

# ── Main ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Fetch grand slam data from Statcast and generate pre-baked JSON files."
    )
    parser.add_argument("--year", choices=["2024", "2025", "2026"], help="Fetch a specific season")
    parser.add_argument("--combine", action="store_true", help="Combine existing season JSONs into gs-data-combined.json")
    parser.add_argument("--years", default="2024,2025", help="Comma-separated years to combine (default: 2024,2025)")
    args = parser.parse_args()

    if args.combine:
        years = [y.strip() for y in args.years.split(",")]
        combine_seasons(years)
        return

    if args.year:
        fetch_season(args.year)
        # Auto-combine if other season JSON already exists
        other_years = [y for y in ["2024", "2025", "2026"] if y != args.year]
        existing = [y for y in other_years if os.path.exists(f"gs-data-{y}.json")]
        if existing:
            combine_with = sorted([args.year] + existing)
            print(f"\nFound existing JSONs for {existing} — combining {combine_with}…")
            combine_seasons(combine_with)
    else:
        # Default: fetch 2024 and 2025
        print("No --year specified. Fetching 2024 and 2025 (use --year to fetch a specific season).")
        print("Estimated time: 20-40 minutes total (large dataset).\n")
        for year in ["2024", "2025"]:
            fetch_season(year)
        combine_seasons(["2024", "2025"])

    print("\n" + "="*60)
    print("Done! Files generated:")
    for f in sorted(os.listdir(".")):
        if f.startswith("gs-data-") and f.endswith(".json"):
            size_kb = os.path.getsize(f) // 1024
            print(f"  {f}  ({size_kb}KB)")
    print("\nPlace these JSON files in the same folder as grand-slam-research.html")
    print("The tool will load them instantly instead of hitting Statcast.")

if __name__ == "__main__":
    main()