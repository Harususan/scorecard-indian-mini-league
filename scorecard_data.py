"""UI-free scorecard data and scoring, shared with the existing IML engine."""
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from copy import deepcopy
import threading
import time
from datetime import datetime, timezone
import json
import urllib.request
import fpl_h2h_v14 as engine
from iml_registry import TEAMS, CLUB_ID_TO_TEAM


def season_key(bootstrap=None):
    deadlines = [e.get("deadline_time", "") for e in (bootstrap or {}).get("events", []) if e.get("deadline_time")]
    if deadlines:
        return f"{min(deadlines)[:4]}-{int(min(deadlines)[:4])+1}"
    now = datetime.now(timezone.utc)
    year = now.year if now.month >= 7 else now.year - 1
    return f"{year}-{year+1}"



_ENDPOINT_CACHE = {}
_INFLIGHT = {}
_CACHE_LOCK = threading.Lock()
_CACHE_EPOCH = 0


def endpoint_ttl(path):
    if path == "bootstrap-static/": return 3600
    if path.startswith("leagues-classic/"): return 3600
    if path.startswith("entry/"): return 300
    if path.startswith("event/"): return 30
    return 60


def _request_json(path):
    request = urllib.request.Request(f"{engine.BASE}/{path}", headers={"User-Agent": "IML-Scorecards/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _validate_response(path, payload):
    if path.startswith("fixtures/"):
        if not isinstance(payload, list): raise ValueError("Invalid fixtures response")
    elif not isinstance(payload, dict):
        raise ValueError("Invalid API response")
    elif path == "bootstrap-static/" or path.startswith("event/"):
        if not payload.get("elements"): raise ValueError("Incomplete player/live response")
    elif path.startswith("entry/"):
        if not payload.get("picks"): raise ValueError("Gameweek picks are not published")
    elif path.startswith("leagues-classic/"):
        standings = payload.get("standings", {})
        if len(standings.get("results", [])) != 4 or standings.get("has_next"):
            raise ValueError("Incomplete or unexpected four-manager club roster")


def fetch(path):
    """Share endpoint snapshots across views/sessions; coalesce simultaneous misses."""
    with _CACHE_LOCK:
        now = time.monotonic()
        cached = _ENDPOINT_CACHE.get(path)
        if cached is not None and cached[0] > now:
            return deepcopy(cached[1])
        epoch = _CACHE_EPOCH
        flight_key = (path, epoch)
        pending = _INFLIGHT.get(flight_key)
        owner = pending is None
        if owner:
            pending = _INFLIGHT[flight_key] = Future()
    if not owner:
        return deepcopy(pending.result())
    try:
        payload = _request_json(path)
        _validate_response(path, payload)
        with _CACHE_LOCK:
            if epoch == _CACHE_EPOCH:
                if len(_ENDPOINT_CACHE) >= 512:
                    oldest = min(_ENDPOINT_CACHE, key=lambda key: _ENDPOINT_CACHE[key][0])
                    _ENDPOINT_CACHE.pop(oldest)
                _ENDPOINT_CACHE[path] = (time.monotonic() + endpoint_ttl(path), payload)
        pending.set_result(payload)
        return deepcopy(payload)
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _CACHE_LOCK:
            _INFLIGHT.pop(flight_key, None)


def clear_endpoint_cache(include_squads=False):
    """Refresh volatile scores without throwing away slower-changing metadata."""
    global _CACHE_EPOCH
    with _CACHE_LOCK:
        _CACHE_EPOCH += 1
        for path in list(_ENDPOINT_CACHE):
            if include_squads or path.startswith(("event/", "fixtures/")):
                _ENDPOINT_CACHE.pop(path, None)


def fixture_kickoffs(fixtures):
    return {f["id"]: {"kickoff": f.get("kickoff_time") or "",
            "home": engine.CLUBS.get(f.get("team_h"), "?"),
            "away": engine.CLUBS.get(f.get("team_a"), "?")}
            for f in fixtures if f.get("id")}


def current_gw(bootstrap):
    events = bootstrap.get("events", [])
    event = next((e for e in events if e.get("is_current")), None)
    event = event or next((e for e in events if e.get("is_next")), None)
    return int(event["id"]) if event else 1


def load_roster(team):
    data = fetch(f"leagues-classic/{TEAMS[team]['league_id']}/standings/?page_standings=1")
    if data.get("standings", {}).get("has_next"):
        raise ValueError("Expected a four-manager club league; registry needs review")
    roster = [{"id": str(r["entry"]), "manager": r.get("player_name") or r.get("entry_name", "Manager")}
              for r in data.get("standings", {}).get("results", [])]
    if len(roster) != 4:
        raise ValueError(f"Expected 4 managers, received {len(roster)}")
    return roster


def score_manager(manager, data, scores):
    if not data.get("picks"):
        raise ValueError("Gameweek picks are not published")
    picks = [{"is_captain": False, "is_vice_captain": False, **p} for p in data["picks"]]
    chip = data.get("active_chip")
    captain = engine.resolve_effective_captain(picks, chip)
    captain_id = captain["element"] if captain else None
    meta = dict(picks=picks, active_chip=chip, eff_cap_id=captain_id,
                cap_mul=engine.fpl_cap_multiplier(chip), manager_id=manager["id"],
                transfer_hit=-abs(data.get("entry_history", {}).get("event_transfers_cost", 0)))
    raw, hit, total = engine.manager_final_score(meta, scores)
    return {**manager, "raw": raw, "hit": hit, "points": total, "chip": chip, "meta": meta}


def load_team(team, gw, scores):
    roster = load_roster(team)
    with ThreadPoolExecutor(max_workers=4) as pool:
        picks = list(pool.map(lambda m: fetch(f"entry/{m['id']}/event/{gw}/picks/"), roster))
    return {"team": team, "managers": [score_manager(m, p, scores) for m, p in zip(roster, picks)]}


def flatten_explanations(elements):
    return {int(element["id"]): [
        {"fixture": block.get("fixture"), "stat": stat.get("identifier", ""),
         "points": stat.get("points", 0), "value": stat.get("value", 0)}
        for block in element.get("explain", []) for stat in block.get("stats", [])
        if stat.get("points", 0) != 0
    ] for element in elements}


def load_snapshot(gw, team_names=None):
    requested = tuple(TEAMS) if team_names is None else tuple(dict.fromkeys(team_names))
    if any(team not in TEAMS for team in requested):
        raise ValueError("Unknown club in requested scorecard")
    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = ["bootstrap-static/", f"event/{gw}/live/", f"fixtures/?event={gw}"]
        bootstrap, live, fixtures = list(pool.map(fetch, paths))
    if not bootstrap.get("elements") or not live.get("elements"):
        raise ValueError("Incomplete player/live response; scores cannot be calculated")
    scores = {int(e["id"]): int(e.get("stats", {}).get("total_points", 0)) for e in live.get("elements", [])}
    explanations = flatten_explanations(live["elements"])
    teams, errors = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(load_team, team, gw, scores): team for team in requested}
        for future in as_completed(futures):
            team = futures[future]
            try:
                teams[team] = future.result()
            except Exception as exc:
                errors[team] = str(exc)
    return dict(gw=gw, requested_teams=requested, season=season_key(bootstrap), teams=teams, errors=errors, scores=scores, explanations=explanations,
                player_map=engine.build_player_map(bootstrap), fixtures=fixtures,
                fetched_at=datetime.now(timezone.utc).isoformat(), demo=False)


def captain_team(team, captain_id=None):
    """Explicit manager selection doubles their complete contribution, including hits."""
    members = [{**m, "captain": str(m["id"]) == str(captain_id),
                "contribution": m["points"] * (2 if str(m["id"]) == str(captain_id) else 1)}
               for m in team["managers"]]
    return {**team, "managers": members, "total": sum(m["contribution"] for m in members),
            "confirmed": captain_id is not None}


def member_leaderboard(snapshot):
    """POTW compares each member's net GW score, without IML team doubling."""
    rows = [{"manager_id":m["id"], "manager":m["manager"], "team":team["team"],
             "raw":m["raw"], "hit":m["hit"], "points":m["points"], "chip":m.get("chip")}
            for team in snapshot["teams"].values() for m in team["managers"]]
    rows.sort(key=lambda row:(-row["points"],row["manager"].casefold(),row["team"]))
    previous, rank = None, 0
    for place, row in enumerate(rows, 1):
        if row["points"] != previous:
            rank = place
        row["rank"] = rank
        previous = row["points"]
    return rows


def matchups(snapshot):
    matches = []
    for fixture in snapshot["fixtures"]:
        home = CLUB_ID_TO_TEAM.get(fixture.get("team_h"))
        away = CLUB_ID_TO_TEAM.get(fixture.get("team_a"))
        if not home or not away:
            continue
        status = "Full time" if fixture.get("finished") or fixture.get("finished_provisional") else "Live" if fixture.get("started") else "Upcoming"
        matches.append(dict(home=home, away=away, status=status, kickoff=fixture.get("kickoff_time")))
    return sorted(matches, key=lambda m: m["kickoff"] or "9999")


def race_picks(team):
    result = []
    for manager in team["managers"]:
        meta = manager["meta"]
        multiplier = 2 if manager["captain"] else 1
        picks = [{**p, "count": multiplier * (meta["cap_mul"] if p["element"] == meta["eff_cap_id"] else 1)} for p in meta["picks"]]
        result.append({**meta, "picks": picks})
    return result


def demo_snapshot(gw=8):
    """Clearly labelled fictional scores for previewing without FPL access."""
    teams = {}
    for index, (name, record) in enumerate(TEAMS.items()):
        members = []
        for i, manager in enumerate(record["managers"]):
            points = 37 + ((index * 13 + i * 11) % 44)
            members.append(dict(id=f"demo-{index}-{i}", manager=manager, raw=points,
                                hit=0, points=points, chip=None, meta={"picks": [], "cap_mul": 2, "eff_cap_id": None, "transfer_hit": 0, "active_chip": None}))
        teams[name] = dict(team=name, managers=members)
    ids = list(CLUB_ID_TO_TEAM)
    fixtures = [dict(team_h=ids[i], team_a=ids[i+1], started=i<6, finished=i<2,
                     kickoff_time=f"2026-10-06T{12+i//2:02d}:00:00Z") for i in range(0,20,2)]
    return dict(gw=gw, season=season_key(), teams=teams, errors={}, scores={}, explanations={}, player_map={},
                fixtures=fixtures, fetched_at=datetime.now(timezone.utc).isoformat(), demo=True)
