#!/usr/bin/env python3
"""
IML Scorecards v16 — performance and reliability pass.
Run: streamlit run streamlit_app.py
Requires fpl_h2h_v14.py alongside this file. The engine's reporting and scorecard
helpers are retained; endpoint caching, analytics and projections live here.
External projections use canonical CSV player_id,gw,xpts rows (GW totals).
"""

import contextlib
import io
import json
import math
import itertools
import os
import time
import sqlite3
import threading
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from collections import defaultdict
from types import SimpleNamespace

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import fpl_h2h_v14 as engine

st.set_page_config(page_title="IML Scorecards", page_icon="⚽", layout="wide")

# Restrained surfaces and spacing; colours follow Streamlit's selected theme.
st.markdown("""<style>
.block-container { max-width: 1180px; padding-top: 2rem; padding-bottom: 3rem; }
[data-testid="stSidebar"] { border-right: 1px solid color-mix(in srgb, var(--text-color) 10%, transparent); }
h1 { font-size: 2rem !important; letter-spacing: -.04em; }
h2 { font-size: 1.35rem !important; }
h3 { font-size: 1.15rem !important; }
[data-testid="stMetric"] { padding: 1rem; border: 1px solid color-mix(in srgb, var(--text-color) 12%, transparent); border-radius: 8px; }
[data-testid="stMetricLabel"] { opacity: .75; }
[data-testid="stMetricValue"] { font-size: 1.8rem; }
[data-testid="stHorizontalBlock"] { gap: 1rem; }
</style>""", unsafe_allow_html=True)

CHIP_STYLE = {
    "available": "background-color: #e6f7f5; color: #0f766e;",
    "used":      "background-color: #eef0f4; color: #64748b; text-decoration: line-through;",
    "active":    "background-color: #e0e7ff; color: #4338ca; font-weight: 700;",
}

# ── League registry ────────────────────────────────────────────────────────────
# Each "team" here is a 4-manager classic mini-league named after its PL club.
# Selecting a team in the sidebar resolves straight to its League ID and
# manager roster — no need to type/paste IDs or guess a captain's name.
from iml_registry import TEAMS, CLUB_ID_TO_TEAM

TEAM_NAMES = sorted(TEAMS)

# ── Cached wrappers around the engine's network calls ────────────────────────
# (kept separate from engine.py itself so the CLI script stays untouched)

@st.cache_data(ttl=3600, show_spinner=False)
def cached_bootstrap():
    return _api_json("bootstrap-static/", ttl=3600)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_player_map(bootstrap):
    return engine.build_player_map(bootstrap)


@st.cache_data(ttl=60, show_spinner=False)
def cached_live_scores(gw: int):
    return engine.get_live_scores(gw)


@st.cache_data(ttl=300, show_spinner=False)
def cached_league_name(league_id: str):
    return cached_league_info(league_id)["name"]


@st.cache_data(ttl=300, show_spinner=False)
def cached_league_managers(league_id: str):
    return cached_league_info(league_id)["managers"]


@st.cache_data(ttl=120, show_spinner=False)
def cached_team_picks(manager_ids, cap_index, gw, team_label, _player_map, managers):
    data_by_id = cached_all_current_picks(gw, tuple(map(str, manager_ids)))
    if len(data_by_id) != len(manager_ids):
        raise ValueError("Incomplete matchup squads; retry after the API recovers")
    out = []
    for i, mid in enumerate(manager_ids):
        data = data_by_id[str(mid)]
        if not data.get("picks"):
            raise ValueError("Manager picks are not published for this gameweek")
        picks = [dict(p) for p in data["picks"]]
        for pick in picks:
            pick.setdefault("is_captain", False)
            pick.setdefault("is_vice_captain", False)
        chip = data.get("active_chip")
        effective = engine.resolve_effective_captain(picks, chip)
        cap_id = effective["element"] if effective else None
        cap_mul = 3 if chip == "3xc" else 2
        team_mul = 2 if i == cap_index else 1
        for pick in picks:
            pick["count"] = team_mul * (cap_mul if pick["element"] == cap_id else 1)
        out.append({"picks": picks, "transfer_hit": -abs(data.get("entry_history", {}).get("event_transfers_cost", 0)),
                    "active_chip": chip, "manager_id": str(mid), "eff_cap_id": cap_id,
                    "cap_mul": cap_mul, "team_multiplier": team_mul})
    return out


@st.cache_data(ttl=120, show_spinner=False)
def cached_fixture_kickoffs(gw: int):
    return engine.get_fixture_kickoffs(gw)


@st.cache_data(ttl=60, show_spinner=False)
def cached_raw_fixtures(gw: int):
    """One shared fetch of raw /fixtures/?event=<gw> data, reused for both the
    per-fixture tab list and the live/finished/upcoming status lookups."""
    return [f for f in cached_fixtures_all() if f.get("event") == gw]


def _fixture_status(f: dict) -> str:
    """finished_provisional flips true right at full-time, well before
    'finished' (which waits on official bonus-point confirmation, sometimes
    ~1hr+ after the final whistle). Treating either as 'finished' is what
    actually matches reality — otherwise players from ended matches sit in
    'live' indefinitely and 'finished' stays empty."""
    if f.get("finished") or f.get("finished_provisional"):
        return "finished"
    if f.get("started"):
        return "live"
    return "upcoming"


def fixture_status_map(gw: int):
    """club_id -> 'finished' | 'live' | 'upcoming'."""
    status = {}
    for f in cached_raw_fixtures(gw):
        s = _fixture_status(f)
        for tid in (f.get("team_h"), f.get("team_a")):
            if tid:
                previous = status.get(tid)
                priority = {"finished": 0, "upcoming": 1, "live": 2}
                if previous is None or priority[s] > priority[previous]:
                    status[tid] = s
    return status


def gw_matchups(gw: int):
    """Every fixture this GW where both clubs map to one of our 20 registered
    teams, sorted by kickoff time. Each entry: team_a (home), team_b (away),
    kickoff (ISO str), status."""
    matchups = []
    for f in cached_raw_fixtures(gw):
        team_a_name = CLUB_ID_TO_TEAM.get(f.get("team_h"))
        team_b_name = CLUB_ID_TO_TEAM.get(f.get("team_a"))
        if not team_a_name or not team_b_name:
            continue
        matchups.append({
            "team_a": team_a_name, "team_b": team_b_name,
            "kickoff": f.get("kickoff_time") or "", "status": _fixture_status(f),
        })
    matchups.sort(key=lambda m: m["kickoff"] or "9999")
    return matchups


TEAM_TO_CLUB_ID = {v: k for k, v in CLUB_ID_TO_TEAM.items()}


def cached_histories(manager_ids):
    return cached_all_manager_histories(tuple(manager_ids))





# ── Pipeline: mirrors main() in fpl_h2h_v14.py, minus argparse/printing ──────

