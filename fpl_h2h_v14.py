#!/usr/bin/env python3
"""
fpl_h2h.py — FPL Head-to-Head Ownership & Point-Swing Analyser
===============================================================
Scrapes the official FPL API and compares player ownership between
two teams of 4 managers each, then calculates the actual point swing
each player caused based on their live GW score.

Usage
-----
  python fpl_h2h.py \\
      --gw 29 \\
      --team-a 111111 222222 333333 444444 \\
      --team-b 555555 666666 777777 888888 \\
      --cap-a 1 \\   # 1-indexed position in --team-a that is captain
      --cap-b 2       # 1-indexed position in --team-b that is captain

Captain / multiplier logic
--------------------------
Each player's ownership count is determined by two independent multipliers
that stack multiplicatively:

  H2H captain manager  x2   (the manager marked via --cap-a / --cap-b)
  FPL captain player   x2   (normal) or x3 (Triple Captain chip active)

  Normal cap, normal mgr           -> count = 2
  Normal cap, H2H cap mgr          -> count = 4   (2 × 2)
  Triple Captain, normal mgr       -> count = 3   (3 × 1)
  Triple Captain, H2H cap mgr      -> count = 6   (3 × 2)
  Normal player, normal mgr        -> count = 1
  Normal player, H2H cap mgr       -> count = 2

  Maximum count per team for any player = 3×1 + 1×6 = 9  (TC + H2H cap)

Point swing
-----------
  diff        = count_A - count_B            (ownership differential)
  live_pts    = player's GW points (raw, before any FPL captain doubling)
  point_swing = diff x live_pts

  A positive swing means Team A gained points on Team B because of that player.
  A negative swing means Team B gained.
  Rows are sorted by abs(point_swing) descending so the biggest movers appear first.
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

# ── FPL API ───────────────────────────────────────────────────────────────────

BASE = "https://fantasy.premierleague.com/api"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
# 2026/27 Premier League teams (per bootstrap-static `teams`, verified live).
# Promoted this season: Coventry City, Hull City, Ipswich Town.
# Relegated out of the top flight (removed below): Burnley, West Ham, Wolves.
# NOTE: team IDs are re-assigned by FPL every season based on this same
# bootstrap-static payload, so this table is refreshed for 26/27 specifically
# rather than reused from 25/26.
CLUBS = {
    1:"Arsenal",    2:"Aston Villa",    3:"Bournemouth",   4:"Brentford",
    5:"Brighton",   6:"Chelsea",        7:"Coventry City", 8:"Crystal Palace",
    9:"Everton",   10:"Fulham",        11:"Hull City",     12:"Ipswich Town",
   13:"Leeds",     14:"Liverpool",     15:"Man City",       16:"Man Utd",
   17:"Newcastle", 18:"Nott'm Forest", 19:"Spurs",          20:"Sunderland",
}

# ── Network ───────────────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 3, delay: float = 1.5) -> dict:
    """Fetch a URL and return parsed JSON, with retry on transient errors."""
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise SystemExit(
                    f"\n x 404 Not Found: {url}\n  Check manager ID and gameweek."
                ) from e
            if attempt == retries:
                raise SystemExit(
                    f"\n x HTTP {e.code} after {retries} attempts: {url}"
                ) from e
            print(f"  ! HTTP {e.code}, retrying ({attempt}/{retries})...")
            time.sleep(delay)
        except Exception as e:
            if attempt == retries:
                raise SystemExit(f"\n x Network error: {e}") from e
            print(f"  ! {e}, retrying ({attempt}/{retries})...")
            time.sleep(delay)


# ── Concurrency ────────────────────────────────────────────────────────────────
# The FPL API has no meaningful rate limit for this kind of read traffic, and
# every fetch() call above is pure I/O wait (DNS + TLS + response latency).
# Firing independent requests concurrently instead of in a for-loop is the
# single biggest lever we have — an 8-manager team fetched one-at-a-time at
# ~300-500ms/request takes 2.5-4s; fetched in parallel it takes ~one request's
# worth of wall-clock time. This is safe here because none of the fetch
# targets share mutable state and the FPL API doesn't require session/cookie
# affinity between requests.

MAX_WORKERS = 12  # generous — these are short-lived GET requests, not heavy jobs


def parallel_map(fn, items: list, max_workers: int = MAX_WORKERS) -> list:
    """
    Apply fn to each item concurrently, preserving input order in the output.
    If fn raises for any item, that exception propagates after all submitted
    work has completed (same failure semantics as a plain for-loop, just faster).
    """
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(fn, items))


# ── FPL data fetchers ─────────────────────────────────────────────────────────

def get_bootstrap() -> dict:
    print("-> Fetching player data from FPL...", flush=True)
    data = fetch(f"{BASE}/bootstrap-static/")
    print(f"   {len(data['elements'])} players loaded")
    return data


def get_picks(manager_id: str, gw: int) -> dict:
    return fetch(f"{BASE}/entry/{manager_id}/event/{gw}/picks/")


# ── Chip half-season windows ─────────────────────────────────────────────────
# Every chip is available ONCE per half-season:
#   First half:  GW1  – GW19
#   Second half: GW20 – GW38
# This applies to ALL chips: Wildcard, Triple Captain, Bench Boost, Free Hit.
CHIP_H1_GWS = range(1,  20)   # GW1  – GW19
CHIP_H2_GWS = range(20, 39)   # GW20 – GW38
ALL_CHIPS    = ["wildcard", "3xc", "bboost", "freehit"]


def get_manager_season_history(manager_id: str) -> dict:
    """
    Fetch a manager's full season history in one request.
    Returns:
      {
        "chips":   [{"name": str, "event": int, "half": int}, ...],
        "gw_scores": {gw: {"points": int, "total_points": int, "rank": int}},
      }
    """
    try:
        data    = fetch(f"{BASE}/entry/{manager_id}/history/")
        # Chips
        chips   = []
        for c in data.get("chips", []):
            name  = c["name"].lower()
            event = c.get("event", 0)
            half  = 1 if event in CHIP_H1_GWS else 2
            chips.append({"name": name, "event": event, "half": half})
        # GW-by-GW scores
        gw_scores = {}
        for gw_entry in data.get("current", []):
            gw = gw_entry.get("event")
            if gw:
                gw_scores[gw] = {
                    "points":       gw_entry.get("points", 0),
                    "total_points": gw_entry.get("total_points", 0),
                    "rank":         gw_entry.get("rank"),
                }
        return {"chips": chips, "gw_scores": gw_scores}
    except Exception:
        return {"chips": [], "gw_scores": {}}


def get_all_season_histories(manager_ids: List[str]) -> Dict[str, dict]:
    """Return { manager_id: season_history_dict } for all managers.
    Fetched concurrently — each manager's history is an independent request."""
    histories = parallel_map(get_manager_season_history, manager_ids)
    return dict(zip(manager_ids, histories))


def chips_used_in_half(chip_history: List[dict], chip_name: str, half: int) -> bool:
    """Return True if the given chip was used in the given half-season."""
    return any(c["name"] == chip_name and c["half"] == half for c in chip_history)


def get_remaining_chips(chip_history: List[dict], current_gw: int) -> Dict[str, Dict[str, bool]]:
    """
    Return availability for each chip in each half.
    Structure: { chip_name: {"h1": bool_available, "h2": bool_available} }
    A chip is available in a half if it hasn't been played in that half yet.
    current_gw tells us which half we are currently in.
    """
    result = {}
    for chip in ALL_CHIPS:
        h1_used = chips_used_in_half(chip_history, chip, 1)
        h2_used = chips_used_in_half(chip_history, chip, 2)
        result[chip] = {
            "h1": not h1_used,
            "h2": not h2_used,
        }
    return result


def get_live_scores(gw: int) -> tuple:
    """
    Fetch live GW scores and minutes for all players.
    Returns:
        scores  : { player_id: total_points_this_gw }
        minutes : { player_id: minutes_played_this_gw }
        explain : { player_id: [ {"fixture": int, "stat": str, "points": int, "value": int} ] }
    These are RAW scores (before FPL captain doubling — we apply our own logic).
    """
    print(f"-> Fetching live GW{gw} scores...", flush=True)
    data = fetch(f"{BASE}/event/{gw}/live/")
    scores:  Dict[int, int] = {}
    minutes: Dict[int, int] = {}
    explain: Dict[int, list] = {}
    for el in data.get("elements", []):
        pid   = el["id"]
        stats = el.get("stats", {})
        scores[pid]  = stats.get("total_points", 0)
        minutes[pid] = stats.get("minutes", 0)
        # Flatten explain blocks: each fixture has a list of stat → points entries
        events = []
        for fix_block in el.get("explain", []):
            fixture_id = fix_block.get("fixture")
            for stat_entry in fix_block.get("stats", []):
                pts = stat_entry.get("points", 0)
                if pts != 0:
                    events.append({
                        "fixture":  fixture_id,
                        "stat":     stat_entry.get("identifier", ""),
                        "points":   pts,
                        "value":    stat_entry.get("value", 0),
                    })
        explain[pid] = events
    print(f"   Live scores loaded for {len(scores)} players")
    return scores, minutes, explain


# ── Data processing ───────────────────────────────────────────────────────────

def build_player_map(bootstrap: dict) -> Dict[int, dict]:
    pmap = {}
    for el in bootstrap["elements"]:
        pmap[el["id"]] = {
            "name":     el["web_name"],
            "full":     f"{el['first_name']} {el['second_name']}",
            "position": POSITIONS.get(el["element_type"], "?"),
            "club":     CLUBS.get(el["team"], f"T{el['team']}"),
        }
    return pmap


def resolve_effective_captain(picks: List[dict], active_chip: Optional[str]) -> dict:
    """
    Return the pick that should be treated as effective captain this GW.

    Rule: if the named FPL captain is on the bench (position 12-15) AND the
    Bench Boost chip is NOT active, the vice captain takes the armband instead.

    Bench Boost exception: when bboost is active, bench players score normally
    so the named captain stays as captain regardless of their position.
    """
    cap  = next((p for p in picks if p["is_captain"]),      None)
    vice = next((p for p in picks if p["is_vice_captain"]), None)

    if cap is None:
        return vice  # safe fallback

    cap_on_bench = cap["position"] > 11
    if cap_on_bench and active_chip != "bboost" and vice is not None:
        return vice

    return cap


