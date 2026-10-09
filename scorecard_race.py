"""Race replay and factual commentary from engine scoring events."""
from html import escape
import math
import hashlib


# Stable variety: an unchanged match retains its wording across reruns.
HEADLINES = {
    "level": ["Honours even, nerves uneven", "A draw with a side of drama", "The points refuse to pick a side", "Level pegging, elevated pulse", "Nobody blinked. Nobody leads.", "A dead heat with live wires", "All square in the points department", "Two clubs, one scoreline"],
    "tight": ["{side} edges the arm-wrestle", "{side} leads by a fingernail", "The advantage wears {side} colours", "{side} has the slimmest of cushions", "{side} ahead. Nobody exhale.", "{side} owns the edge of the seat", "{side} is winning the staring contest", "{side} has room for exactly one breath"],
    "ahead": ["{side} is setting the tempo", "{side} has the points talking", "The scoreboard leans toward {side}", "{side} puts daylight in the gap", "{side} holds the remote", "{side} has a cushion, not a sofa", "{side} is doing the heavy lifting", "The arithmetic smiles on {side}"],
    "wide": ["{side} turns up the volume", "{side} brings the points party", "{side} has opened the escape hatch", "{side} has the scoreboard purring", "{side} stretches the points elastic", "{side} takes the scenic route ahead", "The gap has a {side} postcode", "{side} is collecting points, not compliments"],
    "player": ["{player}: the swing king", "{player} moves the furniture", "The {player} effect", "{player} brings the plot twist", "{player} makes the spreadsheet sweat", "{player} has entered the group chat", "{player} puts the differential in difference", "{player}: small nameplate, big footprint", "{player} tips the scales", "{player} writes the points paragraph"],
    "comeback": ["The plot has changed shirts", "Behind you? Not anymore.", "The comeback has receipts", "A deficit, politely declined", "Reverse gear, forward momentum", "Somebody rewrote the script", "From chasing shadows to setting the pace", "The points pendulum bites back"],
    "volatile": ["Pass the lead, pass the popcorn", "The advantage won't sit still", "A points tug-of-war", "The lead is on loan", "The scoreboard needs a seatbelt", "Plot twists with bonus points", "Both clubs want the last word", "The points pendulum has opinions"],
    "steady": ["The gap tells its own story", "No musical chairs at the top", "The scoreboard keeps its composure", "A quieter kind of pressure", "The numbers know their lines", "The points have picked a lane", "The lead holds the microphone", "Less pinball, more pressure"],
}
BANTER = {
    "level": ["Even the calculator is sitting on the fence.", "The group chat can argue; the numbers cannot.", "Bragging rights remain in the waiting room.", "Nobody gets the smug screenshot yet."],
    "tight": ["A cushion this small comes with a health warning for fingernails.", "The smug screenshot can wait.", "One return could rearrange the group chat.", "Plenty of tension; very little breathing room."],
    "ahead": ["A little breathing room; no licence to get comfortable.", "The calculator approves. The opposition probably doesn't.", "Useful daylight. Still worth watching the rear-view mirror.", "The group chat is warming up its excuses."],
    "wide": ["A healthy gap; fantasy football still refuses to sign guarantees.", "The opposition's calculator would like a quiet word.", "That's a scoreline with screenshot potential.", "The points are doing the talking, rather loudly."],
    "player": ["That's the kind of differential that gets a group chat typing.", "Same player, different exposure, very different moods.", "Ownership counts. This is the receipt.", "A reminder that a shared player isn't always a shared outcome."],
    "comeback": ["Earlier screenshots may need deleting.", "The first draft of the bragging rights has been recalled.", "That's a plot twist with the maths attached.", "A reminder to save the victory lap for later."],
}


def phrase(category, seed, **values):
    index = int.from_bytes(hashlib.sha256(f"{category}:{seed}".encode()).digest()[:8], "big")
    return HEADLINES[category][index % len(HEADLINES[category])].format(**values)


def banter(category, seed):
    index = int.from_bytes(hashlib.sha256(f"banter:{category}:{seed}".encode()).digest()[:8], "big")
    return BANTER[category][index % len(BANTER[category])]