def run_pipeline(gw, league_a, league_b, cap_a_query, cap_b_query, no_live):
    """Runs the full fetch + compute pipeline. Raises SystemExit (caught by
    the caller) on bad captain names, same as the CLI does."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bootstrap  = cached_bootstrap()
        player_map = cached_player_map(bootstrap)

        if no_live:
            live_scores, live_explain = {}, {}
        else:
            live_scores, _, live_explain = cached_live_scores(gw)

        league_name_a = cached_league_name(league_a)
        managers_a    = cached_league_managers(league_a)
        league_name_b = cached_league_name(league_b)
        managers_b    = cached_league_managers(league_b)

        cap_a_idx = engine.resolve_cap_index(cap_a_query, managers_a, "A")
        cap_b_idx = engine.resolve_cap_index(cap_b_query, managers_b, "B")

        team_a_ids = [m["id"] for m in managers_a]
        team_b_ids = [m["id"] for m in managers_b]

        picks_a = cached_team_picks(team_a_ids, cap_a_idx, gw, "A", player_map, managers_a)
        picks_b = cached_team_picks(team_b_ids, cap_b_idx, gw, "B", player_map, managers_b)

        histories_a  = cached_histories(team_a_ids)
        histories_b  = cached_histories(team_b_ids)
        chips_hist_a = {mid: h["chips"] for mid, h in histories_a.items()}
        chips_hist_b = {mid: h["chips"] for mid, h in histories_b.items()}

        rows = engine.build_differential(picks_a, picks_b, player_map, live_scores)

    return dict(
        gw=gw, player_map=player_map, live_scores=live_scores, live_explain=live_explain,
        league_name_a=league_name_a, league_name_b=league_name_b,
        managers_a=managers_a, managers_b=managers_b,
        cap_a_idx=cap_a_idx, cap_b_idx=cap_b_idx,
        team_a_ids=team_a_ids, team_b_ids=team_b_ids,
        picks_a=picks_a, picks_b=picks_b,
        histories_a=histories_a, histories_b=histories_b,
        chips_hist_a=chips_hist_a, chips_hist_b=chips_hist_b,
        rows=rows,
    )


def team_total(picks_list, cap_idx, live_scores):
    total = 0
    for i, mgr in enumerate(picks_list):
        _, _, final = engine.manager_final_score(mgr, live_scores)
        total += final * 2 if i == cap_idx else final
    return total


# ── Shared constants used inside the per-fixture dashboard ────────────────────
STATUS_LABEL    = {"finished": "✅ FT", "live": "🔴 LIVE", "upcoming": "⏳ Upcoming", "unknown": "❔ —"}
STATUS_ORDER    = ["live", "upcoming", "finished", "unknown"]
NAME_TO_TEAM_ID = {v: k for k, v in engine.CLUBS.items()}  # engine club name -> engine club id

STAT_ICON = {
    "goals_scored": "⚽", "assists": "🅰", "clean_sheets": "🛡",
    "saves": "🧤", "penalties_saved": "🧤", "bonus": "⭐", "minutes": "⏱",
    "goals_conceded": "❌", "yellow_cards": "🟨", "red_cards": "🟥",
    "own_goals": "😬", "penalties_missed": "❌", "bps": "📊",
}
HEAVY_THRESHOLD = 10  # aggregate per-player swing (pts) to count as "heavy differential"


# ── Full analysis dashboard for one fixture (Scoreboard, GW Race, ...) ────────
def render_dashboard(res: dict, settings: dict, key_prefix: str) -> None:
    gw            = res["gw"]
    player_map    = res["player_map"]
    live_scores   = res["live_scores"]
    league_name_a = res["league_name_a"]
    league_name_b = res["league_name_b"]
    managers_a, managers_b = res["managers_a"], res["managers_b"]
    cap_a_idx, cap_b_idx   = res["cap_a_idx"], res["cap_b_idx"]
    picks_a, picks_b       = res["picks_a"], res["picks_b"]
    chips_hist_a, chips_hist_b = res["chips_hist_a"], res["chips_hist_b"]
    histories_a, histories_b   = res["histories_a"], res["histories_b"]
    rows          = res["rows"]

    total_a = team_total(picks_a, cap_a_idx, live_scores)
    total_b = team_total(picks_b, cap_b_idx, live_scores)

    st.subheader(f"{league_name_a} vs {league_name_b}")
    c1, c2, c3 = st.columns([2, 1, 2])
    c1.metric(league_name_a, total_a)
    diff = total_a - total_b
    c2.metric("Points apart", abs(diff))
    c3.metric(league_name_b, total_b)
    st.caption(f"{league_name_a if diff > 0 else league_name_b} leads by {abs(diff)} points." if diff else "The teams are level.")

    tab_names = ["Overview", "Gameweek race", "Key players", "Team details"]
    selected_view = st.radio("Matchup view", tab_names, horizontal=True, key=f"{key_prefix}_view")
    detail_view = None
    if selected_view == "Team details":
        details = ["Recent form"]
        if not settings["no_chips"]:
            details.append("Chips")
        if not settings["no_summary"]:
            details.append("Squads")
        detail_view = st.selectbox("Team details", details, key=f"{key_prefix}_details")

    # ── Team totals ─────────────────────────────────────────────────────────
    if selected_view == "Overview":
        for label, picks_list, cap_idx, managers, league_nm, grand_total in (
            ("A", picks_a, cap_a_idx, managers_a, league_name_a, total_a),
            ("B", picks_b, cap_b_idx, managers_b, league_name_b, total_b),
        ):
            st.markdown(f"##### {league_nm} (Team {label})")
            recs = []
            for i, mgr in enumerate(picks_list):
                raw, hit, final = engine.manager_final_score(mgr, live_scores)
                is_cap = (i == cap_idx)
                contribution = final * 2 if is_cap else final
                mgr_label = engine._mgr_label(managers, i, mgr["manager_id"])
                recs.append({
                    "Manager": mgr_label, "Raw": raw, "Hit": hit, "Final": final,
                    "H2H Cap": "★ x2" if is_cap else "—", "Contribution": contribution,
                })
            df = pd.DataFrame(recs)
            st.dataframe(df, hide_index=True, width="stretch")
            st.caption(f"Team {label} total: **{grand_total}** pts")
            st.write("")

        with st.expander("Export scorecard"):
            dl_col1, dl_col2 = st.columns(2)

            if st.checkbox("Prepare PDF export", key=f"{key_prefix}_prepare_pdf"):
                pdf_args = SimpleNamespace(
                    gw=gw, team_a=res["team_a_ids"], team_b=res["team_b_ids"],
                    league_name_a=league_name_a, league_name_b=league_name_b,
                    no_summary=settings["no_summary"],
                )
                pdf_buf = io.BytesIO()
                try:
                    engine.generate_pdf_report(
                        pdf_buf, pdf_args, picks_a, picks_b, player_map, live_scores, rows,
                        cap_a_idx, cap_b_idx, managers_a=managers_a, managers_b=managers_b,
                        chips_hist_a=None if settings["no_chips"] else chips_hist_a,
                        chips_hist_b=None if settings["no_chips"] else chips_hist_b,
                    )
                    dl_col1.download_button(
                        "Download PDF report", data=pdf_buf.getvalue(),
                        file_name=f"fpl_h2h_gw{gw}_{key_prefix}.pdf", mime="application/pdf",
                        width="stretch", key=f"{key_prefix}_dl_pdf",
                    )
                except (Exception, SystemExit) as e:
                    dl_col1.warning(str(e))

            json_payload = json.dumps({
                "gw": gw, "league_a": league_name_a, "league_b": league_name_b,
                "total_a": total_a, "total_b": total_b, "differential": rows,
            }, indent=2)
            dl_col2.download_button(
                "Download JSON data", data=json_payload,
                file_name=f"fpl_h2h_gw{gw}_{key_prefix}.json", mime="application/json",
                width="stretch", key=f"{key_prefix}_dl_json",
            )


    # ── Differential & swing ────────────────────────────────────────────────
    if selected_view == "Key players":
        st.markdown(f"##### Ownership differential & point swing — GW{gw}")

        fixture_status = fixture_status_map(gw)
        for r in rows:
            team_id = NAME_TO_TEAM_ID.get(r["club"])
            r["_status"] = fixture_status.get(team_id, "unknown") if team_id else "unknown"

        counts = {s: sum(1 for r in rows if r["_status"] == s) for s in STATUS_ORDER}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🔴 Live", counts["live"])
        c2.metric("⏳ Upcoming", counts["upcoming"])
        c3.metric("✅ Finished", counts["finished"])
        if counts["unknown"]:
            c4.metric("❔ Unknown", counts["unknown"])

        status_filter = st.radio(
            "Filter by match status", ["All", "🔴 Live", "⏳ Upcoming", "✅ Finished"],
            horizontal=True, label_visibility="collapsed", key=f"{key_prefix}_status_filter",
        )
        status_key = {"🔴 Live": "live", "⏳ Upcoming": "upcoming", "✅ Finished": "finished"}.get(status_filter)
        filtered_rows = rows if status_key is None else [r for r in rows if r["_status"] == status_key]

        if not filtered_rows:
            st.caption("No players in this status right now.")
        else:
            df_rows = pd.DataFrame(filtered_rows)[
                ["name", "position", "club", "_status", "A", "B", "diff", "live_pts", "point_swing"]
            ]
            df_rows.columns = ["Player", "Pos", "Club", "Status", "A", "B", "Diff", "GW Pts", "Swing"]
            df_rows["Status"] = df_rows["Status"].map(STATUS_LABEL)

            def _swing_style(v):
                if v > 0:
                    return "color: #1a8754; font-weight: 600"
                if v < 0:
                    return "color: #c0392b; font-weight: 600"
                return "color: #6b7280"

            st.dataframe(
                df_rows.style.map(_swing_style, subset=["Swing"]),
                hide_index=True, width="stretch", height=420,
            )
        st.caption(
            "Count: H2H captain + FPL captain = x4 · either alone = x2 · normal = x1 · "
            "Swing = diff × GW pts. Positive favours Team A, negative favours Team B. "
            "Status reflects each player's club fixture this gameweek."
        )

        a_rows = [r for r in rows if r["point_swing"] > 0]
        b_rows = [r for r in rows if r["point_swing"] < 0]
        player_net = sum(r["point_swing"] for r in rows)
        hit_a = sum(mgr["transfer_hit"] for mgr in picks_a)
        hit_b = sum(mgr["transfer_hit"] for mgr in picks_b)
        net_h2h = player_net + (hit_a - hit_b)

        st.markdown("###### Swing summary")
        if a_rows:
            st.write(f"**{league_name_a} benefited from:** " +
                     ", ".join(f"{r['name']} (+{r['point_swing']} | {r['live_pts']}pts)" for r in a_rows))
        if b_rows:
            st.write(f"**{league_name_b} benefited from:** " +
                     ", ".join(f"{r['name']} ({r['point_swing']} | {r['live_pts']}pts)" for r in b_rows))
        net_label = (f"{league_name_a} +{player_net} pts" if player_net > 0 else
                     f"{league_name_b} +{abs(player_net)} pts" if player_net < 0 else "Balanced")
        net_h2h_label = (f"{league_name_a} +{net_h2h} pts" if net_h2h > 0 else
                         f"{league_name_b} +{abs(net_h2h)} pts" if net_h2h < 0 else "Level")
        st.write(f"**Net player points swing:** {net_label}")
        st.write(f"**Net H2H swing (incl. transfer hits):** {net_h2h_label}")

    # ── GW Race (in-gameweek live race) ─────────────────────────────────────
    if selected_view == "Gameweek race":
        st.markdown(f"##### Gameweek {gw} race")
        st.caption(f"{league_name_a} vs {league_name_b} · every scoring event this gameweek, in order")

        fixture_ko = cached_fixture_kickoffs(gw)
        timeline = engine.build_intra_gw_timeline(
            picks_a, picks_b, player_map, res["live_explain"], live_scores, fixture_ko,
        )

        if not timeline:
            st.info(
                "No live scoring events yet for this gameweek. This fills in once matches "
                "kick off (or turn off **Skip live scores** in the sidebar if that's enabled)."
            )
        else:
            x       = list(range(len(timeline) + 1))
            sc_a    = [0] + [e["score_a_after"] for e in timeline]
            sc_b    = [0] + [e["score_b_after"] for e in timeline]
            labels  = ["Kickoff"] + [
                f'{STAT_ICON.get(e["stat"], "")} {e["name"]} · {engine.STAT_LABELS.get(e["stat"], e["stat"])} '
                f'({"+" if e["raw_points"] >= 0 else ""}{e["raw_points"]}pts)'
                for e in timeline
            ]

            player_swing = {}
            for idx, e in enumerate(timeline, start=1):
                pid = e["player_id"]
                slot = player_swing.setdefault(pid, {"name": e["name"], "swing": 0, "last_idx": idx})
                slot["swing"] += e["pts_swing_a"] - e["pts_swing_b"]
                slot["last_idx"] = idx
            heavy = {pid: v for pid, v in player_swing.items() if abs(v["swing"]) >= HEAVY_THRESHOLD}

            fig2 = go.Figure()
            fig2.add_scatter(x=x, y=sc_a, mode="lines+markers", line=dict(shape="hv", color="#475569", width=2.5),
                              marker=dict(size=4), name=league_name_a, hovertext=labels,
                              hovertemplate="%{hovertext}<br>%{y} pts<extra></extra>")
            fig2.add_scatter(x=x, y=sc_b, mode="lines+markers", line=dict(shape="hv", color="#0f766e", width=2.5),
                              marker=dict(size=4), name=league_name_b, hovertext=labels,
                              hovertemplate="%{hovertext}<br>%{y} pts<extra></extra>")
            fig2.update_layout(
                height=400, margin=dict(l=0, r=0, t=20, b=0),
                xaxis=dict(title="Scoring events (chronological)", showticklabels=False),
                yaxis_title="Cumulative GW points",
                legend=dict(orientation="h", y=1.12, x=0),
            )
            st.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})

            st.markdown(f"###### Biggest contributions (swing ≥ {HEAVY_THRESHOLD} pts)")
            if heavy:
                for pid, v in sorted(heavy.items(), key=lambda kv: -abs(kv[1]["swing"])):
                    side  = league_name_a if v["swing"] > 0 else league_name_b
                    emoji = "🟢" if v["swing"] > 0 else "🔵"
                    st.write(f'{emoji} **{v["name"]}** has swung **{abs(v["swing"])} pts** toward **{side}** so far')
            else:
                st.caption("No single player has swung this GW by 10+ points yet — tight race.")

            with st.expander("Full event-by-event ticker"):
                df_evt = pd.DataFrame([{
                    "Fixture":  f'{e.get("home","?")} vs {e.get("away","?")}',
                    "Player":   e["name"],
                    "Stat":     engine.STAT_LABELS.get(e["stat"], e["stat"]),
                    "Pts":      e["raw_points"],
                    f"{league_name_a} x": e["count_a"],
                    f"{league_name_b} x": e["count_b"],
                    "Score after": f'{e["score_a_after"]} – {e["score_b_after"]}',
                } for e in timeline])
                st.dataframe(df_evt, hide_index=True, width="stretch", height=420)

    # ── Season Trend (past-gameweek analysis) ───────────────────────────────
    if detail_view == "Recent form":
        N = 8
        show_gws = list(range(max(1, gw - N + 1), gw + 1))

        def _cumulative(ids, histories, cap_idx):
            cum, running = [], 0
            for g in show_gws:
                gw_total = 0
                for i, mid in enumerate(ids):
                    pts = histories.get(mid, {}).get("gw_scores", {}).get(g, {}).get("points", 0) or 0
                    gw_total += pts * 2 if i == cap_idx else pts
                running += gw_total
                cum.append(running)
            return cum

        cum_a = _cumulative(res["team_a_ids"], histories_a, cap_a_idx)
        cum_b = _cumulative(res["team_b_ids"], histories_b, cap_b_idx)

        st.markdown(f"##### Cumulative points — last {len(show_gws)} GWs")
        st.dataframe(pd.DataFrame({
            "Gameweek": show_gws, league_name_a: cum_a, league_name_b: cum_b,
            "Lead": [a - b for a, b in zip(cum_a, cum_b)],
        }), hide_index=True, width="stretch")

        lead = (cum_a[-1] if cum_a else 0) - (cum_b[-1] if cum_b else 0)
        if lead > 0:
            st.caption(f"{league_name_a} lead by {lead} pts over this window.")
        elif lead < 0:
            st.caption(f"{league_name_b} lead by {abs(lead)} pts over this window.")
        else:
            st.caption("Dead level over this window.")

        if len(show_gws) < 2:
            st.caption(
                "Only one gameweek of history is available so far this season — "
                "more history becomes available as gameweeks are played."
            )

    # ── Chip tracker ─────────────────────────────────────────────────────────
    if not settings["no_chips"]:
        if detail_view == "Chips":
            st.markdown("##### Chip tracker  (H1 = GW1-19, H2 = GW20-38)")
            current_half = 1 if gw in engine.CHIP_H1_GWS else 2
            for label, team_ids, managers, picks_list, chips_hist, league_nm in (
                ("A", res["team_a_ids"], managers_a, picks_a, chips_hist_a, league_name_a),
                ("B", res["team_b_ids"], managers_b, picks_b, chips_hist_b, league_name_b),
            ):
                st.markdown(f"**{league_nm} (Team {label})**")
                cols = ["Manager"]
                for c in engine.ALL_CHIPS:
                    cols += [f"{engine.CHIP_DISPLAY[c]} H1", f"{engine.CHIP_DISPLAY[c]} H2"]
                data = []
                for i, (mid, pick) in enumerate(zip(team_ids, picks_list)):
                    history = chips_hist.get(mid, [])
                    active_chip = pick.get("active_chip")
                    mgr_label = engine._mgr_label(managers, i, mid)
                    row = [mgr_label]
                    for c in engine.ALL_CHIPS:
                        for half in (1, 2):
                            entry = next((h for h in history if h["name"] == c and h["half"] == half), None)
                            if active_chip == c and current_half == half:
                                row.append("active")
                            elif entry:
                                row.append(f"used GW{entry['event']}")
                            else:
                                row.append("available")
                    data.append(row)
                df = pd.DataFrame(data, columns=cols)

                def _chip_style(v):
                    if v == "active":
                        return CHIP_STYLE["active"]
                    if isinstance(v, str) and v.startswith("used"):
                        return CHIP_STYLE["used"]
                    if v == "available":
                        return CHIP_STYLE["available"]
                    return ""

                st.dataframe(
                    df.style.map(_chip_style, subset=cols[1:]),
                    hide_index=True, width="stretch",
                )
            st.caption("Every chip has two uses per season: once in GW1-19 (H1), once in GW20-38 (H2).")

    # ── Squad summaries ──────────────────────────────────────────────────────
    if not settings["no_summary"]:
        if detail_view == "Squads":
            for label, team_ids, picks_list, cap_idx, managers, league_nm in (
                ("A", res["team_a_ids"], picks_a, cap_a_idx, managers_a, league_name_a),
                ("B", res["team_b_ids"], picks_b, cap_b_idx, managers_b, league_name_b),
            ):
                st.markdown(f"#### {league_nm} (Team {label})")
                for i, (mid, mgr) in enumerate(zip(team_ids, picks_list)):
                    picks_data  = mgr["picks"]
                    active_chip = mgr["active_chip"]
                    eff_cap_id  = mgr.get("eff_cap_id")
                    cap_mul     = mgr.get("cap_mul", engine.fpl_cap_multiplier(active_chip))
                    mgr_label   = engine._mgr_label(managers, i, mid)
                    cap_flag    = "  🅷 H2H CAPTAIN" if i == cap_idx else ""
                    chip_flag   = f"  · {active_chip.upper()}" if active_chip else ""

                    raw, hit, final = engine.manager_final_score(mgr, live_scores)
                    with st.expander(f"Manager {i+1} — {mgr_label}{cap_flag}{chip_flag}  ·  {final} pts"):
                        starters = [p for p in picks_data if p["position"] <= 11]
                        bench    = [p for p in picks_data if p["position"] > 11]
                        ordered  = sorted(starters, key=lambda p: (
                            -(p["element"] == eff_cap_id), -p["is_vice_captain"]
                        ))

                        def _row(p, is_bench=False):
                            pl  = player_map.get(p["element"], {"name": f"#{p['element']}", "position": "?", "club": "?"})
                            pts = live_scores.get(p["element"], 0)
                            if is_bench:
                                note = "bboost" if active_chip == "bboost" else "bench"
                                mult = "x1"
                            else:
                                is_named_cap, is_eff_cap, is_vice = p["is_captain"], p["element"] == eff_cap_id, p["is_vice_captain"]
                                if is_named_cap and is_eff_cap:
                                    note = "(c) x3" if cap_mul == 3 else "(c)"
                                elif is_eff_cap and not is_named_cap:
                                    note = "(c*) auto-sub"
                                elif is_named_cap and not is_eff_cap:
                                    note = "(c) blank — 0 mins"
                                elif is_vice:
                                    note = "(v)"
                                else:
                                    note = ""
                                mult = f"x{p['count']}"
                            return {"Pos": pl.get("position", "?"), "Player": pl["name"],
                                    "Club": pl.get("club", "?"), "Pts": pts, "Mult": mult, "Note": note}

                        squad_recs = [_row(p) for p in ordered] + [_row(p, is_bench=True) for p in sorted(bench, key=lambda p: p["position"])]
                        st.dataframe(pd.DataFrame(squad_recs), hide_index=True, width="stretch")
                        hit_str = f"({hit})" if hit < 0 else "(0)"
                        st.caption(f"Score: {raw} {hit_str} = **{final}** pts")


# ── Per-fixture tab: captain pickers, Analyse button, then the dashboard ──────
def render_fixture(m: dict, gw: int, no_live: bool, no_summary: bool, no_chips: bool) -> None:
    team_a_name, team_b_name = m["team_a"], m["team_b"]
    key_base = f"{gw}_{team_a_name}_{team_b_name}".replace(" ", "_")

    status_label = {"finished": "✅ Full time", "live": "🔴 Live", "upcoming": "⏳ Not started"}[m["status"]]
    ko = m["kickoff"]
    ko_display = f"{ko[:10]} {ko[11:16]} UTC" if len(ko) >= 16 else "Kickoff TBC"
    st.caption(f"{ko_display} · {status_label}")

    col1, col2 = st.columns(2)
    cap_a = col1.selectbox(f"{team_a_name} captain", TEAMS[team_a_name]["managers"], key=f"capA_{key_base}")
    cap_b = col2.selectbox(f"{team_b_name} captain", TEAMS[team_b_name]["managers"], key=f"capB_{key_base}")

    analyse_clicked = st.button(
        f"Analyse {team_a_name} vs {team_b_name}", key=f"btn_{key_base}", type="primary",
    )

    result_key = f"result_{key_base}"
    input_signature = (cap_a, cap_b, no_live, no_summary, no_chips)
    if analyse_clicked:
        try:
            with st.spinner("Talking to the FPL API — leagues, picks, chip history..."):
                fetched = run_pipeline(
                    gw, TEAMS[team_a_name]["league_id"], TEAMS[team_b_name]["league_id"],
                    cap_a, cap_b, no_live,
                )
            st.session_state[result_key] = dict(
                res=fetched, settings=dict(no_summary=no_summary, no_chips=no_chips),
                inputs=input_signature, fetched_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            )
            st.toast(f"{team_a_name} vs {team_b_name} analysed!", icon="✅")
        except SystemExit as e:
            st.error(str(e).strip() or "Couldn't resolve one of the captains — check the roster and try again.")
        except Exception as e:
            st.error(f"Something went wrong fetching data: {e}")

    stored = st.session_state.get(result_key)
    if stored is None or stored.get("inputs") != input_signature:
        st.info("Pick both captains above, then click **Analyse** to load this fixture.")
        return

    st.divider()
    st.caption(f"Snapshot fetched {stored['fetched_at']}. Click Analyse again to refresh scores.")
    render_dashboard(stored["res"], stored["settings"], key_base)


# ── Weekly Matchups mode: one tab per this-gameweek fixture ──────────────────
def render_weekly_matchups(gw: int, no_live: bool, no_summary: bool, no_chips: bool) -> None:
    try:
        with st.spinner(f"Loading GW{gw} fixtures..."):
            matchups = gw_matchups(gw)
    except Exception as e:
        st.error(f"Couldn't load fixtures for this gameweek: {e}")
        return

    if not matchups:
        st.info(
            f"No fixtures found for GW{gw} yet — could be a blank gameweek, or fixtures "
            "haven't been released. Try a different gameweek in the sidebar."
        )
        return

    fixture_labels = [f'{m["team_a"]} vs {m["team_b"]}' for m in matchups]
    selected = st.selectbox("IML matchup", range(len(matchups)),
                            format_func=lambda i: fixture_labels[i], key="weekly_fixture")
    render_fixture(matchups[selected], gw, no_live, no_summary, no_chips)



# ── League-wide analytics engine ────────────────────────────────────────────────

@st.cache_data(ttl=900, show_spinner=False)
def cached_iml_registry():
    """Return the 80 IML members grouped by their 20 club-named H2H teams."""
    out = []
    rosters = _batch_load(tuple(TEAMS), lambda name: _league_info(TEAMS[name]["league_id"]))
    if len(rosters) != len(TEAMS):
        raise ValueError("IML registry is incomplete. Retry; partial league rosters are not cached.")
    for team_name, cfg in TEAMS.items():
        managers = rosters[team_name]["managers"]
        for m in managers:
            out.append({
                "team": team_name,
                "league_id": cfg["league_id"],
                "manager_id": str(m["id"]),
                "manager": m.get("manager") or m.get("name") or str(m["id"]),
            })
    # De-duplicate defensively in case a manager appears in two registered leagues.
    seen = set()
    deduped = []
    for row in out:
        if row["manager_id"] not in seen:
            seen.add(row["manager_id"])
            deduped.append(row)
    return tuple(deduped)


# Workers perform plain I/O only: no Streamlit calls or shared engine sessions.
# SQLite is a reusable endpoint snapshot, not durable league/captaincy storage.
@st.cache_resource(show_spinner=False)
def _telemetry():
    return threading.Lock(), {"network": 0, "disk_hits": 0, "failures": 0, "seconds": 0.0}

_DATA_LOCK, _DATA_STATS = _telemetry()


def _snapshot_path():
    return Path(os.environ.get("IML_CACHE_PATH", ".iml_cache/fpl.sqlite3"))


def _snapshot_read(url, ttl):
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=10) as db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (url TEXT PRIMARY KEY, saved REAL, payload TEXT)")
            row = db.execute("SELECT saved, payload FROM snapshots WHERE url=?", (url,)).fetchone()
        if row and time.time() - row[0] < ttl:
            with _DATA_LOCK:
                _DATA_STATS["disk_hits"] += 1
            return json.loads(row[1])
    except (OSError, sqlite3.Error, ValueError):
        pass  # Read-only deployments can still use in-memory Streamlit caching.
    return None


def _snapshot_write(url, payload):
    try:
        with sqlite3.connect(_snapshot_path(), timeout=10) as db:
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                       (url, time.time(), json.dumps(payload)))
    except (OSError, sqlite3.Error):
        pass


def _api_json(path, ttl=600):
    url = engine.BASE.rstrip("/") + "/" + path.lstrip("/")
    cached = _snapshot_read(url, ttl)
    if cached is not None:
        return cached
    started = time.perf_counter()
    try:
        with _DATA_LOCK:
            _DATA_STATS["network"] += 1
        request = urllib.request.Request(url, headers={"User-Agent": "IML-Scorecards/16"})
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.load(response)
        if not isinstance(payload, (dict, list)):
            raise ValueError("Unexpected FPL response")
        _snapshot_write(url, payload)
        return payload
    except Exception:
        with _DATA_LOCK:
            _DATA_STATS["failures"] += 1
        raise
    finally:
        with _DATA_LOCK:
            _DATA_STATS["seconds"] += time.perf_counter() - started


def _batch_load(keys, loader):
    keys = tuple(dict.fromkeys(keys))
    def load_one(key):
        try:
            return key, loader(key), None
        except Exception as exc:
            return key, None, type(exc).__name__
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(load_one, keys))
    failures = [str(key) for key, value, error in results if error]
    if failures:
        st.warning(f"{len(failures)} records could not load. Retry this page; partial data is labelled below. IDs: "
                   + ", ".join(failures[:8]))
    return {key: value for key, value, error in results if error is None}


@st.cache_data(ttl=180, show_spinner=False, max_entries=4000)
def cached_manager_gw_picks(manager_id: str, gw: int):
    # Failed requests raise, so an empty failure is never saved as a valid squad.
    return _api_json(f"entry/{manager_id}/event/{gw}/picks/", ttl=180)


def cached_all_current_picks(gw: int, manager_ids: tuple):
    return _batch_load(manager_ids, lambda mid: _api_json(f"entry/{mid}/event/{gw}/picks/", ttl=180))


@st.cache_data(ttl=21600, show_spinner=False, max_entries=1000)
def cached_player_summary(pid):
    return _api_json(f"element-summary/{int(pid)}/", ttl=21600)


def cached_player_summaries(player_ids: tuple):
    return _batch_load(tuple(map(int, player_ids)), lambda pid: _api_json(f"element-summary/{pid}/", ttl=21600))


@st.cache_data(ttl=60, show_spinner=False)
def cached_fixtures_all():
    return _api_json("fixtures/", ttl=60)


def _league_info(league_id):
    managers, page, name = [], 1, f"League {league_id}"
    while True:
        data = _api_json(f"leagues-classic/{league_id}/standings/?page_standings={page}", ttl=900)
        name = data.get("league", {}).get("name", name)
        standings = data.get("standings", {})
        for r in standings.get("results", []):
            managers.append({"id": str(r["entry"]), "name": r.get("entry_name", ""),
                             "manager": r.get("player_name", "")})
        if not standings.get("has_next"):
            break
        page += 1
        if page > 100:
            raise ValueError("Unexpected roster pagination")
    return {"name": name, "managers": managers}


@st.cache_data(ttl=900, show_spinner=False)
def cached_league_info(league_id):
    return _league_info(league_id)


def _manager_history(mid):
    data = _api_json(f"entry/{mid}/history/", ttl=900)
    chips = [{"name": c["name"].lower(), "event": c.get("event", 0),
              "half": 1 if c.get("event", 0) in engine.CHIP_H1_GWS else 2}
             for c in data.get("chips", [])]
    scores = {int(r["event"]): {"points": r.get("points", 0),
                "total_points": r.get("total_points", 0), "rank": r.get("rank")}
              for r in data.get("current", []) if r.get("event")}
    return {"chips": chips, "gw_scores": scores}


def cached_all_manager_histories(manager_ids: tuple):
    return _batch_load(tuple(map(str, manager_ids)), _manager_history)


@st.cache_data(ttl=900, show_spinner=False)
def cached_manager_transfers(manager_id: str):
    return _api_json(f"entry/{manager_id}/transfers/", ttl=900)


def cached_historical_picks(manager_id, gw):
    return _batch_load(tuple(range(1, gw + 1)),
                       lambda g: _api_json(f"entry/{manager_id}/event/{g}/picks/", ttl=180))


def parse_projection_csv(contents, valid_ids):
    """Canonical long format; IDs must be official FPL element IDs, GW totals include DGWs."""
    df = pd.read_csv(io.BytesIO(contents))
    df.columns = [str(c).strip().lower() for c in df.columns]
    if df.columns.duplicated().any():
        raise ValueError("Duplicate CSV columns")
    required = {"player_id", "gw", "xpts"}
    if not required.issubset(df.columns):
        raise ValueError("Required columns: player_id, gw, xpts; optional: xmins")
    for col in required | ({"xmins"} if "xmins" in df else set()):
        df[col] = pd.to_numeric(df[col], errors="raise")
        if not np.isfinite(df[col]).all():
            raise ValueError(f"{col} contains missing or infinite values")
    for col in ("player_id", "gw"):
        if (df[col] != df[col].astype(int)).any():
            raise ValueError(f"{col} must contain whole numbers")
        df[col] = df[col].astype(int)
    if df.empty or not df["gw"].between(1, 38).all():
        raise ValueError("CSV must have rows and gameweeks between 1 and 38")
    if df.duplicated(["player_id", "gw"]).any():
        raise ValueError("Duplicate player_id / gw rows; combine double fixtures into one GW total")
    unknown = set(df.player_id) - set(valid_ids)
    if unknown:
        raise ValueError(f"Unknown FPL player IDs: {sorted(unknown)[:8]}")
    if not df.xpts.between(-20, 100).all():
        raise ValueError("xpts values must be between -20 and 100")
    if "xmins" in df and not df.xmins.between(0, 270).all():
        raise ValueError("xmins must be between 0 and 270 (GW total)")
    return {(int(r.player_id), int(r.gw)): float(r.xpts) for r in df.itertuples()}


def apply_external_projections(rows, projections, gw, horizon):
    for row in rows:
        values = dict(row["projections_by_gw"])
        replaced = 0
        for event in range(gw, min(39, gw + horizon)):
            if (row["id"], event) in projections:
                values[event] = projections[(row["id"], event)]
                replaced += 1
                event_fixtures = [f for f in row["fixtures_next"] if f["event"] == event]
                for fixture in event_fixtures:
                    fixture["projection"] = values[event] / len(event_fixtures)
        row["projections_by_gw"] = values
        row["projected_next"] = sum(values.values())
        row["projected_ppg"] = row["projected_next"] / max(1, min(horizon, 39 - gw))
        row["projection_source"] = "CSV + fallback" if replaced else "Heuristic fallback"
    return rows


def current_or_next_gw(bootstrap: dict) -> int:
    for ev in bootstrap.get("events", []):
        if ev.get("is_current"):
            return int(ev["id"])
    for ev in bootstrap.get("events", []):
        if ev.get("is_next"):
            return int(ev["id"])
    for ev in bootstrap.get("events", []):
        if not ev.get("finished"):
            return int(ev["id"])
    return 1


def _safe_float(v, default=0.0):
    try:
        value = float(v)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _pct_rank(values):
    """Simple percentile rank, robust to a short league at season start."""
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)
    return {
        v: (sum(x <= v for x in ordered) - 1) / max(1, n - 1)
        for v in set(values)
    }


def _normalize01(v, lo, hi):
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _difficulty_factor(fdr):
    # FPL FDR is 1 (best) → 5 (worst). 3 is the neutral baseline.
    return {1: 1.16, 2: 1.08, 3: 1.00, 4: 0.92, 5: 0.84}.get(int(fdr or 3), 1.0)


def _defcon_threshold(position):
    return {"DEF": 10, "MID": 12, "FWD": 12}.get(position, 999)


def _player_position_map(bootstrap):
    return {int(e["id"]): engine.POSITIONS.get(e.get("element_type"), "?")
            for e in bootstrap.get("elements", [])}


def _team_name_map(bootstrap):
    return {int(t["id"]): t.get("name") or str(t["id"]) for t in bootstrap.get("teams", [])}


def _fixture_index(fixtures):
    idx = defaultdict(list)
    for f in fixtures:
        event = f.get("event")
        if not event:
            continue
        h = int(f.get("team_h") or 0)
        a = int(f.get("team_a") or 0)
        if h:
            idx[h].append({
                "event": int(event), "opponent_id": a, "home": True,
                "kickoff": f.get("kickoff_time") or "",
                "fdr": int(f.get("team_h_difficulty") or 3),
                "fixture_id": f.get("id"),
                "minutes": _safe_float(f.get("minutes")),
                "started": bool(f.get("started")),
                "finished": bool(f.get("finished") or f.get("finished_provisional")),
            })
        if a:
            idx[a].append({
                "event": int(event), "opponent_id": h, "home": False,
                "kickoff": f.get("kickoff_time") or "",
                "fdr": int(f.get("team_a_difficulty") or 3),
                "fixture_id": f.get("id"),
                "minutes": _safe_float(f.get("minutes")),
                "started": bool(f.get("started")),
                "finished": bool(f.get("finished") or f.get("finished_provisional")),
            })
    for k in idx:
        idx[k].sort(key=lambda x: (x["event"], x["kickoff"]))
    return idx


def build_player_analytics(bootstrap, fixtures, player_summaries=None, horizon=5, start_gw=None):
    """
    Transparent projection model:
      1) Minutes security: start rate + average minutes.
      2) Underlying: xG90 + xA90 + DC90.
      3) Recent form: recent FPL points adjusted by fixture difficulty.
      4) Next-fixture multiplier from official FPL FDR.
      5) Projected points = blended season/recent baseline × minutes security × FDR.
    """
    player_summaries = player_summaries or {}
    teams = _team_name_map(bootstrap)
    fx_idx = _fixture_index(fixtures)
    current_gw = int(start_gw or current_or_next_gw(bootstrap))
    rows = []

    for p in bootstrap.get("elements", []):
        pid = int(p["id"])
        pos = engine.POSITIONS.get(p.get("element_type"), "?")
        hist = (player_summaries.get(pid) or {}).get("history", [])

        # Use recent player history when available; bootstrap remains the fallback.
        recent = sorted(hist, key=lambda h: int(h.get("round") or 0))[-5:]
        played = [h for h in recent if _safe_float(h.get("minutes")) > 0]
        minutes_total = sum(_safe_float(h.get("minutes")) for h in hist)
        starts_total = sum(_safe_float(h.get("starts")) for h in hist)
        appearances = len([h for h in hist if _safe_float(h.get("minutes")) > 0])

        completed = max(1, sum(bool(e.get("finished")) for e in bootstrap.get("events", [])))
        avg_minutes = (minutes_total / appearances) if appearances else _safe_float(p.get("minutes"), 0) / completed
        start_rate = (starts_total / max(1, appearances)) if hist else 0.75 if _safe_float(p.get("minutes")) >= 60 else 0.4
        minutes_security = max(0.0, min(1.0, 0.58 * min(avg_minutes / 90.0, 1.0) + 0.42 * min(start_rate, 1.0)))

        xg = sum(_safe_float(h.get("expected_goals")) for h in hist) if hist else _safe_float(p.get("expected_goals"))
        xa = sum(_safe_float(h.get("expected_assists")) for h in hist) if hist else _safe_float(p.get("expected_assists"))
        mins = max(1.0, minutes_total if hist else _safe_float(p.get("minutes")))
        xg90 = 90.0 * xg / mins
        xa90 = 90.0 * xa / mins
        xgi90 = xg90 + xa90

        dc_total = sum(_safe_float(h.get("defensive_contribution")) for h in hist)
        dc90 = 90.0 * dc_total / mins if dc_total else 0.0
        threshold = _defcon_threshold(pos)
        defcon_hits = [h for h in played if _safe_float(h.get("defensive_contribution")) >= threshold]
        defcon_reliability = len(defcon_hits) / len(played) if played else 0.0

        # Bootstrap fallback for early season / players without detailed history.
        season_ppg = _safe_float(p.get("points_per_game"), 0.0)
        season_form = _safe_float(p.get("form"), season_ppg)
        if played:
            recent_ppg = sum(_safe_float(h.get("total_points")) for h in played) / len(played)
        else:
            recent_ppg = season_form

        # Fixture-adjusted recent form: punish hard recent schedules, reward easy ones.
        recent_adj = []
        for h in played:
            fdr = 3
            for f in fx_idx.get(int(p.get("team") or 0), []):
                if int(f["event"]) == int(h.get("round") or -1):
                    fdr = f["fdr"]
                    break
            recent_adj.append(_safe_float(h.get("total_points")) / _difficulty_factor(fdr))
        fixture_adjusted_form = sum(recent_adj) / len(recent_adj) if recent_adj else season_form

        # Forward fixture ticker.
        fixtures_next = [
            f for f in fx_idx.get(int(p.get("team") or 0), [])
            if current_gw <= f["event"] < current_gw + horizon
        ]

        proj = []
        per_gw = {event: 0.0 for event in range(current_gw, min(39, current_gw + horizon))}
        chance = p.get("chance_of_playing_next_round")
        availability = _safe_float(chance, 100.0) / 100.0 if chance is not None else 1.0
        if p.get("status") in ("u", "i", "s") and chance is None:
            availability = 0.0
        for f in fixtures_next:
            fixture_baseline = (
                0.55 * season_ppg +
                0.25 * recent_ppg +
                0.20 * fixture_adjusted_form
            )
            underlying_signal = 1.55 * xgi90 + (0.10 * dc90)
            minutes_factor = 0.62 + 0.38 * minutes_security
            match_projection = (fixture_baseline + underlying_signal) * minutes_factor * _difficulty_factor(f["fdr"])
            # Small role correction for goalkeepers/defenders through clean sheets/defcon.
            if pos in ("GKP", "DEF"):
                match_projection += 0.18 * defcon_reliability
            match_projection *= max(0.0, min(1.0, availability))
            f["projection"] = max(0.0, match_projection)
            proj.append(f["projection"])
            per_gw[f["event"]] += f["projection"]

        projected_next = sum(proj)
        next_ppg = projected_next / max(1, min(horizon, 39 - current_gw))

        # Simple reliability blend, constrained to an intuitive 0-100 score.
        ppg_stability = 1.0
        if len(played) >= 2:
            vals = [_safe_float(h.get("total_points")) for h in played]
            mean = sum(vals) / len(vals)
            stdev = (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5
            ppg_stability = 1.0 / (1.0 + (stdev / max(1.0, mean)))
        reliability = 100.0 * (0.58 * minutes_security + 0.24 * defcon_reliability + 0.18 * ppg_stability)

        rows.append({
            "id": pid,
            "name": p.get("web_name") or f"#{pid}",
            "full_name": f'{p.get("first_name","")} {p.get("second_name","")}'.strip(),
            "position": pos,
            "club": teams.get(int(p.get("team") or 0), "?"),
            "club_id": int(p.get("team") or 0),
            "price": _safe_float(p.get("now_cost")) / 10.0,
            "ownership": _safe_float(p.get("selected_by_percent")),
            "season_points": _safe_float(p.get("total_points")),
            "season_ppg": season_ppg,
            "form": season_form,
            "minutes": _safe_float(p.get("minutes")),
            "minutes_security": minutes_security * 100.0,
            "xg90": xg90,
            "xa90": xa90,
            "xgi90": xgi90,
            "dc90": dc90,
            "defcon_reliability": defcon_reliability * 100.0,
            "fixture_adjusted_form": fixture_adjusted_form,
            "projected_next": projected_next,
            "projected_ppg": next_ppg,
            "reliability": reliability,
            "fixtures_next": fixtures_next,
            "projections_by_gw": per_gw,
            "projection_source": "Heuristic fallback",
        })
    return rows


def _squad_player_ids(all_picks):
    ids = set()
    for data in all_picks.values():
        for p in data.get("picks", []) if data else []:
            try:
                ids.add(int(p["element"]))
            except Exception:
                pass
    return tuple(sorted(ids))


def league_ownership_table(registry, all_picks, analytics_rows):
    analytics_map = {r["id"]: r for r in analytics_rows}
    owners = defaultdict(set)
    captainters = defaultdict(float)
    team_counts = defaultdict(lambda: defaultdict(int))

    for member in registry:
        mid = member["manager_id"]
        data = all_picks.get(mid) or {}
        for p in data.get("picks", []):
            pid = int(p["element"])
            owners[pid].add(mid)
            if p.get("position", 0) <= 11:
                team_counts[member["team"]][pid] += 1
                if p.get("is_captain"):
                    # Captain ownership counts as one extra unit; TC gets two extra.
                    mult = 2 if data.get("active_chip") == "3xc" else 1
                    captainters[pid] += mult

    n = max(1, sum(bool((all_picks.get(m["manager_id"]) or {}).get("picks")) for m in registry))
    rows = []
    for pid, owner_set in owners.items():
        r = analytics_map.get(pid)
        if not r:
            continue
        ownership_pct = 100.0 * len(owner_set) / n
        eo_pct = 100.0 * (len(owner_set) + captainters.get(pid, 0)) / n
        team_vals = [team_counts[t].get(pid, 0) for t in TEAMS.keys()]
        max_diff = max(team_vals) - min(team_vals) if team_vals else 0
        rows.append({
            **r,
            "iml_owned": len(owner_set),
            "ownership_pct": ownership_pct,
            "captain_pct": 100.0 * captainters.get(pid, 0) / n,
            "effective_ownership_pct": eo_pct,
            "max_team_ownership_diff": max_diff,
            "swing_potential": max_diff * r["projected_ppg"],
        })
    rows.sort(key=lambda x: (-x["ownership_pct"], -x["projected_next"]))
    return rows, team_counts


def build_iml11(ownership_rows, ownership_threshold=20.0):
    eligible = [r for r in ownership_rows if r["ownership_pct"] >= ownership_threshold]
    if not eligible:
        return []

    # IML Template XI score: ownership is the floor (protect rank),
    # projection is the upside, and security/reliability stop fragile picks winning.
    eligible = [dict(r) for r in eligible]
    for r in eligible:
        r["iml11_score"] = (
            0.30 * min(100.0, r["ownership_pct"]) +
            0.28 * min(100.0, 20.0 * r["projected_ppg"]) +
            0.15 * r["minutes_security"] +
            0.12 * min(100.0, r["fixture_adjusted_form"] * 10.0) +
            0.10 * r["defcon_reliability"] +
            0.05 * r["reliability"]
        )

    # Greedy construction with FPL formation + max-three-per-club constraints.
    selected = []
    counts = defaultdict(int)

    def pick_best(position=None, minimum=False):
        cand = [x for x in eligible if (position is None or x["position"] == position)
                and x["id"] not in {s["id"] for s in selected}
                and counts[x["club_id"]] < 3]
        cand.sort(key=lambda x: x["iml11_score"], reverse=True)
        if not cand:
            return None
        x = cand[0]
        selected.append(x)
        counts[x["club_id"]] += 1
        return x

    pick_best("GKP")
    for _ in range(3):
        pick_best("DEF")
    for _ in range(2):
        pick_best("MID")
    pick_best("FWD")

    while len(selected) < 11:
        # Respect 5 defenders max, 5 mids max, 3 forwards max.
        pos_counts = {p: sum(1 for s in selected if s["position"] == p) for p in ("DEF", "MID", "FWD", "GKP")}
        cand = [x for x in eligible
                if x["id"] not in {s["id"] for s in selected}
                and counts[x["club_id"]] < 3
                and (
                    x["position"] == "DEF" and pos_counts["DEF"] < 5 or
                    x["position"] == "MID" and pos_counts["MID"] < 5 or
                    x["position"] == "FWD" and pos_counts["FWD"] < 3
                )]
        if not cand:
            break
        cand.sort(key=lambda x: x["iml11_score"], reverse=True)
        x = cand[0]
        selected.append(x)
        counts[x["club_id"]] += 1

    return selected[:11]


def h2h_live_win_probability(picks_a, picks_b, player_map, live_scores, analytics_map,
                             manager_histories=None, simulations=10000, gw=None, draw_margin=5):
    """Vectorised heuristic remainder; shared player outcomes stay correlated.

    This is a provisional exposure model: live cards/CS loss, future autosubs and
    bonus revisions are not modelled. It must not be described as calibrated.
    """
    def exposures(managers):
        counts = defaultdict(float)
        hits = 0.0
        for mgr in managers:
            hits += mgr.get("transfer_hit", 0) * mgr.get("team_multiplier", 1)
            for pick in mgr.get("picks", []):
                if mgr.get("active_chip") == "bboost" or 1 <= pick.get("position", 0) <= 11:
                    counts[int(pick["element"])] += pick.get("count", pick.get("multiplier", 1))
        return counts, hits
    ca, hit_a = exposures(picks_a)
    cb, hit_b = exposures(picks_b)
    ids = sorted(set(ca) | set(cb))
    fixed_margin = hit_a - hit_b + sum((ca[pid] - cb[pid]) * live_scores.get(pid, 0) for pid in ids)
    future = []
    for pid in ids:
        row = analytics_map.get(pid, {})
        if not row or ca[pid] == cb[pid]:
            continue
        fixtures = [f for f in row.get("fixtures_next", []) if gw is None or f["event"] == gw]
        if gw is None and fixtures:
            event = min(f["event"] for f in fixtures)
            fixtures = [f for f in fixtures if f["event"] == event]
        remaining = 0.0
        for fixture in fixtures:
            if fixture.get("finished"):
                continue
            fraction = max(0.0, 1.0 - fixture.get("minutes", 0.0) / 90.0) if fixture.get("started") else 1.0
            remaining += fixture.get("projection", 0.0) * fraction
        # CSV is a GW total. Allocate over fixtures for a rough remaining-time estimate.
        if gw is not None and row.get("projection_source") == "CSV + fallback" and fixtures:
            fractions = [0.0 if f.get("finished") else max(0.0, 1 - f.get("minutes", 0) / 90) if f.get("started") else 1.0 for f in fixtures]
            remaining = max(0.0, row.get("projections_by_gw", {}).get(gw, 0.0)) * sum(fractions) / len(fixtures)
        if remaining > 0:
            reliability = max(0.15, min(0.92, row.get("reliability", 50.0) / 100))
            future.append((pid, ca[pid], cb[pid], remaining, reliability))
    seed = int(sum((pid + 1) * (live_scores.get(pid, 0) + 7) for pid in ids)) % (2**32 - 1)
    rng = np.random.default_rng(seed)
    margins = np.full(max(1, int(simulations)), fixed_margin, dtype=float)
    if future:
        means = np.array([f[3] for f in future])
        shapes = np.array([max(1.2, 1.5 + 3.0 * f[4]) for f in future])
        samples = rng.gamma(shapes, means / shapes, size=(len(margins), len(future)))
        margins += samples @ np.array([f[1] - f[2] for f in future])
    return (100.0 * np.mean(margins > draw_margin),
            100.0 * np.mean(np.abs(margins) <= draw_margin),
            100.0 * np.mean(margins < -draw_margin), future)


def simulate_manager_score(mgr_picks, gw, player_histories, player_map):
    """Aggregate DGW histories and use FPL's effective scoring multipliers."""
    picks = mgr_picks.get("picks", [])
    def points(pid):
        return sum(_safe_float(h.get("total_points")) for h in
                   (player_histories.get(int(pid)) or {}).get("history", [])
                   if int(h.get("round") or -1) == int(gw))
    raw = sum(points(p["element"]) * p.get("multiplier", 0) for p in picks)
    bench_waste = sum(points(p["element"]) for p in picks
                      if p.get("position", 0) > 11 and p.get("multiplier", 0) == 0)
    cap = next((p for p in picks if p.get("multiplier", 0) > 1), None)
    eligible = [points(p["element"]) for p in picks if p.get("multiplier", 0) > 0]
    cap_extra = 2 if mgr_picks.get("active_chip") == "3xc" else 1
    regret = max(0.0, (max(eligible, default=0) - (points(cap["element"]) if cap else 0)) * cap_extra)
    return raw, max(0.0, bench_waste), regret