def fetch_team_picks(
    manager_ids:  List[str],
    cap_index:    int,
    gw:           int,
    team_label:   str,
    player_map:   Dict[int, dict],
    managers:     Optional[List[dict]] = None,
) -> List[dict]:
    """
    Returns a list of pick-metadata dicts, one per manager.
    Each pick: { element, is_captain, is_vice_captain, count, position, multiplier }

    count logic (based on effective captain after VC auto-sub resolution):
      H2H cap manager AND effective FPL cap player  -> 4
      H2H cap manager OR  effective FPL cap player  -> 2
      neither                                       -> 1

    transfer_hit: negative penalty for excess transfers (e.g. -4).
    active_chip:  chip name if used this GW, else None.
    """
    # Fetch every manager's picks concurrently — these are independent GETs,
    # so there's no reason to pay N sequential round-trips for an N-manager team.
    for i, mid in enumerate(manager_ids):
        is_cap_manager = (i == cap_index)
        mgr_info  = managers[i] if managers and i < len(managers) else {}
        mgr_label = mgr_info.get("manager") or mgr_info.get("name") or mid
        print(
            f"  -> Team {team_label} M{i+1} ({mgr_label})"
            f"{' [H2H CAPTAIN]' if is_cap_manager else ''}...",
            flush=True,
        )
    picks_data = parallel_map(lambda mid: get_picks(mid, gw), manager_ids)

    all_picks = []
    for i, mid in enumerate(manager_ids):
        is_cap_manager = (i == cap_index)
        mgr_info   = managers[i] if managers and i < len(managers) else {}
        mgr_label  = mgr_info.get("manager") or mgr_info.get("name") or mid

        data = picks_data[i]
        raw  = data.get("picks", [])

        # Transfer hit
        entry_history = data.get("entry_history", {})
        _hit_cost     = entry_history.get("event_transfers_cost", 0)
        transfer_hit  = -abs(_hit_cost) if _hit_cost else 0

        # Active chip
        active_chip = data.get("active_chip", None)

        # Build raw picks (count assigned after VC auto-sub resolution below)
        picks = []
        for p in raw:
            picks.append({
                "element":         p["element"],
                "is_captain":      p.get("is_captain", False),
                "is_vice_captain": p.get("is_vice_captain", False),
                "count":           1,          # placeholder — set below
                "position":        p.get("position", 0),
                "multiplier":      p.get("multiplier", 1),
            })

        # Resolve effective captain (VC takes over if named captain is on the bench,
        # unless Bench Boost is active)
        eff_cap    = resolve_effective_captain(picks, active_chip)
        eff_cap_id = eff_cap["element"] if eff_cap else None

        # Assign counts using effective captain and chip multiplier
        cap_mul = fpl_cap_multiplier(active_chip)  # 3 for TC, 2 otherwise
        for p in picks:
            is_eff_cap = (p["element"] == eff_cap_id)
            if is_cap_manager and is_eff_cap:
                p["count"] = cap_mul * 2          # e.g. 3×2=6 for TC+H2H, 2×2=4 normal
            elif is_eff_cap:
                p["count"] = cap_mul              # e.g. 3 for TC alone, 2 normal
            elif is_cap_manager:
                p["count"] = 2
            else:
                p["count"] = 1

        # Build display info
        named_cap  = next((p for p in picks if p["is_captain"]), None)
        named_name = player_map.get(named_cap["element"], {}).get("name", "?") if named_cap else "?"
        eff_name   = player_map.get(eff_cap_id, {}).get("name", "?") if eff_cap_id else "?"

        if eff_cap_id != (named_cap["element"] if named_cap else None):
            cap_info = f" (c→vc) {named_name} {RED}[benched]{RESET} → {GREEN}{eff_name}{RESET}"
        elif cap_mul == 3:
            cap_info = f" {BOLD}{YELLOW}(c3x){RESET} {eff_name}"
        else:
            cap_info = f" (c) {eff_name}"

        hit_info  = f"  {RED}hit: {transfer_hit}{RESET}" if transfer_hit < 0 else ""
        chip_info = f"  {MAGENTA}[{active_chip.upper()}]{RESET}" if active_chip else ""
        print(f"     {len(picks)} picks{cap_info}{hit_info}{chip_info}")

        all_picks.append({
            "picks":          picks,
            "transfer_hit":   transfer_hit,
            "active_chip":    active_chip,
            "manager_id":     mid,
            "eff_cap_id":     eff_cap_id,
            "cap_mul":        cap_mul,       # 3 for TC, 2 otherwise
        })

    return all_picks


def build_differential(
    picks_a:     List[dict],
    picks_b:     List[dict],
    player_map:  Dict[int, dict],
    live_scores: Dict[int, int],
) -> List[dict]:
    """
    Aggregate counts, compute diff and point_swing for every player.

    Bench players are excluded from the swing calculation UNLESS the manager
    activated the Bench Boost chip (active_chip == 'bboost').

    point_swing = diff x live_pts
      +ve -> Team A benefited from this player vs Team B
      -ve -> Team B benefited
    """
    count: Dict[int, Dict[str, int]] = {}

    def is_playing(pick: dict, active_chip: Optional[str]) -> bool:
        """A pick counts toward swing if it's a starter OR bench boost is active."""
        if active_chip == "bboost":
            return True
        return pick["position"] <= 11  # positions 1-11 are starters

    for mgr in picks_a:
        chip  = mgr["active_chip"]
        for p in mgr["picks"]:
            if not is_playing(p, chip):
                continue
            count.setdefault(p["element"], {"A": 0, "B": 0})
            count[p["element"]]["A"] += p["count"]

    for mgr in picks_b:
        chip  = mgr["active_chip"]
        for p in mgr["picks"]:
            if not is_playing(p, chip):
                continue
            count.setdefault(p["element"], {"A": 0, "B": 0})
            count[p["element"]]["B"] += p["count"]

    rows = []
    for el_id, c in count.items():
        pl       = player_map.get(el_id, {"name": f"#{el_id}", "position": "?", "club": "?"})
        diff     = c["A"] - c["B"]
        live_pts = live_scores.get(el_id, 0)
        swing    = diff * live_pts

        rows.append({
            "id":          el_id,
            "name":        pl["name"],
            "position":    pl["position"],
            "club":        pl["club"],
            "A":           c["A"],
            "B":           c["B"],
            "diff":        diff,
            "live_pts":    live_pts,
            "point_swing": swing,
        })

    # Sort: biggest absolute swing first, then biggest abs diff
    rows.sort(key=lambda r: (-abs(r["point_swing"]), -abs(r["diff"])))
    return rows


# ── ANSI helpers ──────────────────────────────────────────────────────────────

GREEN   = "\033[92m"
CYAN    = "\033[96m"
RED     = "\033[91m"
YELLOW  = "\033[93m"
MAGENTA = "\033[95m"
GREY    = "\033[90m"
BOLD    = "\033[1m"
RESET   = "\033[0m"
DIM     = "\033[2m"

POS_COLOR = {"GKP": YELLOW, "DEF": CYAN, "MID": GREEN, "FWD": RED}


def fpl_cap_multiplier(active_chip: Optional[str]) -> int:
    """Return the FPL captain score multiplier for a given chip.
    Triple Captain chip (3xc) -> 3x, everything else -> 2x.
    """
    return 3 if active_chip == "3xc" else 2


def col(text: str, width: int, align: str = "left") -> str:
    s = str(text)
    if align == "right":  return s.rjust(width)
    if align == "center": return s.center(width)
    return s.ljust(width)


def apad(visible: str, formatted: str, width: int, align: str = "center") -> str:
    """Pad an ANSI-formatted string to `width` based on visible text length."""
    pad = max(0, width - len(visible))
    if align == "center":
        l = pad // 2
        return " " * l + formatted + " " * (pad - l)
    if align == "right":
        return " " * pad + formatted
    return formatted + " " * pad


def fmt_signed(value: int, pos_color: str, neg_color: str) -> tuple:
    """Return (visible_str, formatted_str) for a signed integer."""
    if value > 0:
        v = f"+{value}"
        return v, f"{pos_color}{v}{RESET}"
    elif value < 0:
        v = str(value)
        return v, f"{neg_color}{v}{RESET}"
    else:
        return "0", f"{GREY}0{RESET}"


def fmt_pts(pts: int) -> tuple:
    """Return (visible, formatted) for a player's GW points."""
    v = str(pts)
    if pts >= 12:
        return v, f"{BOLD}{YELLOW}{pts}{RESET}"
    elif pts >= 6:
        return v, f"{YELLOW}{pts}{RESET}"
    elif pts == 0:
        return v, f"{GREY}{pts}{RESET}"
    return v, v


# ── Rendering ─────────────────────────────────────────────────────────────────

def print_team_summary(
    label:       str,
    manager_ids: List[str],
    cap_index:   int,
    picks_list:  List[dict],
    player_map:  Dict[int, dict],
    live_scores: Dict[int, int],
    color:       str,
    managers:    Optional[List[dict]] = None,
) -> None:
    print(f"\n{BOLD}{color}{'─'*72}{RESET}")
    print(f"{BOLD}{color}  TEAM {label}{RESET}")
    print(f"{color}{'─'*72}{RESET}")

    for i, (mid, mgr) in enumerate(zip(manager_ids, picks_list)):
        picks       = mgr["picks"]
        transfer_hit = mgr["transfer_hit"]
        active_chip  = mgr["active_chip"]

        mgr_info  = managers[i] if managers and i < len(managers) else {}
        mgr_label = mgr_info.get("manager") or mgr_info.get("name") or mid

        cap_flag  = f" {GREEN}[H2H CAPTAIN]{RESET}" if i == cap_index else ""
        chip_flag = f"  {MAGENTA}[{active_chip.upper()}]{RESET}" if active_chip else ""

        print(f"\n  {BOLD}Manager {i+1}{RESET}  ({mgr_label}){cap_flag}{chip_flag}")

        starters = [p for p in picks if p["position"] <= 11]
        bench    = [p for p in picks if p["position"] > 11]
        eff_cap_id = mgr.get("eff_cap_id")
        cap_mul    = mgr.get("cap_mul", fpl_cap_multiplier(active_chip))

        # ── Starters ──────────────────────────────────────────────────────────
        ordered = sorted(starters, key=lambda p: (
            -(p["element"] == eff_cap_id),
            -p["is_vice_captain"]
        ))
        for p in ordered:
            pl   = player_map.get(p["element"], {"name": f"#{p['element']}", "position": "?", "club": "?"})
            pc   = POS_COLOR.get(pl.get("position", ""), "")
            pts  = live_scores.get(p["element"], 0)
            cnt  = p["count"]
            _, pts_fmt = fmt_pts(pts)
            mul_str = f"{MAGENTA}x{cnt}{RESET}" if cnt > 1 else f"{GREY}x{cnt}{RESET}"

            is_named_cap = p["is_captain"]
            is_eff_cap   = (p["element"] == eff_cap_id)
            is_vice      = p["is_vice_captain"]

            if is_named_cap and is_eff_cap:
                badge = f" {GREEN}(c){RESET}" if cap_mul == 2 else f" {BOLD}{YELLOW}(c3x){RESET}"
            elif is_eff_cap and not is_named_cap:
                # VC auto-subbed to captain
                badge = f" {YELLOW}(c*){RESET}"
            elif is_named_cap and not is_eff_cap:
                # Named captain but played 0 mins — shown as strikethrough captain
                badge = f" {RED}(c✗){RESET}"
            elif is_vice:
                badge = f" {CYAN}(v){RESET}"
            else:
                badge = "    "

            print(
                f"    {pc}{pl.get('position','?'):3}{RESET}  "
                f"{col(pl['name'], 22)}{badge}  "
                f"{pts_fmt:>3} pts  {mul_str}"
            )

        # ── Bench ─────────────────────────────────────────────────────────────
        bench_ordered = sorted(bench, key=lambda p: p["position"])
        if bench_ordered:
            print(f"    {GREY}── bench ──────────────────────────────────{RESET}")
            for p in bench_ordered:
                pl  = player_map.get(p["element"], {"name": f"#{p['element']}", "position": "?", "club": "?"})
                pc  = POS_COLOR.get(pl.get("position", ""), "")
                pts = live_scores.get(p["element"], 0)
                _, pts_fmt = fmt_pts(pts)
                bench_pts_note = f"{GREY}(bench){RESET}" if active_chip != "bboost" else f"{MAGENTA}(bboost){RESET}"
                print(
                    f"    {pc}{pl.get('position','?'):3}{RESET}  "
                    f"{col(pl['name'], 22)}     "
                    f"{pts_fmt:>3} pts  {GREY}x1{RESET}  {bench_pts_note}"
                )

        # ── Manager score summary ─────────────────────────────────────────────
        raw_score = 0
        for p in starters:
            pts = live_scores.get(p["element"], 0)
            raw_score += pts * cap_mul if p["element"] == eff_cap_id else pts
        if active_chip == "bboost":
            for p in bench:
                raw_score += live_scores.get(p["element"], 0)

        final_score = raw_score + transfer_hit  # hit is already negative (e.g. -4)

        if transfer_hit < 0:
            hit_str = f"{RED}({transfer_hit}){RESET}"
            score_str = (
                f"{BOLD}{raw_score}{RESET} "
                f"{hit_str} = "
                f"{BOLD}{YELLOW}{final_score}{RESET} pts"
            )
        else:
            score_str = (
                f"{BOLD}{raw_score}{RESET} "
                f"{GREY}(0){RESET} = "
                f"{BOLD}{YELLOW}{final_score}{RESET} pts"
            )

        print(f"\n    {BOLD}Score:{RESET} {score_str}")