def race_insights(events, home, away):
    if not events:
        return []
    leads = [0] + [e["score_a_after"] - e["score_b_after"] for e in events]
    players = {}
    changes, previous = 0, 0
    for index, event in enumerate(events, 1):
        player = players.setdefault(event["player_id"], {"name":event["name"],"swing":0,"index":index})
        player["swing"] += event["pts_swing_a"] - event["pts_swing_b"]
        player["index"] = index
        sign = 1 if leads[index] > 0 else -1 if leads[index] < 0 else 0
        if sign:
            changes += int(previous != 0 and sign != previous)
            previous = sign
    final = leads[-1]
    side = home if final > 0 else away
    category = "level" if final == 0 else "tight" if abs(final) <= 5 else "ahead" if abs(final) <= 30 else "wide"
    seed = f"{home}|{away}|{len(events)}|{final}|{changes}"
    headline = phrase(category, seed, side=side)
    summary = f"A {abs(final)}-point lead in player scoring after {len(events)} scoring events." if final else f"Level on player scoring after {len(events)} scoring events."
    insights = [(headline, summary + " " + banter(category, seed))]
    biggest = max(players.values(),key=lambda p:abs(p["swing"]))
    if biggest["swing"]:
        player_side = home if biggest["swing"] > 0 else away
        player_seed = f"{seed}|{biggest['name']}|{biggest['swing']}"
        insights.append((phrase("player", player_seed, player=biggest["name"]),
            f"{biggest['name']} has swung {abs(biggest['swing'])} net points toward {player_side}. " + banter("player", player_seed)))
    if final > 0 and min(leads) < 0:
        insights.append((phrase("comeback", seed),f"{home} trailed by as much as {abs(min(leads))} player points and now leads by {final}. " + banter("comeback", seed)))
    elif final < 0 and max(leads) > 0:
        insights.append((phrase("comeback", seed),f"{away} trailed by as much as {max(leads)} player points and now leads by {abs(final)}. " + banter("comeback", seed)))
    else:
        insights.append((phrase("volatile" if changes else "steady", seed),f"{changes} lead change{'s' if changes != 1 else ''} · largest gap {max(abs(v) for v in leads)} player points. Ties do not count as a lead change."))
    return insights


def commentary_html(insights):
    return '<div class="commentary-grid">'+''.join(
        f'<div class="commentary-card"><div class="commentary-kicker">MATCH INTELLIGENCE / {i+1:02d}</div><h3>{escape(title)}</h3><p>{escape(body)}</p></div>'
        for i,(title,body) in enumerate(insights))+'</div>'