def manager_transfer_roi(manager_id, transfers, player_histories, window=4):
    """4-GW transfer ROI: incoming points minus outgoing points over next N GWs, less hit."""
    rows = []
    for t in transfers or []:
        try:
            gw = int(t.get("event"))
            inn = int(t.get("element_in"))
            out = int(t.get("element_out"))
        except Exception:
            continue
        h_in = (player_histories.get(inn) or {}).get("history", [])
        h_out = (player_histories.get(out) or {}).get("history", [])
        end = gw + window - 1
        in_pts = sum(_safe_float(h.get("total_points")) for h in h_in if gw <= int(h.get("round") or -1) <= end)
        out_pts = sum(_safe_float(h.get("total_points")) for h in h_out if gw <= int(h.get("round") or -1) <= end)
        cost = abs(_safe_float(t.get("cost"), 0.0))
        rows.append({
            "gw": gw, "incoming": inn, "outgoing": out,
            "incoming_pts": in_pts, "outgoing_pts": out_pts,
            "hit": cost, "roi": in_pts - out_pts - cost,
        })
    return rows


def manager_power_rankings(registry, histories, chip_histories):
    records = []
    for member in registry:
        mid = member["manager_id"]
        h = histories.get(mid, {})
        gws = [v for g, v in sorted(h.get("gw_scores", {}).items()) if v.get("points") is not None]
        pts = [float(v.get("points") or 0) for v in gws]
        total = float(gws[-1].get("total_points") or 0) if gws else 0.0
        recent = pts[-5:] if pts else []
        mean = sum(pts) / len(pts) if pts else 0.0
        stdev = (sum((x - mean) ** 2 for x in pts) / len(pts)) ** 0.5 if pts else 0.0
        consistency = 100.0 / (1.0 + stdev / max(1.0, mean))
        recent_mean = sum(recent) / len(recent) if recent else mean

        chips = chip_histories.get(mid, [])
        chip_events = [int(c["event"]) for c in chips]
        chip_roi = 0.0
        if chips and pts:
            all_gw_nums = sorted(h.get("gw_scores", {}).keys())
            nonchip = [
                float(h.get("gw_scores", {}).get(g, {}).get("points", 0) or 0)
                for g in all_gw_nums
                if int(g) not in chip_events
            ]
            baseline = sum(nonchip) / len(nonchip) if nonchip else mean
            chip_pts = sum(
                h.get("gw_scores", {}).get(g, {}).get("points", 0) or 0
                for g in chip_events
            )
            chip_roi = chip_pts / len(chips) - baseline

        records.append({
            **member,
            "total_points": total,
            "recent_mean": recent_mean,
            "consistency": consistency,
            "chip_roi": chip_roi,
            "weeks": len(pts),
        })

    if not records:
        return []

    p_total = _pct_rank([r["total_points"] for r in records])
    p_recent = _pct_rank([r["recent_mean"] for r in records])
    p_cons = _pct_rank([r["consistency"] for r in records])
    p_chip = _pct_rank([r["chip_roi"] for r in records])

    for r in records:
        r["power_score"] = 100.0 * (
            0.50 * p_total[r["total_points"]] +
            0.25 * p_recent[r["recent_mean"]] +
            0.20 * p_cons[r["consistency"]] +
            0.05 * p_chip[r["chip_roi"]]
        )
    records.sort(key=lambda x: (-x["power_score"], -x["total_points"]))
    for i, r in enumerate(records, 1):
        r["rank"] = i
    return records