def print_differential_table(rows: List[dict], gw: int) -> None:
    W = 84
    print(f"\n{BOLD}{'='*W}{RESET}")
    print(f"{BOLD}  OWNERSHIP DIFFERENTIAL & POINT SWING  --  GW{gw}{RESET}")
    print(f"{'='*W}{RESET}")

    # col widths: Player=18 Pos=5 Club=14 A=7 B=7 Diff=7 Pts=7 Swing=9
    hdr = (
        f"  {col('Player', 18)}"
        f"{col('Pos', 5)}"
        f"{col('Club', 14)}"
        f"{col('A', 7, 'center')}"
        f"{col('B', 7, 'center')}"
        f"{col('Diff', 7, 'center')}"
        f"{col('GW Pts', 7, 'center')}"
        f"{col('Swing', 9, 'center')}"
    )
    print(f"{BOLD}{GREY}{hdr}{RESET}")
    print(f"{GREY}  {'─'*W}{RESET}")

    for row in rows:
        pc = POS_COLOR.get(row["position"], "")

        diff_v, diff_f = fmt_signed(row["diff"],        GREEN, RED)
        sw_v,   sw_f   = fmt_signed(row["point_swing"], GREEN, RED)
        pts_v,  pts_f  = fmt_pts(row["live_pts"])

        line = (
            f"  {col(row['name'], 18)}"
            f"{pc}{col(row['position'], 5)}{RESET}"
            f"{GREY}{col(row['club'], 14)}{RESET}"
            f"{apad(str(row['A']), f'{GREEN}{row[chr(65)]}{RESET}', 7)}"
            f"{apad(str(row['B']), f'{CYAN}{row[chr(66)]}{RESET}', 7)}"
            f"{apad(diff_v, diff_f, 7)}"
            f"{apad(pts_v,  pts_f,  7)}"
            f"{apad(sw_v,   sw_f,   9)}"
        )
        print(line)

    print(f"{GREY}  {'─'*W}{RESET}")
    print(
        f"\n{DIM}  Count: H2H cap + FPL cap=x4 | either alone=x2 | normal=x1"
        f"  |  Swing = diff x GW pts\n"
        f"  {GREEN}+ve swing{RESET}{DIM} = Team A pts advantage  "
        f"  {RED}-ve swing{RESET}{DIM} = Team B pts advantage{RESET}\n"
    )


def print_swing_summary(rows: List[dict], picks_a: List[dict], picks_b: List[dict], live_scores: Dict[int, int]) -> None:
    a_rows = [r for r in rows if r["point_swing"] > 0]
    b_rows = [r for r in rows if r["point_swing"] < 0]
    blanks = [r for r in rows if r["point_swing"] == 0 and r["diff"] != 0]
    player_net = sum(r["point_swing"] for r in rows)

    # Total transfer hits per team (already stored as negative, e.g. -4)
    hit_a = sum(mgr["transfer_hit"] for mgr in picks_a)
    hit_b = sum(mgr["transfer_hit"] for mgr in picks_b)
    # Net hit swing from Team A's perspective: A's hit hurts A, B's hit hurts B
    hit_swing = hit_a - hit_b   # e.g. A took -8, B took -4 → -8 - (-4) = -4 (bad for A)
    net_h2h   = player_net + hit_swing

    print(f"{BOLD}  SWING SUMMARY{RESET}")

    if a_rows:
        parts = ", ".join(
            f"{r['name']} ({GREEN}+{r['point_swing']}{RESET} | {r['live_pts']}pts)"
            for r in a_rows
        )
        print(f"  {GREEN}Team A benefited from:{RESET} {parts}")
    else:
        print(f"  {GREEN}Team A benefited from:{RESET} none")

    if b_rows:
        parts = ", ".join(
            f"{r['name']} ({RED}{r['point_swing']}{RESET} | {r['live_pts']}pts)"
            for r in b_rows
        )
        print(f"  {CYAN}Team B benefited from:{RESET} {parts}")
    else:
        print(f"  {CYAN}Team B benefited from:{RESET} none")

    if blanks:
        print(f"  {GREY}Differential but blanked:{RESET} " + ", ".join(r["name"] for r in blanks))

    # ── Line 1: pure player points swing ──────────────────────────────────────
    print()
    if player_net > 0:
        swing_str = f"{GREEN}Team A  +{player_net} pts{RESET}"
    elif player_net < 0:
        swing_str = f"{CYAN}Team B  +{abs(player_net)} pts{RESET}"
    else:
        swing_str = f"{GREY}0 pts — balanced{RESET}"
    print(f"  {BOLD}Net Player Points Swing this GW:{RESET}  {swing_str}")

    # ── Divider ────────────────────────────────────────────────────────────────
    # Build a hit breakdown note if either team took hits
    hit_parts = []
    if hit_a < 0:
        hit_parts.append(f"Team A hit {RED}{hit_a}{RESET}")
    if hit_b < 0:
        hit_parts.append(f"Team B hit {RED}{hit_b}{RESET}")
    hit_note = f"  {GREY}({', '.join(hit_parts)}){RESET}" if hit_parts else ""
    print(f"\n  {GREY}{'·'*60}{RESET}{hit_note}")

    # ── Line 2: net H2H swing including hits ──────────────────────────────────
    if net_h2h > 0:
        net_str = f"{GREEN}Team A  +{net_h2h} pts{RESET}"
    elif net_h2h < 0:
        net_str = f"{CYAN}Team B  +{abs(net_h2h)} pts{RESET}"
    else:
        net_str = f"{GREY}0 pts — level{RESET}"
    print(f"  {BOLD}Net H2H Swing (incl. hits):{RESET}       {net_str}")
    print()



# ── Visual widgets ────────────────────────────────────────────────────────────

def hbar(value: int, max_val: int, width: int = 20, fill: str = "█", empty: str = "░") -> str:
    """Render a horizontal bar scaled to max_val."""
    if max_val == 0:
        return empty * width
    filled = round(width * min(abs(value), max_val) / max_val)
    return fill * filled + empty * (width - filled)


def print_h2h_scoreboard(
    total_a: int,
    total_b: int,
    league_name_a: str,
    league_name_b: str,
    gw: int,
) -> None:
    W   = 80
    sep = "═" * W
    print(f"\n{BOLD}{YELLOW}{sep}{RESET}")
    print(f"{BOLD}{YELLOW}  ⚽  FPL H2H MATCH REPORT  —  GW{gw}{RESET}")
    print(f"{BOLD}{YELLOW}{sep}{RESET}")

    name_a = league_name_a[:28]
    name_b = league_name_b[:28]

    score_a = f"{BOLD}{GREEN}{total_a:>5}{RESET}"
    score_b = f"{BOLD}{CYAN}{total_b:<5}{RESET}"
    vs      = f"{BOLD}{GREY}  vs  {RESET}"
    print(f"\n  {GREEN}{name_a:<30}{RESET}{score_a}{vs}{score_b}{CYAN}{name_b}{RESET}")

    BAR_W = 44
    total = total_a + total_b
    a_fill = round(BAR_W * total_a / total) if total else BAR_W // 2
    b_fill = BAR_W - a_fill
    bar_a  = f"{GREEN}{'█' * a_fill}{RESET}"
    bar_b  = f"{CYAN}{'█' * b_fill}{RESET}"
    pct_a  = round(100 * total_a / total) if total else 50
    pct_b  = 100 - pct_a
    print(f"\n  {GREEN}{pct_a}%{RESET} {bar_a}{bar_b} {CYAN}{pct_b}%{RESET}\n")

    diff = total_a - total_b
    if diff > 0:
        banner = f"{BOLD}{GREEN}  ▲ {name_a} lead by +{diff} pts{RESET}"
    elif diff < 0:
        banner = f"{BOLD}{CYAN}  ▲ {name_b} lead by +{abs(diff)} pts{RESET}"
    else:
        banner = f"{BOLD}{YELLOW}  ══ Scores level — {total_a} pts each{RESET}"
    print(banner)
    print(f"  {GREY}{sep}{RESET}\n")