def build_race_figure(events, home, away, replay=True):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    x = list(range(len(events)+1))
    a = [0]+[e["score_a_after"] for e in events]
    b = [0]+[e["score_b_after"] for e in events]
    lead = [pa-pb for pa,pb in zip(a,b)]
    labels = ["Kickoff"]+[f"{escape(e['name'])} · {escape(e['stat'].replace('_',' '))} ({e['raw_points']:+d} pts)" for e in events]
    players = {}
    for i,event in enumerate(events,1):
        p=players.setdefault(event["player_id"],dict(name=event["name"],swing=0,index=i))
        p["swing"]+=event["pts_swing_a"]-event["pts_swing_b"]
        p["index"]=i
    stars=sorted([p for p in players.values() if abs(p["swing"])>=10],key=lambda p:-abs(p["swing"]))[:6]

    def traces(end):
        sl=slice(0,end+1)
        visible=[p for p in stars if p['index']<=end]
        return [
            go.Scatter(x=x[sl],y=[max(v,0) for v in lead[sl]],mode="lines",line=dict(shape="hv",width=0),fill="tozeroy",fillcolor="rgba(28,159,109,.22)",name=home,hoverinfo="skip"),
            go.Scatter(x=x[sl],y=[min(v,0) for v in lead[sl]],mode="lines",line=dict(shape="hv",width=0),fill="tozeroy",fillcolor="rgba(21,145,185,.22)",name=away,hoverinfo="skip"),
            go.Scatter(x=x[sl],y=lead[sl],mode="lines+markers",line=dict(shape="hv",color="#e4e8ed",width=1.6),marker=dict(size=3),name="Lead",text=labels[sl],hovertemplate="%{text}<br>Lead %{y:+d}<extra></extra>"),
            go.Scatter(x=[p['index'] for p in visible],y=[lead[p['index']] for p in visible],mode="markers+text",showlegend=False,
                marker=dict(symbol="star",size=15,color=["#ffb544" if p['swing']>0 else "#ff557a" for p in visible],line=dict(color="#fff",width=1.2)),
                text=[f"{escape(p['name'])} {p['swing']:+d}" for p in visible],textposition="top center",textfont=dict(size=10),hovertemplate="%{text}<extra></extra>"),
            go.Scatter(x=x[sl],y=a[sl],mode="lines",name=home,legendgroup="scores",line=dict(shape="hv",color="#1c9f6d",width=2.5),text=labels[sl],hovertemplate="%{text}<br>%{y} pts<extra></extra>"),
            go.Scatter(x=x[sl],y=b[sl],mode="lines",name=away,legendgroup="scores",line=dict(shape="hv",color="#1591b9",width=2.5),text=labels[sl],hovertemplate="%{text}<br>%{y} pts<extra></extra>")]
    fig=make_subplots(rows=2,cols=1,vertical_spacing=.16,row_heights=[.6,.4],subplot_titles=["WHO HAS THE EDGE?","THE POINTS RACE"])
    for i,trace in enumerate(traces(len(events))):
        fig.add_trace(trace,row=1 if i<4 else 2,col=1)
    # Bound replay payload while preserving the final event.
    indices=sorted(set([0,len(events)]+list(range(1,len(events)+1,max(1,math.ceil(len(events)/80))))))
    fig.frames=[go.Frame(name=str(i),data=traces(i),traces=list(range(6))) for i in indices] if replay else []
    fig.add_hline(y=0,line_dash="dot",line_color="#6f7b8c",row=1,col=1)
    gap=max([abs(v) for v in lead]+[1])
    fig.update_yaxes(title_text=f"{away} ◀ · Lead · ▶ {home}",range=[min(lead)-gap*.2,max(lead)+gap*.25],row=1,col=1)
    fig.update_yaxes(title_text="Cumulative player points",range=[min(a+b)-10,max(a+b)+20],row=2,col=1)
    fig.update_xaxes(range=[0,len(events)+max(1,len(events)*.03)],showticklabels=False,title_text="Scoring events · engine order")
    fig.update_layout(height=770,paper_bgcolor="#0e131b",plot_bgcolor="#0e131b",font=dict(color="#bfcad8",size=11),
        margin=dict(l=20,r=25,t=85,b=105),hovermode="closest",legend=dict(orientation="h",y=1.08,x=0),
        updatemenus=[dict(type="buttons",direction="left",x=0,y=-.13,showactive=False,active=-1,bgcolor="#1c293a",bordercolor="#33465b",font=dict(color="#edf4ff",size=12),buttons=[
            dict(label="▶ Replay the gameweek",method="animate",args=[None,dict(frame=dict(duration=130,redraw=True),transition=dict(duration=0),fromcurrent=False,mode="immediate")]),
            dict(label="Ⅱ Pause",method="animate",args=[[None],dict(frame=dict(duration=0,redraw=False),mode="immediate")])])],
        sliders=[dict(x=.48,len=.51,y=-.13,active=len(indices)-1,currentvalue=dict(prefix="Event ",font=dict(color="#bfcad8")),
            bgcolor="#33465b",bordercolor="#33465b",steps=[dict(label=str(i),method="animate",args=[[str(i)],dict(mode="immediate",frame=dict(duration=0,redraw=True),transition=dict(duration=0))]) for i in indices])])
    if not replay:
        fig.update_layout(updatemenus=[], sliders=[], height=680, margin=dict(l=20,r=25,t=85,b=35))
    fig.update_yaxes(gridcolor="#29313e",zeroline=False)
    fig.update_xaxes(showgrid=False)
    for annotation in fig.layout.annotations:
        annotation.font=dict(size=11,color="#7990a9")
    return fig


def demo_events(home_total,away_total):
    names=["Opening minutes","Captain return","Clean sheet","Differential goal","Bonus points","Late return"]
    af=[.10,.26,.30,.57,.73,1]
    bf=[.14,.21,.43,.52,.82,1]
    events=[]
    last_a=last_b=0
    for i,(fa,fb) in enumerate(zip(af,bf)):
        a,b=round(home_total*fa),round(away_total*fb)
        events.append(dict(player_id=i,name=names[i],stat="demo_event",raw_points=1,
            pts_swing_a=a-last_a,pts_swing_b=b-last_b,score_a_after=a,score_b_after=b))
        last_a,last_b=a,b
    return events