def differential_finder(ownership_rows, min_ownership=2.5):
    """Low-owned player shortlist using projection, reliability, fixture and crowd leverage."""
    rows = []
    for r in ownership_rows:
        own = r["ownership_pct"]
        if own > min_ownership + 7.5:
            continue
        upside = 0.55 * r["projected_ppg"] + 0.20 * r["fixture_adjusted_form"] + 0.15 * r["reliability"] / 20.0 + 0.10 * r["minutes_security"] / 20.0
        leverage = max(0.0, 100.0 - own) / 100.0
        score = upside * (0.65 + 0.35 * leverage)
        rows.append({**r, "differential_score": score})
    rows.sort(key=lambda x: (-x["differential_score"], -x["projected_ppg"]))
    return rows[:30]


def member_snapshot(manager_id, gw, player_histories, player_map):
    """Historical manager lab: captain regret, bench waste, consistency and season totals."""
    weekly = []
    for g, picks in sorted(cached_historical_picks(manager_id, gw).items()):
        if not picks or not picks.get("picks"):
            continue
        raw, bench_waste, cap_regret = simulate_manager_score(
            picks, g, player_histories, player_map
        )
        weekly.append({"GW": g, "Points": raw, "Bench Wasted": bench_waste, "Captain Regret": cap_regret})

    pts = [x["Points"] for x in weekly]
    mean = sum(pts) / len(pts) if pts else 0.0
    sd = (sum((x - mean) ** 2 for x in pts) / len(pts)) ** 0.5 if pts else 0.0
    consistency = 100.0 / (1.0 + sd / max(1.0, mean))
    return {
        "weekly": weekly,
        "avg_gw": mean,
        "consistency": consistency,
        "bench_wasted": sum(x["Bench Wasted"] for x in weekly),
        "captain_regret": sum(x["Captain Regret"] for x in weekly),
    }


