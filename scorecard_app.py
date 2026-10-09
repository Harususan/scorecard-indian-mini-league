#!/usr/bin/env python3
"""Standalone IML matchday scorecards. Run: streamlit run scorecard_app.py"""
from datetime import datetime
import pandas as pd
import streamlit as st
import fpl_h2h_v14 as engine
import scorecard_data as data
import scorecard_views as view
import scorecard_race as race
from scorecard_browser import captain_picker, validate_choices

st.set_page_config(page_title="IML · The Scorecard", page_icon="⚽", layout="wide")
st.markdown(view.CSS, unsafe_allow_html=True)

@st.cache_data(ttl=60, max_entries=32, show_spinner=False)
def snapshot(gw, team_names=None):
    return data.load_snapshot(gw, team_names)

@st.cache_data(ttl=3600, show_spinner=False)
def bootstrap():
    return data.fetch("bootstrap-static/")

@st.cache_data(ttl=300, max_entries=16, show_spinner=False)
def race_figure(events, home, away, replay):
    return race.build_race_figure(events, home, away, replay=replay)


def html(content):
    st.markdown(content, unsafe_allow_html=True)


def render_race(snap, home, away):
    st.subheader("The gameweek race")
    if snap["demo"]:
        events = race.demo_events(home["total"], away["total"])
        st.caption("Illustrative race and commentary · fictional demo events.")
    else:
        events = engine.build_intra_gw_timeline(data.race_picks(home), data.race_picks(away),
            snap["player_map"], snap["explanations"], snap["scores"], data.fixture_kickoffs(snap["fixtures"]))
        st.caption("Player points only; transfer hits are included in the scorecard above. Replay follows engine ordering by fixture and stat, not exact event timestamps.")
    if not events:
        st.info("The race appears once scoring events are available.")
        return
    html(race.commentary_html(race.race_insights(events,home["team"],away["team"])))
    replay = st.toggle("Enable gameweek replay", value=False, key="enable_race_replay")
    st.plotly_chart(race_figure(events,home["team"],away["team"],replay),width="stretch",config={"displayModeBar":False})
    st.caption("Enable replay, then press Replay to watch the gap unfold. Stars mark the six largest player swings of at least 10 points; gold favours the home side, pink the away side.")


def render_match(snap, teams, matches, selected_match=None):
    if not matches:
        st.info("No mapped IML fixtures for this gameweek.")
        return
    if selected_match is None:
        selected = st.selectbox("Fixture", range(len(matches)),
            format_func=lambda i:f"{matches[i]['home']} vs {matches[i]['away']}")
        selected_match = matches[selected]
    match = selected_match
    home, away = teams.get(match["home"]), teams.get(match["away"])
    if not home or not away:
        st.warning("Both club rosters must load before this match can be scored. See data coverage below.")
        return
    html(view.scoreboard(home,away,match["status"]))
    if not home["confirmed"] or not away["confirmed"]:
        st.info("Provisional score: select both IML captain managers in the sidebar to apply their ×2 contributions.")
    render_race(snap,home,away)
    st.subheader("The managers behind the score")
    left,right=st.columns(2)
    for col,team in [(left,home),(right,away)]:
        with col:
            st.subheader(team["team"])
            html(view.member_cards(team))
    with st.expander("Players making the difference"):
        if snap["demo"]:
            st.caption("Real player contributions appear when live FPL data is loaded.")
        else:
            rows=engine.build_differential(data.race_picks(home),data.race_picks(away),snap["player_map"],snap["scores"])
            if rows:
                df=pd.DataFrame(rows).sort_values("point_swing",key=lambda s:s.abs(),ascending=False)
                df=df[["name","position","live_pts","point_swing"]].rename(columns={"name":"Player","position":"Pos","live_pts":"GW points","point_swing":"Swing"})
                st.dataframe(df,hide_index=True,width="stretch")
                st.caption(f"Positive swing favours {home['team']}; negative favours {away['team']}.")
            else:
                st.caption("No differential player points yet.")


def render_potw(snap):
    rows = data.member_leaderboard(snap)
    html(view.potw_hero(int(snap["gw"]), rows, snap["demo"]))
    html(view.member_spotlights(rows))
    st.subheader("POTW race")
    st.caption("Individual IML member scores after transfer hits. FPL captaincy and chips count; IML captain doubling applies only to the team race. Equal scores share a rank.")
    search_col, club_col = st.columns([2,1])
    query = search_col.text_input("Find a member", placeholder="Search by name", key="potw_search").strip().casefold()
    club = club_col.selectbox("Club", ["All clubs"] + sorted(snap["teams"]), key="potw_club")
    filtered = [row for row in rows if (not query or query in row["manager"].casefold())
                and (club == "All clubs" or row["team"] == club)]
    if filtered:
        html(view.member_sheet(filtered))
        st.caption(f"{len(filtered)} of {len(rows)} members shown. Ranks refer to the full POTW race.")
    else:
        st.info("No members match these filters.")
    return rows


