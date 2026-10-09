"""Escaped HTML presentation for the standalone scorecard."""
from html import escape
from pathlib import Path
from functools import lru_cache
import base64
import json

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap');
:root { --ink:#142623; --muted:#71817b; --paper:#f5f6f1; --line:#dce3db; --green:#123c32; }
.stApp, body { background:var(--paper); color:var(--ink); font-family:'DM Sans',sans-serif; }
.block-container { max-width:1250px; padding-top:2rem; padding-bottom:4rem; }
h1,h2,h3,.display,.score { font-family:'Space Grotesk',sans-serif; letter-spacing:-.04em; }
[data-testid="stSidebar"] { background:#eaf0e8; border-right:1px solid var(--line); }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stMetric"] { border:1px solid var(--line); border-radius:12px; padding:16px; background:white; }
.eyebrow { font-size:11px; font-weight:700; letter-spacing:.16em; text-transform:uppercase; }
.masthead { display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--line); padding-bottom:18px; margin-bottom:28px; }
.brand { display:flex; align-items:center; gap:12px; font-weight:700; }
.brandmark { background:var(--green); color:#d9edb1; border-radius:9px; padding:10px; font-family:'Space Grotesk',sans-serif; letter-spacing:-1px; }
.edition { color:var(--muted); font-size:12px; }
.hero { background:var(--green); color:#f2f7ec; border-radius:20px; padding:32px 38px; position:relative; overflow:hidden; margin-bottom:24px; }
.hero:after { content:''; position:absolute; width:320px; height:320px; border:1px solid #ffffff16; border-radius:50%; right:-100px; top:-110px; pointer-events:none; }
.hero .eyebrow { color:#c4dda3; }
.hero h1 { font-size:46px; line-height:1.12; margin:12px 0; color:#f2f7ec; font-weight:600; }
.hero p { color:#c2d0c8; margin:0; font-size:14px; }
.hero-foot { margin-top:26px; display:flex; gap:28px; font-size:12px; color:#d3dfd6; }
.pill { display:inline-block; padding:5px 9px; border-radius:5px; font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; background:#edf2ea; color:#506c60; }
.pill.live { color:#0b7350; background:#e3f6e9; }
.pill.live:before { content:'● '; font-size:9px; }
.pill.full-time { background:#eef0f4; color:#657080; }
.fixture { border:1px solid var(--line); border-radius:14px; background:white; padding:20px; margin-bottom:14px; }
.fixture-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; color:var(--muted); font-size:11px; }
.fixture-line { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:12px 0; font-size:14px; font-weight:600; }
.fixture-score { font-family:'Space Grotesk',sans-serif; font-size:26px; font-weight:600; }
.club { display:flex; align-items:center; gap:10px; }
.club-crest { width:32px; height:36px; object-fit:contain; flex-shrink:0; filter:drop-shadow(0 2px 3px #0003); }
.scoreboard .club-crest { width:64px; height:70px; display:block; margin:0 auto 12px; }
.fixture-note { border-top:1px solid #edf0e9; padding-top:12px; margin-top:16px; color:var(--muted); font-size:11px; }
.scoreboard { background:var(--green); color:#f2f7ec; border-radius:20px; padding:30px; margin:18px 0 24px; text-align:center; }
.scoreboard-top { color:#bbd0b9; font-size:11px; letter-spacing:.12em; text-transform:uppercase; }
.scoreboard-grid { display:grid; grid-template-columns:1fr auto 1fr; gap:20px; align-items:center; margin:24px 0; }
.score { font-size:76px; line-height:1; font-weight:600; }
.team-name { font-size:16px; margin-bottom:16px; }
.versus { color:#97b2a2; font-size:20px; }
.result-note { color:#d0dec9; font-size:12px; }
.sheet { background:white; border:1px solid var(--line); border-radius:14px; overflow:hidden; margin-bottom:22px; }
.sheet table { width:100%; border-collapse:collapse; font-size:13px; }
.sheet th { font-size:10px; letter-spacing:.09em; text-transform:uppercase; color:#71817b; background:#f9faf6; padding:15px; text-align:left; }
.sheet td { padding:15px; border-top:1px solid #edf0e9; }
.sheet td.number,.sheet th.number { text-align:right; font-variant-numeric:tabular-nums; }
.sheet td.total { font-family:'Space Grotesk',sans-serif; font-size:18px; font-weight:700; }
.sheet tr:first-child td { border-top:0; }
.sheet .rank { color:#95a398; width:36px; }
.member { border:1px solid var(--line); border-radius:10px; background:white; padding:15px; display:flex; justify-content:space-between; align-items:center; margin:8px 0; }
.member-name { font-size:13px; font-weight:600; }
.member-meta { font-size:11px; color:var(--muted); margin-top:5px; }
.member-points { font-size:25px; font-family:'Space Grotesk',sans-serif; font-weight:600; }
.captain { border-left:3px solid #88aa47; }
.smallnote { font-size:12px; color:var(--muted); margin:10px 0 20px; }
@media(max-width:640px) { .hero { padding:24px; } .hero h1 { font-size:32px; } .score { font-size:48px; } .scoreboard { padding:20px; } .team-name { font-size:13px; } .sheet { overflow-x:auto; } .hero-foot { flex-wrap:wrap; gap:12px; } .edition { display:none; } }

/* Plotly supplies a pale active/hover fill; keep replay controls readable. */
.js-plotly-plot .updatemenu-button rect { fill:#1c293a !important; stroke:#40566d !important; }
.js-plotly-plot .updatemenu-button text { fill:#edf4ff !important; }
.js-plotly-plot .updatemenu-button:hover rect { fill:#284638 !important; stroke:#79dba6 !important; }

/* Night-match broadcast treatment. */
:root { --ink:#eaf0f8; --muted:#8b9bb0; --paper:#090e16; --line:#253245; --green:#102a25; }
.stApp,body { color-scheme:dark; }
[data-testid="stSidebar"] { background:#101722; }
.brandmark { background:#143e30; color:#8ee3aa; box-shadow:0 0 24px #22aa6c14; }
.hero { background:radial-gradient(ellipse at 90% 10%,#194538,transparent 55%),linear-gradient(125deg,#132237,#0b151f); border:1px solid #2c4053; }
.hero h1 { font-size:54px; }
.hero:after { border:1px solid #88e3ba18; box-shadow:0 0 0 50px #88e3ba04,0 0 0 100px #88e3ba03; }
.hero .eyebrow { color:#78dbaa; }
.hero p,.hero-foot { color:#9fb4c6; }
.fixture { background:linear-gradient(140deg,#151f2e,#101722); border-color:#283449; transition:transform .2s,border-color .2s; }
.fixture:hover { transform:translateY(-3px); border-color:#41816b; }
.fixture-score { color:#e7f1ff; }
.fixture-note { border-color:#243044; }

.pill { background:#203043; color:#aec3db; }
.pill.live { background:#113c2c; color:#80e2af; }
.pill.live:before { display:inline-block; animation:livepulse 1.8s infinite; }
.pill.full-time { background:#252d3a; color:#acb9cd; }
.scoreboard { background:radial-gradient(ellipse at top,#20362c,transparent 60%),linear-gradient(120deg,#111d2d,#111922); border:1px solid #33445a; position:relative; overflow:hidden; }
.scoreboard:before { content:''; position:absolute; width:1px; height:100%; background:linear-gradient(transparent,#4e665780,transparent); left:50%; top:0; }
.score { font-size:86px; color:#f0f6ff; text-shadow:0 0 35px #9aceb21c; animation:scorearrival .6s ease-out both; }
.scoreboard-top { color:#7fdba8; }
.sheet { background:#111a27; }
.sheet th { background:#182234; color:#9caec6; }
.sheet td { border-color:#223045; }
.sheet tr:hover td { background:#192739; }
.member { background:#141e2b; }
.captain { border-left-color:#85dda5; background:linear-gradient(110deg,#153227,#141e2b); }
[data-testid="stMetric"] { background:#141e2b; }
.commentary-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:16px 0 20px; }
.commentary-card { background:linear-gradient(130deg,#182638,#101a27); border:1px solid #2b3d51; border-radius:12px; padding:20px; animation:scorearrival .45s ease-out both; }
.commentary-card:nth-child(2) { animation-delay:.10s; }
.commentary-card:nth-child(3) { animation-delay:.20s; }
.commentary-kicker { color:#79dba6; font-size:9px; font-weight:700; letter-spacing:.14em; }
.commentary-card h3 { margin:12px 0 9px; font-size:18px !important; color:#eef4ff; }
.commentary-card p { color:#aab8cd; font-size:12px; line-height:1.6; margin:0; }
@keyframes livepulse { 50% { opacity:.25; } }
@keyframes scorearrival { from { opacity:0;transform:translateY(10px); } to { opacity:1;transform:translateY(0); } }
@media(max-width:700px) { .commentary-grid { grid-template-columns:1fr; } .hero h1 { font-size:36px; } .score { font-size:52px; } }
@media(prefers-reduced-motion:reduce) { *,*:before,*:after { animation:none !important; transition:none !important; } }
</style>
"""

def e(value):
    return escape(str(value))


@lru_cache(maxsize=1)
def crest_manifest():
    return json.loads((Path(__file__).parent / "assets" / "crests" / "manifest.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def crest(name):
    record = crest_manifest().get(name)
    if record is None:
        return ""
    asset = Path(__file__).parent / "assets" / "crests" / record["file"]
    encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
    return f'<img class="club-crest" src="data:image/svg+xml;base64,{encoded}" alt="{e(name)} crest" width="32" height="36">'


def masthead(gw):
    return f'<div class="masthead"><div class="brand"><span class="brandmark">IML</span><span>THE SCORECARD</span></div><span class="edition">MATCHDAY EDITION / GAMEWEEK {gw:02d}</span></div>'


def hero(gw, teams, fixtures, demo=False):
    return f'<div class="hero"><div class="eyebrow">{"Design preview · fictional scores" if demo else "The IML matchday"}</div><h1>TOTW race.<br>Under the lights.</h1><p>Big swings. Captain calls. Every point has a story.</p><div class="hero-foot"><span>GAMEWEEK {gw:02d}</span><span>{teams} CLUBS LOADED</span><span>{fixtures} FIXTURES</span></div></div>'


def fixture_card(match, home, away):
    confirmed = home and away and home['confirmed'] and away['confirmed']
    status = match['status']
    lines = ''
    for name, team in [(match['home'],home),(match['away'],away)]:
        score = team['total'] if team else '—'
        lines += f'<div class="fixture-line"><span class="club">{crest(name)}{e(name)}</span><span class="fixture-score">{score}</span></div>'
    note = 'Captain selections applied' if confirmed else 'Provisional · captain selections incomplete'
    if not home or not away:
        note = 'Score unavailable · incomplete club data'
    return f'<div class="fixture"><div class="fixture-top"><span class="pill {e(status.lower().replace(" ","-"))}">{e(status)}</span><span>GW SCORE</span></div>{lines}<div class="fixture-note">{note}</div></div>'


def scoreboard(home, away, status):
    difference = home['total'] - away['total']
    note = f"{home['team'] if difference > 0 else away['team']} leads by {abs(difference)} points" if difference else 'Scores are level'
    return f'<div class="scoreboard"><div class="scoreboard-top">{e(status)} · gameweek score</div><div class="scoreboard-grid"><div>{crest(home["team"])}<div class="team-name">{e(home["team"])}</div><div class="score">{home["total"]}</div></div><div class="versus">:</div><div>{crest(away["team"])}<div class="team-name">{e(away["team"])}</div><div class="score">{away["total"]}</div></div></div><div class="result-note">{e(note)}</div></div>'


def league_sheet(teams):
    rows = ''
    for rank, team in enumerate(sorted(teams,key=lambda t:(-t['total'],t['team'])),1):
        captain = next((m['manager'] for m in team['managers'] if m['captain']), 'Not selected')
        hits = sum(m['hit'] * (2 if m['captain'] else 1) for m in team['managers'])
        rows += f'<tr><td class="rank">{rank:02d}</td><td><span class="club">{crest(team["team"])}<strong>{e(team["team"])}</strong></span></td><td>{e(captain)}</td><td class="number">{hits}</td><td class="number total">{team["total"]}</td><td><span class="pill">{"Selected" if team["confirmed"] else "Provisional"}</span></td></tr>'
    return f'<div class="sheet"><table><thead><tr><th>#</th><th>Club</th><th>IML captain</th><th class="number">Hits</th><th class="number">GW points</th><th>Captain status</th></tr></thead><tbody>{rows}</tbody></table></div>'


def member_cards(team):
    cards=''
    for m in team['managers']:
        meta=f"{m['points']} pts after hits" + (' · IML captain ×2' if m['captain'] else '') + (f" · {m['chip'].upper()}" if m['chip'] else '')
        cards+=f'<div class="member {"captain" if m["captain"] else ""}"><div><div class="member-name">{e(m["manager"])}</div><div class="member-meta">{e(meta)}</div></div><div class="member-points">{m["contribution"]}</div></div>'
    return cards


def potw_hero(gw, rows, demo=False):
    top = rows[0]["points"] if rows else 0
    leaders = [row for row in rows if row["points"] == top]
    if not leaders:
        title, detail = "The POTW race awaits.", "Member scores will appear when club squads are available."
    elif len(leaders) > 1:
        title = "Shared lead.<br>Unshared bragging rights."
        detail = f"{len(leaders)} members are tied on {top} points. The group chat may need a referee."
    else:
        title = "One player.<br>All the bragging rights."
        gap = top - rows[1]["points"] if len(rows) > 1 else None
        detail = f"{leaders[0]['manager']} leads on {top} points." + (f" A {gap}-point cushion over second place." if gap is not None else "")
    return f'<div class="hero"><div class="eyebrow">{"Design preview · fictional scores" if demo else "Player of the week / IML members"}</div><h1>{title}</h1><p>{e(detail)}</p><div class="hero-foot"><span>GAMEWEEK {gw:02d}</span><span>{len(rows)} MEMBERS LOADED</span><span>NET GW POINTS</span></div></div>'


def member_spotlights(rows):
    cards = ""
    for row in rows[:3]:
        cards += f'<div class="commentary-card"><div class="commentary-kicker">POTW RACE / RANK {row["rank"]:02d}</div><h3>{e(row["manager"])}</h3><span class="club">{crest(row["team"])}{e(row["team"])}</span><div class="member-points" style="margin-top:16px">{row["points"]} <span style="font-size:12px;color:var(--muted)">GW pts</span></div></div>'
    return f'<div class="commentary-grid">{cards}</div>'


def member_sheet(rows):
    body = ""
    for row in rows:
        body += f'<tr><td class="rank">{row["rank"]:02d}</td><td><strong>{e(row["manager"])}</strong></td><td><span class="club">{crest(row["team"])}{e(row["team"])}</span></td><td class="number">{row["raw"]}</td><td class="number">{row["hit"]}</td><td>{e((row["chip"] or "—").upper())}</td><td class="number total">{row["points"]}</td></tr>'
    return f'<div class="sheet"><table><thead><tr><th>Rank</th><th>IML member</th><th>Club</th><th class="number">Raw</th><th class="number">Hit</th><th>Chip</th><th class="number">GW points</th></tr></thead><tbody>{body}</tbody></table></div>'