def format_fixture_ticker(row, team_map):
    ticker = []
    for f in row.get("fixtures_next", []):
        opp = team_map.get(f["opponent_id"], str(f["opponent_id"]))
        ticker.append({
            "GW": f["event"],
            "Fixture": f'{"H" if f["home"] else "A"} · {opp}',
            "FDR": f["fdr"],
            "Proj": round(f.get("projection", 0.0), 2),
        })
    return ticker


# ── Sidebar: global settings ────────────────────────────────────────────────────
for _k, _default in (("gw_value", None), ("skip_live_value", False),
                     ("skip_summary_value", False), ("skip_chips_value", False)):
    if _k not in st.session_state:
        st.session_state[_k] = _default

with st.sidebar:
    st.header("IML")
    st.caption("Scores · squads · decisions")
    mode = st.radio("Workspace", ["Matchups", "Players", "Managers"], key="workspace")

    try:
        bootstrap_sidebar = cached_bootstrap()
    except Exception as exc:
        st.error(f"FPL data is unavailable ({type(exc).__name__}). Retry shortly.")
        st.stop()
    default_gw = current_or_next_gw(bootstrap_sidebar)

    with st.expander("External projections (CSV)"):
        st.caption("Upload player_id,gw,xpts (optional xmins). IDs must match this FPL season. Each row is a complete GW total, including double fixtures. Missing rows use the fallback model. xmins is validated but not used by the current model.")
        projection_file = st.file_uploader("Projection CSV", type=["csv"], key="projection_upload")
        if projection_file is None:
            st.session_state.external_projections = {}
        else:
            try:
                st.session_state.external_projections = parse_projection_csv(
                    projection_file.getvalue(), [p["id"] for p in bootstrap_sidebar.get("elements", [])])
                st.success(f"Loaded {len(st.session_state.external_projections)} player/GW projections")
            except (ValueError, TypeError, pd.errors.ParserError) as exc:
                st.session_state.external_projections = {}
                st.error(f"CSV rejected: {exc}")
    st.divider()
    if mode == "Matchups":
        st.header("Gameweek")
        gw = st.number_input("Gameweek", min_value=1, max_value=38,
                             value=int(st.session_state.gw_value or default_gw),
                             step=1, key="gw_input")
        st.session_state.gw_value = gw

        with st.expander("Matchup settings"):
            no_live = st.checkbox(
                "Skip live scores",
                value=st.session_state.skip_live_value,
                key="skip_live",
                help="Use for a finished season or faster testing.",
            )
            st.session_state.skip_live_value = no_live
            no_summary = st.checkbox(
                "Skip squad summaries",
                value=st.session_state.skip_summary_value,
                key="skip_summary",
            )
            st.session_state.skip_summary_value = no_summary
            no_chips = st.checkbox(
                "Skip chip tracker",
                value=st.session_state.skip_chips_value,
                key="skip_chips",
            )
            st.session_state.skip_chips_value = no_chips
    else:
        st.header("Analytics")
        gw = st.number_input(
            "Analysis gameweek",
            min_value=1, max_value=38,
            value=int(st.session_state.gw_value or default_gw),
            step=1, key="analytics_gw",
        )
        st.session_state.gw_value = gw
        horizon = 5
        ownership_threshold = 20.0
        if mode == "Players":
            with st.expander("Projection settings"):
                horizon = st.slider("Look ahead (gameweeks)", 3, 8, 5, key="analytics_horizon")
                ownership_threshold = st.slider("Template minimum ownership (%)", 5.0, 40.0, 20.0, 2.5, key="ownership_threshold")