with st.sidebar:
    st.markdown("### IML / The Scorecard")
    demo=st.toggle("Design preview",value=False,help="Uses clearly labelled fictional scores; no FPL requests.")
    default=8
    if not demo:
        try:
            default=data.current_gw(bootstrap())
        except Exception:
            st.warning("FPL is unavailable. Enable Design preview to explore the scorecard, or retry live data later.")
    gw=st.number_input("Gameweek",min_value=1,max_value=38,value=default,step=1)
    screen=st.radio("View",["TOTW race","POTW race","Match scorecard"])
    if st.button("Refresh scores",width="stretch"):
        snapshot.clear()
        data.clear_endpoint_cache()
    with st.expander("Data options"):
        st.caption("Rosters are reused for an hour and squads for five minutes. Refresh scores reloads live points; use this button if a roster or squad needs an immediate reload.")
        if st.button("Reload squads and rosters", width="stretch"):
            snapshot.clear()
            bootstrap.clear()
            data.clear_endpoint_cache(include_squads=True)
    st.divider()
    st.caption("Four managers per club. The selected IML captain contributes twice their net score.")

selected_match = None
requested_teams = None
try:
    if screen == "Match scorecard":
        fixtures = data.demo_snapshot(int(gw))["fixtures"] if demo else data.fetch(f"fixtures/?event={int(gw)}")
        available = data.matchups({"fixtures": fixtures})
        if not available:
            st.info("No mapped IML fixtures for this gameweek.")
            st.stop()
        selected = st.selectbox("Fixture", range(len(available)),
            format_func=lambda i:f"{available[i]['home']} vs {available[i]['away']}", key=f"match_fixture:{gw}")
        selected_match = available[selected]
        requested_teams = (selected_match["home"], selected_match["away"])
    with st.spinner("Preparing the matchday scorecard…"):
        snap=data.demo_snapshot(int(gw)) if demo else snapshot(int(gw), requested_teams)
except Exception as exc:
    st.error(f"Live scorecard could not load: {exc}")
    st.info("Enable Design preview in the sidebar to explore the layout with fictional scores.")
    st.stop()

if not snap["teams"]:
    st.error("No complete club squads loaded. Try another gameweek or refresh scores.")
    with st.expander("Data coverage"):
        st.json(snap["errors"])
    st.stop()

current = {}
if screen != "POTW race":
    selection_mode = "demo" if demo else "live"
    season = snap.get("season", data.season_key())
    selection_key = f"{season}:{selection_mode}:{gw}"
    with st.sidebar:
        with st.expander("IML captain selections"):
            st.caption("Remembered in this browser for each season and gameweek. Choose your league's submitted captains.")
            preferences = captain_picker(snap["teams"], selection_key)
    current = validate_choices(preferences, snap["teams"], selection_key)
    if current is None:
        st.caption("Restoring captain choices from this browser…")
        st.stop()
    if preferences.get("error"):
        st.warning(preferences["error"])

teams={name:data.captain_team(team,current.get(name)) for name,team in snap["teams"].items()}
matches=data.matchups(snap)
html(view.masthead(int(gw)))
if demo:
    st.warning("DESIGN PREVIEW — all scores and race events below are fictional.")

if screen=="TOTW race":
    html(view.hero(int(gw),len(teams),len(matches),demo))
    left,right=st.columns([3,2])
    with left:
        st.subheader("TOTW race")
    with right:
        st.caption(f"{sum(t['confirmed'] for t in teams.values())}/{len(teams)} captain selections applied")
    st.caption("Sorted by this gameweek's club points. This is a weekly scorecard, not the season H2H standings.")
    html(view.league_sheet(list(teams.values())))
    st.subheader("Around the grounds")
    if not matches:
        st.info("No mapped IML fixtures for this gameweek.")
    else:
        columns=st.columns(2)
        for i,match in enumerate(matches):
            with columns[i%2]:
                html(view.fixture_card(match,teams.get(match["home"]),teams.get(match["away"])))
elif screen=="POTW race":
    potw_rows = render_potw(snap)
else:
    render_match(snap,teams,matches,selected_match)

st.divider()
st.caption(f"{'Demo generated' if demo else 'Data fetched'} {datetime.fromisoformat(snap['fetched_at']).strftime('%d %b %Y · %H:%M UTC')} · {len(teams)}/{len(snap.get('requested_teams', data.TEAMS))} complete club rosters")
if snap["errors"]:
    st.warning(f"{len(snap['errors'])} clubs could not load. Their scores are excluded; no zero scores have been substituted.")
    with st.expander("Data coverage"):
        for name,message in sorted(snap["errors"].items()):
            st.write(f"{name}: {message}")
if screen == "POTW race":
    export = pd.DataFrame([{"Rank":r["rank"], "Member":r["manager"], "Club":r["team"],
        "GW":int(gw), "Raw":r["raw"], "Hit":r["hit"], "Points":r["points"],
        "Chip":r["chip"], "Demo":demo} for r in potw_rows])
    export_race = "POTW"
else:
    export = pd.DataFrame([{"Club":t["team"],"GW":int(gw),"Points":t["total"],"Captain selected":t["confirmed"],"Demo":demo} for t in sorted(teams.values(),key=lambda t:-t["total"])])
    export_race = "TOTW"
st.download_button(f"Download {export_race} race",export.to_csv(index=False),file_name=f"iml_{'demo_' if demo else ''}gw{gw}_{export_race.lower()}.csv",mime="text/csv")