def print_points_race(
    team_a_ids:    List[str],
    team_b_ids:    List[str],
    managers_a:    List[dict],
    managers_b:    List[dict],
    histories_a:   Dict[str, dict],
    histories_b:   Dict[str, dict],
    cap_a_idx:     int,
    cap_b_idx:     int,
    current_gw:    int,
    league_name_a: str = "Team A",
    league_name_b: str = "Team B",
) -> None:
    N        = 8
    PLOT_H   = 12
    show_gws = list(range(max(1, current_gw - N + 1), current_gw + 1))

    def team_cumulative(ids, histories, cap_idx):
        scores_per_gw = []
        for gw in show_gws:
            gw_total = 0
            for i, mid in enumerate(ids):
                pts = histories.get(mid, {}).get("gw_scores", {}).get(gw, {}).get("points", 0) or 0
                gw_total += pts * 2 if i == cap_idx else pts
            scores_per_gw.append(gw_total)
        cum, running = [], 0
        for s in scores_per_gw:
            running += s
            cum.append(running)
        return cum

    cum_a   = team_cumulative(team_a_ids, histories_a, cap_a_idx)
    cum_b   = team_cumulative(team_b_ids, histories_b, cap_b_idx)
    max_pts = max(max(cum_a, default=1), max(cum_b, default=1), 1)
    range_  = max_pts or 1
    n_gws   = len(show_gws)
    col_w   = max(5, 56 // n_gws)

    W = 80
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(f"{BOLD}  📈  CUMULATIVE POINTS RACE  —  Last {n_gws} GWs{RESET}")
    print(f"  {GREEN}{league_name_a[:24]}{GREY}  vs  {CYAN}{league_name_b[:24]}{RESET}")
    print(f"{'─'*W}{RESET}")

    grid = [[" " for _ in range(n_gws)] for _ in range(PLOT_H)]

    def to_row(pts):
        frac = pts / range_
        row  = PLOT_H - 1 - round(frac * (PLOT_H - 1))
        return max(0, min(PLOT_H - 1, row))

    for gi in range(n_gws):
        ra = to_row(cum_a[gi])
        rb = to_row(cum_b[gi])
        if ra == rb:
            grid[ra][gi] = "X"
        else:
            grid[ra][gi] = "A"
            grid[rb][gi] = "B"

    gw_header = "".join(f"{'GW'+str(g):^{col_w}}" for g in show_gws)
    print(f"       {GREY}{gw_header}{RESET}")

    for row in range(PLOT_H):
        y_val = int(max_pts * (1 - row / (PLOT_H - 1)))
        line  = ""
        for gi in range(n_gws):
            cell = grid[row][gi]
            pad  = " " * (col_w - 2)
            if cell == "A":
                line += f"{GREEN}●{RESET} {pad}"
            elif cell == "B":
                line += f"{CYAN}●{RESET} {pad}"
            elif cell == "X":
                line += f"{YELLOW}◈{RESET} {pad}"
            else:
                line += f"{GREY}·{RESET} {pad}"
        print(f"  {GREY}{y_val:>5}{RESET} │ {line}")

    print(f"  {GREY}       └{'─'*(col_w * n_gws + 1)}{RESET}")

    final_a, final_b = (cum_a[-1] if cum_a else 0), (cum_b[-1] if cum_b else 0)
    bar_max = max(final_a, final_b, 1)
    BAR = 30
    bar_a_w = round(BAR * final_a / bar_max)
    bar_b_w = round(BAR * final_b / bar_max)
    print(f"\n  {GREEN}{'█'*bar_a_w}{'░'*(BAR-bar_a_w)}{RESET}  {GREEN}{league_name_a[:20]}{RESET}  {BOLD}{GREEN}{final_a}{RESET} pts")
    print(f"  {CYAN}{'█'*bar_b_w}{'░'*(BAR-bar_b_w)}{RESET}  {CYAN}{league_name_b[:20]}{RESET}  {BOLD}{CYAN}{final_b}{RESET} pts")
    lead = final_a - final_b
    if lead > 0:
        print(f"\n  {GREEN}▲ {league_name_a[:24]} lead by {lead} pts over this window{RESET}\n")
    elif lead < 0:
        print(f"\n  {CYAN}▲ {league_name_b[:24]} lead by {abs(lead)} pts over this window{RESET}\n")
    else:
        print(f"\n  {YELLOW}══ Dead level over this window{RESET}\n")


def print_swing_bar_chart(rows: List[dict], gw: int, top_n: int = 10) -> None:
    BAR_HALF = 22
    swing_rows = [r for r in rows if r["point_swing"] != 0][:top_n]
    if not swing_rows:
        return
    max_abs = max(abs(r["point_swing"]) for r in swing_rows) or 1
    W = 80
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(f"{BOLD}  📊  TOP SWING PLAYERS  —  GW{gw}{RESET}")
    print(f"{'─'*W}{RESET}")
    print(f"  {GREY}{'Team B ◄':>{BAR_HALF+2}}  {'Player':<18}  {'► Team A':<{BAR_HALF}}{RESET}")
    print(f"  {GREY}{'─'*(BAR_HALF*2 + 24)}{RESET}")
    for r in swing_rows:
        sw      = r["point_swing"]
        name    = r["name"][:16]
        pts     = r["live_pts"]
        pc      = POS_COLOR.get(r.get("position", ""), "")
        bar_len = round(BAR_HALF * abs(sw) / max_abs)
        if sw > 0:
            left  = " " * BAR_HALF
            right = f"{GREEN}{'█'*bar_len}{'░'*(BAR_HALF-bar_len)}{RESET}"
            swing_lbl = f"{GREEN}+{sw}{RESET}"
        else:
            left  = f"{CYAN}{'░'*(BAR_HALF-bar_len)}{'█'*bar_len}{RESET}"
            right = " " * BAR_HALF
            swing_lbl = f"{CYAN}{sw}{RESET}"
        print(f"  {left}  {pc}{name:<16}{RESET}  {right}  {swing_lbl} ({pts}pts)")
    print(f"  {GREY}{'─'*(BAR_HALF*2 + 24)}{RESET}")
    print(f"  {DIM}Bar = swing magnitude  |  pts = raw GW score{RESET}\n")


def print_position_breakdown(
    picks_a:       List[dict],
    picks_b:       List[dict],
    player_map:    Dict[int, dict],
    live_scores:   Dict[int, int],
    league_name_a: str = "Team A",
    league_name_b: str = "Team B",
) -> None:
    POS_ORDER = ["GKP", "DEF", "MID", "FWD"]
    BAR = 20

    def pos_pts(picks_list):
        totals = {p: 0 for p in POS_ORDER}
        for mgr in picks_list:
            chip = mgr["active_chip"]
            for p in mgr["picks"]:
                if p["position"] > 11 and chip != "bboost":
                    continue
                pl  = player_map.get(p["element"], {})
                pos = pl.get("position", "?")
                if pos in totals:
                    totals[pos] += live_scores.get(p["element"], 0) * p["count"]
        return totals

    pts_a   = pos_pts(picks_a)
    pts_b   = pos_pts(picks_b)
    total_a = sum(pts_a.values()) or 1
    total_b = sum(pts_b.values()) or 1
    max_any = max(max(pts_a.values(), default=0), max(pts_b.values(), default=0), 1)
    W = 80
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(f"{BOLD}  🔢  POINTS BY POSITION{RESET}")
    print(f"{'─'*W}{RESET}")
    print(f"  {GREY}{'Pos':<5}  {GREEN}{league_name_a[:18]:<22}{GREY}  {CYAN}{league_name_b[:18]:<20}{RESET}")
    print(f"  {GREY}{'─'*72}{RESET}")
    for pos in POS_ORDER:
        pa  = pts_a[pos]
        pb  = pts_b[pos]
        pc  = POS_COLOR.get(pos, "")
        wa  = round(BAR * pa / max_any)
        wb  = round(BAR * pb / max_any)
        pct_a = round(100 * pa / total_a)
        pct_b = round(100 * pb / total_b)
        bar_a = f"{GREEN}{'█'*wa}{'░'*(BAR-wa)}{RESET}"
        bar_b = f"{CYAN}{'█'*wb}{'░'*(BAR-wb)}{RESET}"
        print(f"  {pc}{pos:<5}{RESET}  {bar_a} {GREEN}{pa:>4}pts ({pct_a:>2}%){RESET}  {bar_b} {CYAN}{pb:>4}pts ({pct_b:>2}%){RESET}")
    print(f"  {GREY}{'─'*72}{RESET}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def manager_final_score(mgr: dict, live_scores: Dict[int, int]) -> tuple:
    """Return (raw_score, transfer_hit, final_score) for a manager.
    Uses the effective captain and correct chip multiplier (3x for TC, 2x otherwise).
    """
    picks        = mgr["picks"]
    transfer_hit = mgr["transfer_hit"]
    active_chip  = mgr["active_chip"]
    eff_cap_id   = mgr.get("eff_cap_id")
    cap_mul      = mgr.get("cap_mul", fpl_cap_multiplier(active_chip))
    raw_score = 0
    for p in picks:
        if p["position"] <= 11 or active_chip == "bboost":
            pts = live_scores.get(p["element"], 0)
            raw_score += pts * cap_mul if p["element"] == eff_cap_id else pts
    return raw_score, transfer_hit, raw_score + transfer_hit


def print_team_score_totals(
    picks_a:     List[dict],
    picks_b:     List[dict],
    live_scores: Dict[int, int],
    cap_a_idx:   int,
    cap_b_idx:   int,
    managers_a:  Optional[List[dict]] = None,
    managers_b:  Optional[List[dict]] = None,
) -> None:
    """Print team score totals. H2H captain manager's score is doubled in the team total."""
    print(f"\n{BOLD}{'─'*72}{RESET}")
    print(f"{BOLD}  TEAM SCORE TOTALS{RESET}")
    print(f"{'─'*72}{RESET}")

    total_a = total_b = 0
    for label, picks_list, cap_idx, color, managers in (
        ("A", picks_a, cap_a_idx, GREEN, managers_a),
        ("B", picks_b, cap_b_idx, CYAN,  managers_b),
    ):
        team_score = 0
        print(f"\n  {BOLD}{color}Team {label}{RESET}")
        for i, mgr in enumerate(picks_list):
            raw, hit, final = manager_final_score(mgr, live_scores)
            is_h2h_cap   = (i == cap_idx)
            contribution = final * 2 if is_h2h_cap else final
            team_score  += contribution

            mgr_info  = managers[i] if managers and i < len(managers) else {}
            mgr_label = mgr_info.get("manager") or mgr_info.get("name") or mgr["manager_id"]

            hit_str = f"{RED}({hit}){RESET}" if hit < 0 else f"{GREY}(0){RESET}"

            if is_h2h_cap:
                cap_badge  = f"  {MAGENTA}[H2H CAP × 2  →  {contribution} pts to total]{RESET}"
                score_line = (
                    f"{raw} {hit_str} = "
                    f"{BOLD}{YELLOW}{final}{RESET} pts"
                    f"{cap_badge}"
                )
            else:
                score_line = (
                    f"{raw} {hit_str} = "
                    f"{BOLD}{YELLOW}{final}{RESET} pts"
                )
            # Mini contribution bar
            bar_max = max(total_a if label == "B" else 9999, contribution, 1)
            bar_len = min(20, round(20 * contribution / max(contribution, 1)))
            bar_str = f"{color}{'█'*bar_len}{'░'*(20-bar_len)}{RESET}"
            print(f"    Manager {i+1} ({mgr_label}): {score_line}")
            print(f"             {bar_str}  {GREY}contributes {contribution} to team total{RESET}")

        if label == "A":
            total_a = team_score
        else:
            total_b = team_score
        print(f"    {BOLD}Team {label} Total: {YELLOW}{team_score}{RESET}")

    print(f"\n  {'─'*40}")
    diff = total_a - total_b
    if diff > 0:
        print(f"  {BOLD}Team A leads by {GREEN}+{diff}{RESET}{BOLD} pts  ({total_a} vs {total_b}){RESET}")
    elif diff < 0:
        print(f"  {BOLD}Team B leads by {CYAN}+{abs(diff)}{RESET}{BOLD} pts  ({total_b} vs {total_a}){RESET}")
    else:
        print(f"  {BOLD}Scores level — {total_a} pts each{RESET}")
    print()


def print_form_table(
    team_a_ids:  List[str],
    team_b_ids:  List[str],
    managers_a:  List[dict],
    managers_b:  List[dict],
    histories_a: Dict[str, dict],
    histories_b: Dict[str, dict],
    cap_a_idx:   int,
    cap_b_idx:   int,
    current_gw:  int,
) -> None:
    """
    Print a last-5-GW form table for all 8 managers, side by side per team.
    Columns: Manager | GW-4 | GW-3 | GW-2 | GW-1 | GW(current) | Avg | Total
    Scores coloured: >=80 green, 50-79 yellow, <50 red.
    """
    N    = 5
    # Which GWs to show — last 5 up to and including current_gw
    show_gws = list(range(max(1, current_gw - N + 1), current_gw + 1))

    W = 96

    def score_color(pts: Optional[int]) -> str:
        if pts is None: return GREY
        if pts >= 70:   return GREEN
        if pts >= 50:   return YELLOW
        return RED

    def fmt_score(pts: Optional[int], width: int = 7) -> str:
        if pts is None:
            return f"{GREY}{'-':^{width}}{RESET}"
        c = score_color(pts)
        return f"{c}{str(pts):^{width}}{RESET}"

    def bar_chart(scores: List[Optional[int]], width: int = 5) -> str:
        """Mini spark-bar using block chars."""
        BLOCKS = " ▁▂▃▄▅▆▇█"
        max_s  = max((s for s in scores if s is not None), default=1) or 1
        result = ""
        for s in scores:
            if s is None:
                result += f"{GREY}·{RESET}"
            else:
                idx   = min(8, int((s / max_s) * 8))
                color = score_color(s)
                result += f"{color}{BLOCKS[idx]}{RESET}"
        return result

    def render_team_form(
        label:    str,
        team_ids: List[str],
        managers: List[dict],
        histories: Dict[str, dict],
        cap_idx:  int,
        color:    str,
    ) -> None:
        print(f"\n{BOLD}{color}{'─'*W}{RESET}")
        print(f"{BOLD}{color}  TEAM {label} — LAST {N} GAMEWEEKS FORM{RESET}")
        print(f"{color}{'─'*W}{RESET}")

        # Header
        gw_hdrs = "  ".join(f"{'GW'+str(g):^7}" for g in show_gws)
        print(f"  {BOLD}{GREY}{'Manager':<30}  {gw_hdrs}  {'Avg':^7}  {'Trend':^7}  {'Season':^8}{RESET}")
        print(f"  {GREY}{'─'*(W-2)}{RESET}")

        ANSI_RE    = re.compile(r"\033\[[0-9;]*m")
        COL_WIDTH  = 30  # visible characters reserved for the manager label

        def ansi_ljust(s: str, width: int) -> str:
            """Left-justify s to `width` visible characters, ignoring ANSI codes."""
            visible = len(ANSI_RE.sub("", s))
            return s + " " * max(0, width - visible)

        team_totals: List[int] = []
        for i, (mid, mgr_info) in enumerate(zip(team_ids, managers)):
            hist       = histories.get(mid, {})
            gw_scores  = hist.get("gw_scores", {})
            is_cap_mgr = (i == cap_idx)

            mgr_name  = mgr_info.get("name", mid)[:24]
            # Build label with fixed visible width regardless of ANSI codes in ©
            if is_cap_mgr:
                label_str = f"M{i+1} {GREEN}©{RESET} {mgr_name}"
            else:
                label_str = f"M{i+1}   {mgr_name}"

            scores = [gw_scores.get(g, {}).get("points") for g in show_gws]
            valid  = [s for s in scores if s is not None]
            avg    = round(sum(valid) / len(valid), 1) if valid else None
            # Season total from the latest available GW entry
            season_total = None
            for g in reversed(show_gws):
                entry = gw_scores.get(g, {})
                if entry.get("total_points") is not None:
                    season_total = entry["total_points"]
                    break

            score_cols = "  ".join(fmt_score(s) for s in scores)
            avg_str    = f"{color}{avg:>5.1f}{RESET}" if avg is not None else f"{GREY}  -{RESET}"
            trend      = bar_chart(scores)
            total_str  = f"{BOLD}{str(season_total) if season_total is not None else '-':^8}{RESET}"

            print(f"  {ansi_ljust(label_str, COL_WIDTH)}  {score_cols}  {avg_str}  {trend}    {total_str}")
            if valid:
                team_totals.append(int(sum(valid)))

        # Team average row
        if team_totals:
            n_mgrs   = len(team_totals)
            team_avg = sum(team_totals) / n_mgrs
            print(f"  {GREY}{'─'*(W-2)}{RESET}")
            print(f"  {BOLD}{color}{'Team avg':^30}{RESET}  {'':^{7*len(show_gws) + 2*(len(show_gws)-1)}}  "
                  f"{color}{team_avg/N:>5.1f}{RESET}")
        print()

    render_team_form("A", team_a_ids, managers_a, histories_a, cap_a_idx, GREEN)
    render_team_form("B", team_b_ids, managers_b, histories_b, cap_b_idx, CYAN)

    print(f"  {DIM}Colours: {GREEN}70+{RESET}{DIM} pts  {YELLOW}50-69{RESET}{DIM} pts  "
          f"{RED}<50{RESET}{DIM} pts  |  Trend bar shows relative scores across the 5 GWs{RESET}\n")


CHIP_DISPLAY = {
    "wildcard": "WC",
    "3xc":      "TC",
    "bboost":   "BB",
    "freehit":  "FH",
}
CHIP_FULL = {
    "wildcard": "Wildcard",
    "3xc":      "Triple Cap",
    "bboost":   "Bench Boost",
    "freehit":  "Free Hit",
}


def print_chip_board(
    team_a_ids:   List[str],
    team_b_ids:   List[str],
    managers_a:   List[dict],
    managers_b:   List[dict],
    picks_a:      List[dict],
    picks_b:      List[dict],
    chips_hist_a: Dict[str, List[dict]],
    chips_hist_b: Dict[str, List[dict]],
    cap_a_idx:    int,
    cap_b_idx:    int,
    current_gw:   int,
) -> None:
    """
    Print a visual chip availability board for both teams.

    Every chip has TWO uses per season — once in GW1-19 (H1) and once in GW20-38 (H2).
    Each chip gets two pill slots: one for H1, one for H2.

    Pill colours:
      GREEN  = available in that half
      RED    = already used in that half (shows the GW it was played)
      YELLOW = active THIS gameweek
    """
    current_half = 1 if current_gw in CHIP_H1_GWS else 2
    W = 96

    def chip_pill(used: bool, active_this_gw: bool, gw_played: Optional[int],
                  label: str, this_half: bool) -> str:
        """Render a single half-season pill for one chip."""
        if active_this_gw:
            return f"{BOLD}{YELLOW}[{label}★]{RESET}"
        if used:
            gw_str = f"GW{gw_played}" if gw_played else "used"
            return f"{RED}[{label} {gw_str}]{RESET}"
        if not this_half:
            # Future half — show as dimmed available
            return f"{GREY}[{label}]{RESET}"
        return f"{GREEN}[{label}]{RESET}"

    def vis(s: str) -> str:
        """Strip ANSI codes to get visible character count."""
        return re.sub(r"\033\[[0-9;]*m", "", s)

    def padpill(pill: str, width: int) -> str:
        pad = width - len(vis(pill))
        return pill + " " * max(0, pad)

    def render_team_chips(
        label:      str,
        team_ids:   List[str],
        managers:   List[dict],
        picks:      List[dict],
        chips_hist: Dict[str, List[dict]],
        cap_idx:    int,
        color:      str,
    ) -> None:
        print(f"\n{BOLD}{color}{'─'*W}{RESET}")
        print(f"{BOLD}{color}  TEAM {label} — CHIP TRACKER  "
              f"{GREY}(H1 = GW1-19  |  H2 = GW20-38  |  current: GW{current_gw}){RESET}")
        print(f"{color}{'─'*W}{RESET}")

        # Column headers: chip name spans both H1+H2 pills
        # Manager(28) | WC H1 | WC H2 || TC H1 | TC H2 || BB H1 | BB H2 || FH H1 | FH H2
        hdr_mgr   = f"{'Manager':<30}"
        hdr_chips = "  ".join(
            f"{'── ' + CHIP_FULL[c] + ' ──':^26}"
            for c in ALL_CHIPS
        )
        print(f"  {BOLD}{GREY}{hdr_mgr}  {hdr_chips}{RESET}")

        # Sub-header: H1 / H2 under each chip
        sub_mgr   = " " * 30
        sub_chips = "  ".join(f"{'H1':^12}  {'H2':^12}" for _ in ALL_CHIPS)
        print(f"  {GREY}{sub_mgr}  {sub_chips}{RESET}")
        print(f"  {GREY}{'─'*(W-2)}{RESET}")

        for i, (mid, mgr_info, pick) in enumerate(zip(team_ids, managers, picks)):
            history     = chips_hist.get(mid, [])
            active_chip = pick.get("active_chip")
            is_cap_mgr  = (i == cap_idx)

            mgr_name  = mgr_info.get("name", mid)[:24]
            if is_cap_mgr:
                label_str = f"M{i+1} {GREEN}©{RESET} {mgr_name}"
            else:
                label_str = f"M{i+1}   {mgr_name}"

            ANSI_RE2 = re.compile(r"\033\[[0-9;]*m")
            def ansi_ljust2(s: str, width: int) -> str:
                visible = len(ANSI_RE2.sub("", s))
                return s + " " * max(0, width - visible)

            pills = []
            for chip in ALL_CHIPS:
                short = CHIP_DISPLAY[chip]

                # Find uses in each half
                h1_entry = next((c for c in history if c["name"] == chip and c["half"] == 1), None)
                h2_entry = next((c for c in history if c["name"] == chip and c["half"] == 2), None)

                h1_used   = h1_entry is not None
                h2_used   = h2_entry is not None
                h1_active = (active_chip == chip and current_half == 1)
                h2_active = (active_chip == chip and current_half == 2)

                p1 = chip_pill(h1_used, h1_active,
                               h1_entry["event"] if h1_entry else None,
                               short, this_half=(current_half == 1))
                p2 = chip_pill(h2_used, h2_active,
                               h2_entry["event"] if h2_entry else None,
                               short, this_half=(current_half == 2))

                pills.append(f"{padpill(p1, 12)}  {padpill(p2, 12)}")

            print(f"  {ansi_ljust2(label_str, 30)}  {'  '.join(pills)}")

        print()

        # Team summary — count remaining across all managers
        total_avail = {chip: {"h1": 0, "h2": 0} for chip in ALL_CHIPS}
        for mid in team_ids:
            rem = get_remaining_chips(chips_hist.get(mid, []), current_gw)
            for chip in ALL_CHIPS:
                if rem[chip]["h1"]: total_avail[chip]["h1"] += 1
                if rem[chip]["h2"]: total_avail[chip]["h2"] += 1

        avail_parts = []
        used_parts  = []
        for chip in ALL_CHIPS:
            n_mgrs = len(team_ids)
            h1_av  = total_avail[chip]["h1"]
            h2_av  = total_avail[chip]["h2"]
            h1_us  = n_mgrs - h1_av
            h2_us  = n_mgrs - h2_av
            name   = CHIP_FULL[chip]
            if h1_av: avail_parts.append(f"{GREEN}{h1_av}x {name} H1{RESET}")
            if h2_av: avail_parts.append(f"{GREEN}{h2_av}x {name} H2{RESET}")
            if h1_us: used_parts.append(f"{RED}{h1_us}x {name} H1{RESET}")
            if h2_us: used_parts.append(f"{RED}{h2_us}x {name} H2{RESET}")

        print(f"  {BOLD}Remaining:{RESET} " + (", ".join(avail_parts) if avail_parts else f"{GREY}none{RESET}"))
        print(f"  {BOLD}Used:     {RESET} " + (", ".join(used_parts)  if used_parts  else f"{GREY}none{RESET}"))
        print()

    render_team_chips("A", team_a_ids, managers_a, picks_a, chips_hist_a, cap_a_idx, GREEN)
    render_team_chips("B", team_b_ids, managers_b, picks_b, chips_hist_b, cap_b_idx, CYAN)

    print(
        f"  {DIM}Legend:  "
        f"{GREEN}[XX]{RESET}{DIM} = available  "
        f"{RED}[XX GWn]{RESET}{DIM} = used in GWn  "
        f"{YELLOW}[XX★]{RESET}{DIM} = active this GW  "
        f"{GREY}[XX]{RESET}{DIM} = future half (not yet unlocked)  "
        f"H1=GW1-19  H2=GW20-38{RESET}\n"
    )



# ── Intra-GW points race ──────────────────────────────────────────────────────

# Human-readable labels for each FPL stat identifier
STAT_LABELS = {
    "minutes":                  "Playing time",
    "goals_scored":             "Goal",
    "assists":                  "Assist",
    "clean_sheets":             "Clean sheet",
    "goals_conceded":           "Goals conceded",
    "own_goals":                "Own goal",
    "penalties_saved":          "Penalty save",
    "penalties_missed":         "Penalty missed",
    "yellow_cards":             "Yellow card",
    "red_cards":                "Red card",
    "saves":                    "Saves",
    "bonus":                    "Bonus",
    "bps":                      "BPS",
}


def get_fixture_kickoffs(gw: int) -> Dict[int, dict]:
    """
    Fetch fixture metadata for all matches in this GW.
    Returns { fixture_id: { "kickoff": ISO-str, "home": club_name, "away": club_name } }
    Falls back to empty dict if the fetch fails.
    """
    try:
        data   = fetch(f"{BASE}/fixtures/?event={gw}")
        result = {}
        for f in data:
            fid = f.get("id")
            ko  = f.get("kickoff_time", "")
            if fid:
                result[fid] = {
                    "kickoff": ko,
                    "home":    CLUBS.get(f.get("team_h"), f"H{f.get('team_h')}"),
                    "away":    CLUBS.get(f.get("team_a"), f"A{f.get('team_a')}"),
                }
        return result
    except Exception:
        return {}


def build_intra_gw_timeline(
    picks_a:      List[dict],
    picks_b:      List[dict],
    player_map:   Dict[int, dict],
    live_explain: Dict[int, list],
    live_scores:  Dict[int, int],
    fixture_ko:   Optional[Dict[int, str]] = None,
) -> List[dict]:
    """
    Build a truly chronological list of scoring events across ALL fixtures
    in the current GW — interleaved across matches by kickoff time and
    stat type so the output reads like a live match thread.

    Ordering:
      1. Fixture kickoff time (earliest game first — 3pm before 5:30pm)
      2. Within a kickoff slot, stat priority: goals → assists → pen saves →
         clean sheets → saves → bonus → minutes → cards → own goals → bps
      3. Within same kickoff + stat, alphabetical by player name (stable tie-break)

    Each event dict:
      { player_id, name, position, club, fixture, kickoff,
        stat, raw_points, value, count_a, count_b,
        pts_swing_a, pts_swing_b, score_a_after, score_b_after }
    """
    # Stat sort order — lower = earlier in the timeline within a match
    STAT_PRIORITY = {
        "goals_scored":     0,
        "assists":          1,
        "penalties_saved":  2,
        "clean_sheets":     3,
        "saves":            4,
        "bonus":            5,
        "minutes":          6,
        "goals_conceded":   7,
        "yellow_cards":     8,
        "red_cards":        9,
        "own_goals":        10,
        "penalties_missed": 11,
        "bps":              99,
    }

    fixture_ko = fixture_ko or {}

    # ── Build ownership map ───────────────────────────────────────────────────
    # player_id → { "A": effective_count, "B": effective_count }
    ownership: Dict[int, Dict[str, int]] = {}

    def is_playing(pick: dict, active_chip: Optional[str]) -> bool:
        return pick["position"] <= 11 or active_chip == "bboost"

    for mgr in picks_a:
        for p in mgr["picks"]:
            if not is_playing(p, mgr["active_chip"]):
                continue
            pid = p["element"]
            ownership.setdefault(pid, {"A": 0, "B": 0})
            ownership[pid]["A"] += p["count"]

    for mgr in picks_b:
        for p in mgr["picks"]:
            if not is_playing(p, mgr["active_chip"]):
                continue
            pid = p["element"]
            ownership.setdefault(pid, {"A": 0, "B": 0})
            ownership[pid]["B"] += p["count"]

    # ── Flatten events from all relevant players ──────────────────────────────
    raw_events = []
    for pid, own in ownership.items():
        if own["A"] == 0 and own["B"] == 0:
            continue
        for ev in live_explain.get(pid, []):
            if ev["points"] == 0:
                continue
            fid = ev["fixture"]
            fix_meta = fixture_ko.get(fid, {})
            ko       = fix_meta.get("kickoff", "") if isinstance(fix_meta, dict) else ""
            raw_events.append({
                "player_id": pid,
                "fixture":   fid,
                "kickoff":   ko,
                "home":      fix_meta.get("home", "") if isinstance(fix_meta, dict) else "",
                "away":      fix_meta.get("away", "") if isinstance(fix_meta, dict) else "",
                "stat":      ev["stat"],
                "points":    ev["points"],
                "value":     ev["value"],
                "count_a":   own["A"],
                "count_b":   own["B"],
            })

    # ── Sort: kickoff time → stat priority → player name ─────────────────────
    raw_events.sort(key=lambda e: (
        e["kickoff"] or "9999",                         # earliest game first
        STAT_PRIORITY.get(e["stat"], 50),               # goal before assist etc.
        player_map.get(e["player_id"], {}).get("name", ""),  # stable tie-break
    ))

    # ── Build timeline with running H2H scores ────────────────────────────────
    score_a = score_b = 0
    timeline = []
    for ev in raw_events:
        pid  = ev["player_id"]
        pl   = player_map.get(pid, {"name": f"#{pid}", "position": "?", "club": "?"})
        ca, cb = ev["count_a"], ev["count_b"]
        pts_a  = ev["points"] * ca
        pts_b  = ev["points"] * cb
        score_a += pts_a
        score_b += pts_b
        timeline.append({
            "player_id":     pid,
            "name":          pl["name"],
            "position":      pl.get("position", "?"),
            "club":          pl.get("club", "?"),
            "fixture":       ev["fixture"],
            "kickoff":       ev["kickoff"],
            "home":          ev.get("home", ""),
            "away":          ev.get("away", ""),
            "stat":          ev["stat"],
            "raw_points":    ev["points"],
            "value":         ev["value"],
            "count_a":       ca,
            "count_b":       cb,
            "pts_swing_a":   pts_a,
            "pts_swing_b":   pts_b,
            "score_a_after": score_a,
            "score_b_after": score_b,
        })

    return timeline


def print_intra_gw_race(
    timeline:      List[dict],
    gw:            int,
    league_name_a: str = "Team A",
    league_name_b: str = "Team B",
    picks_a:       Optional[List[dict]] = None,
    picks_b:       Optional[List[dict]] = None,
    live_scores:   Optional[Dict[int, int]] = None,
) -> None:
    """
    Print the within-GW scoring timeline as a live commentary-style points race.

    Visual design:
      ┌─ Fixture header: "Arsenal  vs  Liverpool  14:00 UTC" ─────────────┐
      │  ⚽ Salah  Goal  +6  →  benefits The Invincibles (A×4)
      │     The Invincibles  36 ◀████████░░░░│░░░░░░░░░░░░  12  Chaos FC
      └────────────────────────────────────────────────────────────────────┘
    """
    # ── Stat icons ────────────────────────────────────────────────────────────
    STAT_ICONS = {
        "goals_scored":     "⚽",
        "assists":          "🅰 ",
        "clean_sheets":     "🛡 ",
        "saves":            "🧤",
        "penalties_saved":  "🧤",
        "bonus":            "⭐",
        "minutes":          "⏱ ",
        "goals_conceded":   "❌",
        "yellow_cards":     "🟨",
        "red_cards":        "🟥",
        "own_goals":        "😬",
        "penalties_missed": "❌",
        "bps":              "📊",
    }

    W   = 88
    BAR = 18   # half-width of tug-of-war bar

    if not timeline:
        print(f"\n{GREY}  (No intra-GW events to display yet){RESET}\n")
        return

    na = league_name_a[:22]
    nb = league_name_b[:22]

    max_lead = max(
        max(abs(e["score_a_after"] - e["score_b_after"]) for e in timeline),
        1,
    )

    # ── Section header ────────────────────────────────────────────────────────
    print(f"\n{BOLD}{YELLOW}{'━'*W}{RESET}")
    print(f"{BOLD}{YELLOW}  ⚡  GW{gw} — LIVE POINTS RACE{RESET}")
    print(f"  {BOLD}{GREEN}{na}{RESET}  {GREY}vs{RESET}  {BOLD}{CYAN}{nb}{RESET}")
    print(f"{BOLD}{YELLOW}{'━'*W}{RESET}")

    prev_fixture = None
    prev_lead    = None   # "A" | "B" | "="

    for ev in timeline:
        name     = ev["name"]
        stat     = ev["stat"]
        icon     = STAT_ICONS.get(stat, "  ")
        stat_lbl = STAT_LABELS.get(stat, stat)
        raw_pts  = ev["raw_points"]
        ca, cb   = ev["count_a"], ev["count_b"]
        sa, sb   = ev["score_a_after"], ev["score_b_after"]
        pc       = POS_COLOR.get(ev.get("position", ""), "")
        fixture  = ev.get("fixture")
        ko       = ev.get("kickoff", "")
        home     = ev.get("home", "")
        away     = ev.get("away", "")

        # ── Fixture match header ──────────────────────────────────────────────
        if fixture != prev_fixture:
            ko_time = ko[11:16] + " UTC" if len(ko) >= 16 else ""
            if home and away:
                match_str = f"  {home}  vs  {away}"
                if ko_time:
                    match_str += f"  {GREY}({ko_time}){RESET}"
            else:
                match_str = f"  Fixture {fixture}  {GREY}({ko_time}){RESET}" if ko_time else f"  Fixture {fixture}"

            inner_w = W - 4
            print(f"\n  {BOLD}┌{'─'*inner_w}┐{RESET}")
            # Centre the match string (approximate — ignores ANSI in width calc)
            visible = match_str.replace(GREY,"").replace(RESET,"").replace(BOLD,"")
            pad = max(0, inner_w - len(visible) - 2)
            lpad = pad // 2
            rpad = pad - lpad
            print(f"  {BOLD}│{RESET}{' '*lpad}{BOLD}{match_str}{RESET}{' '*rpad}{BOLD}│{RESET}")
            print(f"  {BOLD}└{'─'*inner_w}┘{RESET}")
            prev_fixture = fixture

        # ── Points badge colouring (by magnitude) ────────────────────────────
        if raw_pts >= 9:
            pts_color = f"{BOLD}{YELLOW}"
        elif raw_pts >= 6:
            pts_color = YELLOW
        elif raw_pts > 0:
            pts_color = GREEN
        else:
            pts_color = RED
        pts_badge = f"{pts_color}+{raw_pts}pts{RESET}" if raw_pts > 0 else f"{RED}{raw_pts}pts{RESET}"

        # ── Benefit string ────────────────────────────────────────────────────
        if ca > 0 and cb > 0:
            benefit_str = f"{GREEN}{na}{RESET} & {CYAN}{nb}{RESET}"
        elif ca > 0:
            benefit_str = f"{BOLD}{GREEN}▲ {na}{RESET}"
        else:
            benefit_str = f"{BOLD}{CYAN}▲ {nb}{RESET}"

        # Multiplier note if relevant
        if ca > 1 and cb > 1:
            mul_note = f"  {GREY}(A×{ca}, B×{cb}){RESET}"
        elif ca > 1:
            mul_note = f"  {GREY}(×{ca}){RESET}"
        elif cb > 1:
            mul_note = f"  {GREY}(×{cb}){RESET}"
        else:
            mul_note = ""

        # ── Event line ────────────────────────────────────────────────────────
        print(
            f"  {icon} {pc}{BOLD}{name}{RESET}  "
            f"{GREY}{stat_lbl}{RESET}  "
            f"{pts_badge}  →  {benefit_str}{mul_note}"
        )

        # ── Tug-of-war scoreboard ─────────────────────────────────────────────
        lead = sa - sb
        cur_lead = "A" if lead > 0 else ("B" if lead < 0 else "=")

        # Lead-change banner before scoreline
        if prev_lead is not None and cur_lead != prev_lead:
            if cur_lead == "A":
                print(f"  {BOLD}{GREEN}  ┗━▶ {na} take the lead!{RESET}")
            elif cur_lead == "B":
                print(f"  {BOLD}{CYAN}  ┗━▶ {nb} take the lead!{RESET}")
            else:
                print(f"  {BOLD}{YELLOW}  ┗━▶ Scores level!{RESET}")
        prev_lead = cur_lead

        # Bar — centre-anchored, grows toward whoever is leading
        filled = min(BAR, round(BAR * abs(lead) / max_lead)) if max_lead else 0
        empty  = BAR - filled
        if lead > 0:
            l_bar = f"{GREEN}{'░'*empty}{'█'*filled}{RESET}"
            r_bar = f"{GREY}{'░'*BAR}{RESET}"
            arrow = f"{GREEN}◀{RESET}"
        elif lead < 0:
            l_bar = f"{GREY}{'░'*BAR}{RESET}"
            r_bar = f"{CYAN}{'█'*filled}{'░'*empty}{RESET}"
            arrow = f"{CYAN}▶{RESET}"
        else:
            l_bar = f"{GREY}{'░'*BAR}{RESET}"
            r_bar = f"{GREY}{'░'*BAR}{RESET}"
            arrow = f"{YELLOW}◆{RESET}"

        score_a_fmt = f"{BOLD}{GREEN}{sa}{RESET}"
        score_b_fmt = f"{BOLD}{CYAN}{sb}{RESET}"

        print(
            f"  {GREY}  └╴{RESET}"
            f"{GREEN}{na[:16]:<16}{RESET} "
            f"{score_a_fmt:} {arrow} "
            f"{l_bar}{GREY}│{RESET}{r_bar} "
            f"{arrow} {score_b_fmt} "
            f"{CYAN}{nb[:16]}{RESET}"
        )

    # ── Final result block ────────────────────────────────────────────────────
    final_a = timeline[-1]["score_a_after"] if timeline else 0
    final_b = timeline[-1]["score_b_after"] if timeline else 0
    diff    = final_a - final_b

    print(f"\n{BOLD}{YELLOW}{'━'*W}{RESET}")
    if diff > 0:
        verdict = f"{BOLD}{GREEN}  ▲  {na}  {final_a} – {final_b}  (+{diff} pts){RESET}"
    elif diff < 0:
        verdict = f"{BOLD}{CYAN}  ▲  {nb}  {final_b} – {final_a}  (+{abs(diff)} pts){RESET}"
    else:
        verdict = f"{BOLD}{YELLOW}  ══  Level  {final_a} – {final_b}{RESET}"
    print(verdict)
    print(f"{BOLD}{YELLOW}{'━'*W}{RESET}")
    print(f"  {DIM}Ordered by kickoff time → goal/assist/CS/bonus. Counts include captain ×.{RESET}\n")


def render_all(args, picks_a, picks_b, player_map, live_scores, rows, cap_a_idx, cap_b_idx,
               managers_a=None, managers_b=None, chips_hist_a=None, chips_hist_b=None,
               histories_a=None, histories_b=None, live_explain=None):
    league_name_a = getattr(args, "league_name_a", "Team A")
    league_name_b = getattr(args, "league_name_b", "Team B")

    # ── Compute team totals for scoreboard ───────────────────────────────────
    total_a = total_b = 0
    for i, mgr in enumerate(picks_a):
        _, _, final = manager_final_score(mgr, live_scores)
        total_a += final * 2 if i == cap_a_idx else final
    for i, mgr in enumerate(picks_b):
        _, _, final = manager_final_score(mgr, live_scores)
        total_b += final * 2 if i == cap_b_idx else final

    # ── H2H Scoreboard ────────────────────────────────────────────────────────
    print_h2h_scoreboard(total_a, total_b, league_name_a, league_name_b, args.gw)

    # ── Intra-GW points race (live scoring timeline within this gameweek) ────
    if live_explain is not None:
        fixture_ko = get_fixture_kickoffs(args.gw)
        timeline = build_intra_gw_timeline(
            picks_a, picks_b, player_map, live_explain, live_scores,
            fixture_ko=fixture_ko,
        )
        print_intra_gw_race(
            timeline, args.gw, league_name_a, league_name_b,
            picks_a=picks_a, picks_b=picks_b, live_scores=live_scores,
        )

    # ── Cumulative points race (last 8 GWs trend) ─────────────────────────────
    if managers_a is not None and histories_a is not None:
        print_points_race(
            args.team_a, args.team_b,
            managers_a, managers_b,
            histories_a, histories_b,
            cap_a_idx, cap_b_idx,
            args.gw,
            league_name_a, league_name_b,
        )

    # ── Form table ────────────────────────────────────────────────────────────
    if managers_a is not None and histories_a is not None:
        print_form_table(
            args.team_a, args.team_b,
            managers_a, managers_b,
            histories_a, histories_b,
            cap_a_idx, cap_b_idx,
            args.gw,
        )
    # ── Chip board ────────────────────────────────────────────────────────────
    if (not getattr(args, "no_chips", False)
            and managers_a is not None and chips_hist_a is not None):
        print_chip_board(
            args.team_a, args.team_b,
            managers_a, managers_b,
            picks_a, picks_b,
            chips_hist_a, chips_hist_b,
            cap_a_idx, cap_b_idx,
            args.gw,
        )
    # ── Squad summaries ───────────────────────────────────────────────────────
    if not args.no_summary:
        print_team_summary("A", args.team_a, cap_a_idx, picks_a, player_map, live_scores, GREEN, managers_a)
        print_team_summary("B", args.team_b, cap_b_idx, picks_b, player_map, live_scores, CYAN,  managers_b)

    # ── Swing bar chart ───────────────────────────────────────────────────────
    print_swing_bar_chart(rows, args.gw)

    # ── Position breakdown ────────────────────────────────────────────────────
    print_position_breakdown(picks_a, picks_b, player_map, live_scores, league_name_a, league_name_b)

    # ── Differential table + swing summary + score totals ────────────────────
    print_differential_table(rows, args.gw)
    print_swing_summary(rows, picks_a, picks_b, live_scores)
    print_team_score_totals(picks_a, picks_b, live_scores, cap_a_idx, cap_b_idx, managers_a, managers_b)


# ── League helpers ────────────────────────────────────────────────────────────

def get_league_info(league_id: str) -> Dict[str, object]:
    """
    Fetch a classic league's name AND its manager roster in one pass.

    get_league_name() and get_league_managers() used to each hit
    `leagues-classic/{id}/standings/?page_standings=1` independently — same
    URL, fetched twice for every league, every time. This does it once.
    Only leagues with >50 entries need a second page for the roster; the
    name is always on page 1, so that part never pays the extra request.

    Returns: {"name": str, "managers": [{"id","name","manager"}, ...]}
    """
    try:
        page1 = fetch(f"{BASE}/leagues-classic/{league_id}/standings/?page_standings=1")
    except Exception:
        return {"name": f"League {league_id}", "managers": []}

    name = page1.get("league", {}).get("name", f"League {league_id}")

    def to_managers(results: list) -> List[dict]:
        return [
            {
                "id":      str(r["entry"]),
                "name":    r.get("entry_name", f"Team #{r['entry']}"),
                "manager": r.get("player_name", ""),
            }
            for r in results
        ]

    managers = to_managers(page1.get("standings", {}).get("results", []))
    has_next = page1.get("standings", {}).get("has_next", False)

    if has_next:
        # Rare (>50-manager league) — fall back to sequential pagination for
        # the remaining pages only; page 1 above already saved one request.
        page = 2
        while True:
            data    = fetch(f"{BASE}/leagues-classic/{league_id}/standings/?page_standings={page}")
            results = data.get("standings", {}).get("results", [])
            if not results:
                break
            managers.extend(to_managers(results))
            if not data["standings"].get("has_next", False):
                break
            page += 1

    return {"name": name, "managers": managers}


def get_league_name(league_id: str) -> str:
    """Fetch the human-readable name of a classic league.
    Prefer get_league_info() if you also need the roster — this and
    get_league_managers() otherwise duplicate the same request."""
    return get_league_info(league_id)["name"]


def get_league_managers(league_id: str) -> List[dict]:
    """Fetch the classic league standings and return a list of managers.
    Prefer get_league_info() if you also need the league name — this and
    get_league_name() otherwise duplicate the same request."""
    return get_league_info(league_id)["managers"]


def print_league_roster(label: str, managers: List[dict], color: str, league_name: str = "") -> None:
    """Print the managers fetched from a league so the user can confirm."""
    title = league_name if league_name else f"League {label}"
    print(f"\n  {BOLD}{color}{title} (Team {label}) managers:{RESET}")
    for i, m in enumerate(managers):
        print(f"    {i+1}.  {col(m['name'], 30)}  {GREY}({m['manager']}  —  ID: {m['id']}){RESET}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="fpl_h2h",
        description="FPL Head-to-Head Ownership & Point-Swing Analyser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Standard run — pull managers from two league IDs
  python fpl_h2h.py --gw 29 \\
      --league-a 123456 \\
      --league-b 654321 \\
      --cap-a "John Smith" --cap-b "Rahul Verma"

  # No per-manager squad summary
  python fpl_h2h.py --gw 29 \\
      --league-a 123456 --league-b 654321 \\
      --cap-a "John Smith" --cap-b "Rahul Verma" --no-summary

  # Save plain-text output to file
  python fpl_h2h.py --gw 29 \\
      --league-a 123456 --league-b 654321 \\
      --cap-a "John Smith" --cap-b "Rahul Verma" --output gw29.txt

  # JSON export
  python fpl_h2h.py --gw 29 \\
      --league-a 123456 --league-b 654321 \\
      --cap-a "John Smith" --cap-b "Rahul Verma" --json

  # Skip live score fetch (useful mid-week or pre-GW)
  python fpl_h2h.py --gw 29 \\
      --league-a 123456 --league-b 654321 \\
      --cap-a "John Smith" --cap-b "Rahul Verma" --no-live

  # Only show the live intra-GW points race (fastest output during a gameweek)
  python fpl_h2h.py --gw 35 \\
      --league-a 123456 --league-b 654321 \\
      --cap-a "John Smith" --cap-b "Rahul Verma" --race-only

Notes
-----
  --league-a / --league-b
      The numeric ID from the league URL:
      https://fantasy.premierleague.com/leagues/{league_id}/standings/c

  --cap-a / --cap-b
      The real name of the H2H captain manager for each team.
      Case-insensitive; partial names work as long as they match exactly one manager.
      Example: "Smith", "john smith", "John Smith" all resolve the same person.
      Fallback: numeric manager ID still accepted if you prefer.
        """,
    )

    p.add_argument("--gw",         type=int,    required=True,  help="Gameweek number (1-38)")
    p.add_argument("--league-a",   type=str,    required=True,  help="League ID for Team A")
    p.add_argument("--league-b",   type=str,    required=True,  help="League ID for Team B")
    p.add_argument("--cap-a",      type=str,    required=True,  help="Name of H2H captain manager in Team A (partial match supported)")
    p.add_argument("--cap-b",      type=str,    required=True,  help="Name of H2H captain manager in Team B (partial match supported)")
    p.add_argument("--output",     metavar="FILE",               help="Save plain-text output to file")
    p.add_argument("--json",       action="store_true",          help="Print raw JSON instead of table")
    p.add_argument("--no-summary", action="store_true",          help="Skip per-manager squad section")
    p.add_argument("--no-live",    action="store_true",          help="Skip live score fetch (swing = 0)")
    p.add_argument("--race-only",  action="store_true",          help="Only render the intra-GW points race (skips all other sections)")

    return p.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.gw <= 38:
        raise SystemExit("✗  --gw must be between 1 and 38")


def resolve_cap_index(cap_query: str, managers: List[dict], team_label: str) -> int:
    """
    Resolve the H2H captain from a name query (or numeric ID fallback).

    Matching priority:
      1. Exact match on manager real name (case-insensitive)
      2. Exact match on FPL team/entry name (case-insensitive)
      3. Partial match on real name (case-insensitive substring)
      4. Partial match on team name (case-insensitive substring)
      5. Numeric ID match (legacy fallback)
    """
    q = cap_query.strip().lower()

    # Priority 1 & 2: exact matches
    for i, m in enumerate(managers):
        real = (m.get("manager") or "").lower()
        team = (m.get("name")    or "").lower()
        if q == real or q == team:
            label = m.get("manager") or m.get("name") or cap_query
            print(f"  [H2H captain Team {team_label}] {label}")
            return i

    # Priority 3 & 4: partial matches — collect all hits
    partial = []
    for i, m in enumerate(managers):
        real = (m.get("manager") or "").lower()
        team = (m.get("name")    or "").lower()
        if q in real or q in team:
            partial.append((i, m))

    if len(partial) == 1:
        i, m = partial[0]
        label = m.get("manager") or m.get("name") or cap_query
        print(f"  [H2H captain Team {team_label}] {label}  {GREY}(partial match){RESET}")
        return i

    if len(partial) > 1:
        hits = ", ".join(
            f"'{m.get('manager') or m.get('name')}'" for _, m in partial
        )
        raise SystemExit(
            f"  --cap-{team_label.lower()} '{cap_query}' matched multiple managers: {hits}\n"
            f"   Be more specific."
        )

    # Priority 5: numeric ID fallback
    for i, m in enumerate(managers):
        if m["id"] == cap_query:
            label = m.get("manager") or m.get("name") or cap_query
            print(f"  [H2H captain Team {team_label}] {label}  {GREY}(matched by ID){RESET}")
            return i

    names = ", ".join(m.get("manager") or m.get("name") or m["id"] for m in managers)
    raise SystemExit(
        f"  --cap-{team_label.lower()} '{cap_query}' not found in League {team_label}.\n"
        f"   Valid managers: {names}"
    )


def main() -> None:
    args = parse_args()
    validate_args(args)

    print(f"\n{BOLD}FPL H2H SCOUT{RESET}  {GREY}GW{args.gw}{RESET}")
    print(f"{GREY}{'─'*40}{RESET}\n")

    # 1. Bootstrap + live scores
    bootstrap  = get_bootstrap()
    player_map = build_player_map(bootstrap)

    if args.no_live:
        live_scores:  Dict[int, int] = {}
        live_explain: Dict[int, list] = {}
        print(f"  {GREY}(live scores skipped){RESET}")
    else:
        live_scores, _, live_explain = get_live_scores(args.gw)

    # 2. Resolve managers from league IDs (also fetch human-readable league names).
    # One combined request per league (was two), and both leagues fetched at once.
    print(f"\n-> Fetching League A + B info  (IDs: {args.league_a}, {args.league_b})...")
    info_a, info_b = parallel_map(get_league_info, [args.league_a, args.league_b])
    league_name_a, managers_a = info_a["name"], info_a["managers"]
    league_name_b, managers_b = info_b["name"], info_b["managers"]
    args.league_name_a = league_name_a
    args.league_name_b = league_name_b
    print_league_roster("A", managers_a, GREEN, league_name_a)
    print_league_roster("B", managers_b, CYAN, league_name_b)

    # Print a named matchup banner now that we have both league names
    print(f"\n{BOLD}  {GREEN}{league_name_a}{RESET}{BOLD}  vs  {CYAN}{league_name_b}{RESET}  {GREY}GW{args.gw}{RESET}")
    print(f"{GREY}{'─'*60}{RESET}")

    # Resolve captain indices
    cap_a_idx = resolve_cap_index(args.cap_a, managers_a, "A")
    cap_b_idx = resolve_cap_index(args.cap_b, managers_b, "B")

    team_a_ids = [m["id"] for m in managers_a]
    team_b_ids = [m["id"] for m in managers_b]

    # 3. Picks
    print(f"\n-> Fetching Team A picks...")
    picks_a = fetch_team_picks(team_a_ids, cap_a_idx, args.gw, "A", player_map, managers_a)
    print(f"\n-> Fetching Team B picks...")
    picks_b = fetch_team_picks(team_b_ids, cap_b_idx, args.gw, "B", player_map, managers_b)

    # 4. Season histories — chips + GW scores (single request per manager)
    # Skipped entirely under --race-only (not needed for the points race)
    if not args.race_only:
        print(f"\n-> Fetching manager season histories...")
        histories_a = get_all_season_histories(team_a_ids)
        histories_b = get_all_season_histories(team_b_ids)
        chips_hist_a = {mid: h["chips"] for mid, h in histories_a.items()}
        chips_hist_b = {mid: h["chips"] for mid, h in histories_b.items()}
        print(f"   Done")
    else:
        histories_a = histories_b = chips_hist_a = chips_hist_b = {}

    # ── Race-only mode: render just the intra-GW points race and exit ─────────
    if args.race_only:
        if args.no_live:
            print(f"\n{RED}✗  --race-only requires live scores. Remove --no-live and try again.{RESET}\n")
            raise SystemExit(1)
        fixture_ko = get_fixture_kickoffs(args.gw)
        timeline   = build_intra_gw_timeline(
            picks_a, picks_b, player_map, live_explain, live_scores,
            fixture_ko=fixture_ko,
        )
        print_intra_gw_race(
            timeline, args.gw, league_name_a, league_name_b,
            picks_a=picks_a, picks_b=picks_b, live_scores=live_scores,
        )
        if args.output:
            ansi_escape = re.compile(r"\033\[[0-9;]*m")
            buf = io.StringIO()
            sys.stdout, old = buf, sys.stdout
            print_intra_gw_race(
                timeline, args.gw, league_name_a, league_name_b,
                picks_a=picks_a, picks_b=picks_b, live_scores=live_scores,
            )
            sys.stdout = old
            with open(args.output, "w") as f:
                f.write(ansi_escape.sub("", buf.getvalue()))
            print(f"{GREEN}Saved to {args.output}{RESET}\n")
        return

    # 5. Differential + swing
    rows = build_differential(picks_a, picks_b, player_map, live_scores)

    # 5. Output
    # Attach resolved team IDs onto args so render_all can still use them
    args.team_a = team_a_ids
    args.team_b = team_b_ids

    if args.json:
        def mgr_json(mgr, managers_list, idx):
            raw, hit, final = manager_final_score(mgr, live_scores)
            mgr_info  = managers_list[idx] if managers_list and idx < len(managers_list) else {}
            mgr_label = mgr_info.get("manager") or mgr_info.get("name") or mgr["manager_id"]
            return {"manager_id": mgr["manager_id"], "manager_name": mgr_label,
                    "raw_score": raw, "transfer_hit": hit, "final_score": final,
                    "active_chip": mgr["active_chip"]}
        print(json.dumps({
            "gw":            args.gw,
            "league_a":      args.league_name_a,
            "league_b":      args.league_name_b,
            "net_swing":     sum(r["point_swing"] for r in rows),
            "differential":  rows,
            "team_a_scores": [mgr_json(m, managers_a, i) for i, m in enumerate(picks_a)],
            "team_b_scores": [mgr_json(m, managers_b, i) for i, m in enumerate(picks_b)],
        }, indent=2))
        return

    render_all(args, picks_a, picks_b, player_map, live_scores, rows, cap_a_idx, cap_b_idx,
               managers_a=managers_a, managers_b=managers_b,
               chips_hist_a=chips_hist_a, chips_hist_b=chips_hist_b,
               histories_a=histories_a, histories_b=histories_b,
               live_explain=live_explain)

    if args.output:
        ansi_escape = re.compile(r"\033\[[0-9;]*m")
        buf = io.StringIO()
        sys.stdout, old = buf, sys.stdout
        render_all(args, picks_a, picks_b, player_map, live_scores, rows, cap_a_idx, cap_b_idx,
               managers_a=managers_a, managers_b=managers_b,
               chips_hist_a=chips_hist_a, chips_hist_b=chips_hist_b,
               histories_a=histories_a, histories_b=histories_b,
               live_explain=live_explain)
        sys.stdout = old
        with open(args.output, "w") as f:
            f.write(ansi_escape.sub("", buf.getvalue()))
        print(f"{GREEN}Saved to {args.output}{RESET}\n")


if __name__ == "__main__":
    main()