# ── League analytics UI ─────────────────────────────────────────────────────────
def render_league_analytics(gw: int, horizon: int, ownership_threshold: float):
    panel_names = ["Player explorer", "Matchup outlook", "Manager review", "Template XI", "Rankings"]
    available_panels = [panel_names[0], panel_names[1], panel_names[3]] if mode == "Players" else [panel_names[4], panel_names[2]]
    active_panel = st.radio("View", available_panels, horizontal=True, key=f"{mode}_panel")

    bootstrap = cached_bootstrap()
    player_map = cached_player_map(bootstrap)
    registry = list(cached_iml_registry())
    if not registry:
        st.warning("No IML managers could be loaded.")
        return
    manager_ids = tuple(sorted({m["manager_id"] for m in registry}))

    requested_ids = manager_ids
    if active_panel == panel_names[2]:
        manager_options = [f'{m["manager"]} · {m["team"]}' for m in registry]
        chosen_manager = st.selectbox("IML member", manager_options, key="manager_lab_member")
        member = registry[manager_options.index(chosen_manager)]
        mid = member["manager_id"]
        requested_ids = (mid,)
    with st.spinner("Loading IML league data and building the analytics model…"):
        all_picks = cached_all_current_picks(gw, requested_ids)


        owned_ids = _squad_player_ids(all_picks)
        # Detailed player histories are fetched for the league's currently owned pool.
        player_summaries = cached_player_summaries(owned_ids)
        fixtures = cached_fixtures_all()
        analytics_rows = build_player_analytics(
            bootstrap, fixtures, player_summaries, horizon=horizon, start_gw=gw
        )
        analytics_rows = apply_external_projections(analytics_rows,
            st.session_state.get("external_projections", {}), gw, horizon)
        loaded = sum(bool(v.get("picks")) for v in all_picks.values())
        st.caption(f"Squad coverage: {loaded}/{len(requested_ids)} requested managers. Missing squads are excluded from ownership percentages.")
        if loaded == 0:
            st.info("No squads available for this gameweek. Picks may not be published yet.")
            return
        st.caption("Forecasts are heuristic estimates unless labelled CSV; they are not calibrated probabilities. CSV source and calibration remain the uploader’s responsibility.")
        ownership_rows, team_counts = league_ownership_table(registry, all_picks, analytics_rows)
        analytics_map = {r["id"]: r for r in analytics_rows}

    # ── Player metrics ──────────────────────────────────────────────────────────
    if active_panel == panel_names[0]:
        st.subheader("Player explorer")
        st.caption(
            "Search your league's players, compare expected points, and check upcoming fixtures."
        )

        search = st.text_input("Find a player", placeholder="Search by name…", key="player_search")
        pool = ownership_rows if ownership_rows else [{**r, "ownership_pct": 0.0} for r in analytics_rows]
        if search.strip():
            q = search.strip().lower()
            filtered = [r for r in pool if q in r["name"].lower() or q in r["full_name"].lower()]
        else:
            filtered = pool

        f1, f2 = st.columns(2)
        position = f1.selectbox("Position", ["All positions"] + sorted({r["position"] for r in pool}), key="player_position")
        club = f2.selectbox("Club", ["All clubs"] + sorted({r["club"] for r in pool}), key="player_club")
        filtered = [r for r in filtered if (position == "All positions" or r["position"] == position)
                    and (club == "All clubs" or r["club"] == club)]
        filtered = sorted(filtered, key=lambda r: -r["projected_next"])

        show_model = st.checkbox("Show detailed model metrics", key="player_model_details")
        fields = {
            "name": "Player", "position": "Pos", "club": "Club", "price": "Price",
            "ownership_pct": "IML own %", "projected_ppg": "Expected / GW",
            "projected_next": f"Expected next {horizon} GWs", "projection_source": "Source",
        }
        if show_model:
            fields.update({"minutes_security": "Minutes security %", "xg90": "xG / 90",
                "xa90": "xA / 90", "xgi90": "xGI / 90",
                "defcon_reliability": "DEFCON reliability %",
                "fixture_adjusted_form": "Fixture-adjusted form"})
        if filtered:
            df = pd.DataFrame(filtered)[list(fields)].rename(columns=fields)
            st.dataframe(df.round(2), hide_index=True, width="stretch", height=420)
            st.caption(f"{len(filtered)} players · Expected points are estimates over the selected horizon.")
        else:
            st.info("No players match your search.")

        st.divider()
        selected_id = st.selectbox(
            "Player details", [r["id"] for r in filtered],
            format_func=lambda pid: f'{analytics_map[pid]["name"]} · {analytics_map[pid]["club"]}',
            key="player_deep_dive_id",
        ) if filtered else None
        selected = next((r for r in filtered if r["id"] == selected_id), None)
        if selected:
            a, b, c = st.columns(3)
            a.metric("Expected / GW", f'{selected["projected_ppg"]:.1f}')
            b.metric("IML ownership", f'{selected["ownership_pct"]:.1f}%')
            c.metric("Minutes security", f'{selected["minutes_security"]:.0f}%')

            ticker = format_fixture_ticker(selected, _team_name_map(bootstrap))
            if ticker:
                st.markdown("##### Fixture ticker")
                st.dataframe(pd.DataFrame(ticker), hide_index=True, width="stretch")
            else:
                st.caption("No remaining fixtures were available in the API response for this player.")

            ph = (player_summaries.get(selected["id"]) or {}).get("history", [])
            if ph:
                hist_df = pd.DataFrame(sorted(ph, key=lambda x: int(x.get("round") or 0))[-10:])
                hist_df["GW"] = hist_df["round"].astype(int)
                hist_df["Points"] = hist_df["total_points"].astype(float)
                hist_df["Minutes"] = hist_df["minutes"].astype(float)
                with st.expander("Recent gameweeks"):
                    st.dataframe(hist_df[["GW", "Points", "Minutes"]], hide_index=True, width="stretch")

    # ── Forward look ────────────────────────────────────────────────────────────
    if active_panel == panel_names[1]:
        st.subheader("Matchup outlook")
        st.caption(
            "Effective ownership is squad ownership plus weighted captain exposure. "
            "Expected Swing asks: how much could a player matter in the most asymmetric IML matchup?"
        )

        frows = sorted(
            ownership_rows,
            key=lambda r: (-abs(r["swing_potential"]), -r["projected_ppg"])
        )
        swing_df = pd.DataFrame(frows[:20])[
            ["name", "ownership_pct", "effective_ownership_pct",
             "projected_ppg", "max_team_ownership_diff", "swing_potential"]
        ].copy()
        swing_df.columns = ["Player", "Overlap %", "Effective Own %",
                            "Proj/GW", "Max Team Diff", "Expected Swing"]
        swing_df = swing_df.round(2)
        st.dataframe(swing_df, hide_index=True, width="stretch")

        st.divider()
        st.markdown("##### Provisional H2H outcome estimate")
        st.caption("Draw band: ±5 points. Uses clock-scaled fixture forecasts; excludes future autosubs and bonus revisions. Select the IML captain managers to match your league submissions.")
        matchups = gw_matchups(gw)
        if not matchups:
            st.info(f"No usable fixtures are available for GW{gw}.")
        else:
            matchup_labels = [f'{m["team_a"]} vs {m["team_b"]}' for m in matchups]
            chosen = st.selectbox("Fixture", matchup_labels, key="analytics_live_matchup")
            m = matchups[matchup_labels.index(chosen)]
            with st.spinner("Calculating live ownership state…"):
                try:
                    ma = cached_league_managers(TEAMS[m["team_a"]]["league_id"])
                    mb = cached_league_managers(TEAMS[m["team_b"]]["league_id"])
                    ids_a = [str(x["id"]) for x in ma]
                    ids_b = [str(x["id"]) for x in mb]
                    cap_a = st.selectbox("IML captain — " + m["team_a"], range(-1, len(ma)),
                        format_func=lambda i: "No IML captain" if i < 0 else ma[i].get("manager", str(i)), key="live_cap_a")
                    cap_b = st.selectbox("IML captain — " + m["team_b"], range(-1, len(mb)),
                        format_func=lambda i: "No IML captain" if i < 0 else mb[i].get("manager", str(i)), key="live_cap_b")
                    pa = cached_team_picks(tuple(ids_a), cap_a, gw, "A", player_map, ma)
                    pb = cached_team_picks(tuple(ids_b), cap_b, gw, "B", player_map, mb)
                    live_scores, _, _ = cached_live_scores(gw)
                    win_a, draw_p, win_b, future = h2h_live_win_probability(
                        pa, pb, player_map, live_scores, analytics_map, gw=gw
                    )
                    l1, l2, l3 = st.columns(3)
                    l1.metric(m["team_a"], f"{win_a:.1f}%")
                    l2.metric("Draw", f"{draw_p:.1f}%")
                    l3.metric(m["team_b"], f"{win_b:.1f}%")
                    st.caption(
                        f"Simulation uses locked/live points plus projected points for "
                        f"{len(future)} differential/remaining player exposures."
                    )
                except Exception as e:
                    st.warning(f"Live probability unavailable for this matchup: {e}")

    # ── Manager lab ─────────────────────────────────────────────────────────────
    if active_panel == panel_names[2]:
        st.subheader("Manager review")
        st.caption(
            "Review scoring, captain decisions, bench points and transfer returns for one manager."
        )
        manager_histories = cached_all_manager_histories((mid,))
        # Fetch only this member's historical squad picks and transfer players.
        transfer_log = cached_manager_transfers(mid)
        transfer_player_ids = set()
        for t in transfer_log or []:
            for k in ("element_in", "element_out"):
                try:
                    transfer_player_ids.add(int(t[k]))
                except Exception:
                    pass
        historic_picks = cached_historical_picks(mid, gw)
        historic_ids = set(_squad_player_ids(historic_picks))
        lab_player_ids = tuple(sorted(historic_ids | transfer_player_ids))
        lab_histories = cached_player_summaries(lab_player_ids)

        snapshot = member_snapshot(mid, gw, lab_histories, player_map)
        transfer_rows = manager_transfer_roi(mid, transfer_log, lab_histories)

        latest = manager_histories.get(mid, {}).get("gw_scores", {})
        latest_total = 0
        if latest:
            last_gw = max(latest)
            latest_total = latest[last_gw].get("total_points") or 0

        # Chip ROI is surfaced from the manager's own history and chip usage.
        chip_events = manager_histories.get(mid, {}).get("chips", [])
        chip_baseline = snapshot["avg_gw"]
        chip_rows = []
        for c in chip_events:
            ev = int(c.get("event") or 0)
            pts = manager_histories.get(mid, {}).get("gw_scores", {}).get(ev, {}).get("points", 0) or 0
            chip_rows.append({"Chip": c["name"].upper(), "GW": ev, "Points": pts, "ROI vs avg": round(pts - chip_baseline, 1)})

        m1, m2, m3 = st.columns(3)
        m1.metric("Season points", int(latest_total))
        m2.metric("Average gameweek", f'{snapshot["avg_gw"]:.1f}')
        m3.metric("Consistency", f'{snapshot["consistency"]:.0f}/100')
        with st.expander("Captain and bench decisions"):
            m4, m5 = st.columns(2)
            m4.metric("Captain points missed", f'{snapshot["captain_regret"]:.1f}')
            m5.metric("Bench points", f'{snapshot["bench_wasted"]:.1f}')
            st.caption("Captain points missed compares the chosen captain with the highest-scoring starter. Bench points count unused substitutes.")

        if snapshot["weekly"]:
            with st.expander("Gameweek history"):
                st.dataframe(pd.DataFrame(snapshot["weekly"]).round(1), hide_index=True, width="stretch")

        st.markdown("##### Transfer ROI")
        if transfer_rows:
            tdf = pd.DataFrame(transfer_rows)
            tdf["Incoming"] = tdf["incoming"].map(lambda x: player_map.get(x, {}).get("name", str(x)))
            tdf["Outgoing"] = tdf["outgoing"].map(lambda x: player_map.get(x, {}).get("name", str(x)))
            tdf = tdf[["gw", "Incoming", "Outgoing", "incoming_pts", "outgoing_pts", "hit", "roi"]]
            tdf.columns = ["GW", "Incoming", "Outgoing", "In pts (4GW)", "Out pts (4GW)", "Hit", "Transfer ROI"]
            st.dataframe(tdf.round(1), hide_index=True, width="stretch", height=300)
            st.caption("Transfer ROI = incoming 4-GW points − outgoing 4-GW points − transfer hit.")
        else:
            st.caption("No transfer history is available yet.")

        st.markdown("##### Chip ROI")
        if chip_rows:
            st.dataframe(pd.DataFrame(chip_rows), hide_index=True, width="stretch")
            st.caption("For WC/FH this is impact versus the manager's average GW score; it is directional, not causal.")
        else:
            st.caption("No chip usage in the available history.")

    # ── Template XI ──────────────────────────────────────────────────────────────
    if active_panel == panel_names[3]:
        st.subheader("IML Template XI")
        st.caption(
            "A template is built only from highly-owned IML assets. The IML XI score blends "
            "ownership protection, projected output, minutes security, fixture-adjusted form and DEFCON reliability."
        )
        eligible = [r for r in ownership_rows if r["ownership_pct"] >= ownership_threshold]
        template = build_iml11(ownership_rows, ownership_threshold)

        c1, c2, c3 = st.columns(3)
        c1.metric("Eligible highly-owned assets", len(eligible))
        c2.metric("Template players", len(template))
        c3.metric("Template projected / GW", f'{sum(x["projected_ppg"] for x in template):.1f}')

        if template:
            template_df = pd.DataFrame(template)[
                ["name", "position", "club", "ownership_pct", "iml11_score",
                 "minutes_security", "projected_ppg", "fixture_adjusted_form", "defcon_reliability"]
            ].copy()
            template_df.columns = [
                "Player", "Pos", "Club", "IML Own %", "IML11 Score",
                "Min Security", "Proj/GW", "FDR Form", "DEFCON Rel."
            ]
            st.dataframe(template_df.round(2), hide_index=True, width="stretch")

        else:
            st.warning("No players meet the current highly-owned threshold. Lower the threshold in the sidebar.")

        st.divider()
        st.subheader("Low-owned options")
        st.caption(
            "Compare less popular players by expected points, form and playing-time reliability."
        )
        diffs = differential_finder(ownership_rows)
        if diffs:
            ddf = pd.DataFrame(diffs)[
                ["name", "position", "club", "ownership_pct", "projected_ppg",
                 "minutes_security", "fixture_adjusted_form", "reliability", "differential_score"]
            ].copy()
            ddf.columns = [
                "Player", "Pos", "Club", "IML Own %", "Proj/GW",
                "Min Security", "FDR Form", "Reliability", "Differential Score"
            ]
            st.dataframe(ddf.round(2), hide_index=True, width="stretch")



    # ── Manager rankings ───────────────────────────────────
    if active_panel == panel_names[4]:
        st.subheader("Manager rankings")
        manager_histories = cached_all_manager_histories(manager_ids)
        chip_histories = {mid: h.get("chips", []) for mid, h in manager_histories.items()}
        rankings = manager_power_rankings(registry, manager_histories, chip_histories)
        if rankings:
            rdf = pd.DataFrame(rankings)[
                ["rank", "manager", "team", "power_score", "total_points", "recent_mean", "consistency", "chip_roi"]
            ].copy()
            rdf.columns = ["Rank", "Manager", "Team", "Power Score", "Season Pts", "Recent Avg", "Consistency", "Chip ROI"]
            st.dataframe(rdf.round(2), hide_index=True, width="stretch", height=560)



# ── Main ───────────────────────────────────────────────────────────────────────
st.title("IML Scorecards")
st.caption({"Matchups": "Follow this gameweek. Compare scores and the players making the difference.", "Players": "Compare ownership, upcoming fixtures and expected points.", "Managers": "Review season performance, transfers and captain decisions."}[mode])

try:
    if mode in ("Players", "Managers"):
        render_league_analytics(int(gw), int(horizon), float(ownership_threshold))
    else:
        render_weekly_matchups(int(gw), no_live, no_summary, no_chips)
except (Exception, SystemExit) as exc:
    st.error(f"This view could not load: {exc}")
    st.caption("Retry the view; failed API requests are not stored as valid snapshots.")

with st.sidebar.expander("Data diagnostics"):
    st.caption("Process totals since startup; Streamlit memory-cache hits are not counted. Request seconds are cumulative worker time, not page load time.")
    with _DATA_LOCK:
        stats = dict(_DATA_STATS)
    st.json(stats)
