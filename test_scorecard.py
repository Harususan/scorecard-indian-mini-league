"""Offline scoring and failure-path checks for the separate scorecard app."""
import unittest
from unittest.mock import patch
import scorecard_data as data
import scorecard_views as view
import scorecard_race as race

class ScorecardTests(unittest.TestCase):
    def member(self, chip=None):
        return data.score_manager({"id":"1","manager":"Example"}, {
            "picks":[{"element":10,"position":1,"is_captain":True,"multiplier":3 if chip else 2},
                     {"element":11,"position":2,"is_captain":False,"multiplier":1}],
            "active_chip":chip,"entry_history":{"event_transfers_cost":4}}, {10:5,11:2})

    def test_captain_doubles_net_score_and_hit(self):
        manager=self.member()
        team={"team":"Club","managers":[manager]}
        self.assertEqual(data.captain_team(team)["total"],8)
        self.assertEqual(data.captain_team(team,"1")["total"],16)
        self.assertFalse(data.captain_team(team)["confirmed"])
        self.assertTrue(data.captain_team(team,"1")["confirmed"])

    def test_triple_captain_exposure_and_score_agree(self):
        team=data.captain_team({"team":"Club","managers":[self.member("3xc")]},"1")
        self.assertEqual(team["total"],26)
        picks=data.race_picks(team)
        self.assertEqual([p["count"] for p in picks[0]["picks"]],[6,2])

    def test_missing_picks_not_zero_score(self):
        with self.assertRaises(ValueError):
            data.score_manager({"id":"1"},{"picks":[]},{})

    def test_explanations_are_compatible_with_race(self):
        elements=[{"id":10,"explain":[{"fixture":1,"stats":[
            {"identifier":"goals_scored","points":5,"value":1},
            {"identifier":"yellow_cards","points":0,"value":0}]}]}]
        self.assertEqual(data.flatten_explanations(elements),{10:[{"fixture":1,"stat":"goals_scored","points":5,"value":1}]})

    def test_failed_club_excluded_from_snapshot(self):
        def fetch(path):
            if path=='bootstrap-static/': return {"elements":[{"id":10,"web_name":"Player","first_name":"Test","second_name":"Player","element_type":3,"team":1}],"teams":[{"id":1,"name":"Club"}]}
            if path.endswith('/live/'): return {"elements":[{"id":10,"stats":{"total_points":5}}]}
            return []
        def team(name,gw,scores):
            if name=='Missing': raise ValueError('No squad')
            return {"team":name,"managers":[]}
        with patch.object(data,'TEAMS',{'Good':{},'Missing':{}}), patch.object(data,'fetch',fetch), patch.object(data,'load_team',team):
            result=data.load_snapshot(2)
        self.assertEqual(set(result['teams']),{'Good'})
        self.assertEqual(result['errors'],{'Missing':'No squad'})

    def test_empty_live_response_rejected(self):
        with patch.object(data,'fetch',return_value={}):
            with self.assertRaises(ValueError): data.load_snapshot(2)

    def test_demo_and_html_escaping(self):
        snap=data.demo_snapshot()
        self.assertTrue(snap['demo'])
        self.assertEqual(len(snap['teams']),20)
        self.assertEqual(len(data.matchups(snap)),10)
        team=data.captain_team({'team':'<script>alert(1)</script>','managers':[self.member()]})
        markup=view.league_sheet([team])
        self.assertNotIn('<script>',markup)
        self.assertIn('&lt;script&gt;',markup)

class POTWTests(unittest.TestCase):
    def snapshot(self):
        members=[dict(id="1",manager="One",raw=74,hit=-4,points=70,chip="3xc",contribution=140),
                 dict(id="2",manager="Two",raw=70,hit=0,points=70,chip=None,contribution=70),
                 dict(id="3",manager="Three",raw=73,hit=-8,points=65,chip=None,contribution=65)]
        return {"teams":{"Arsenal":{"team":"Arsenal","managers":members}}}
    def test_net_points_ignore_iml_multiplier_and_share_ranks(self):
        rows=data.member_leaderboard(self.snapshot())
        self.assertEqual([row["points"] for row in rows],[70,70,65])
        self.assertEqual([row["rank"] for row in rows],[1,1,3])
        self.assertEqual(rows[0]["chip"],"3xc")
        self.assertEqual(rows[0]["manager_id"],"1")
    def test_tied_leader_and_empty_race_copy(self):
        rows=data.member_leaderboard(self.snapshot())
        self.assertIn("2 members are tied on 70 points",view.potw_hero(8,rows))
        self.assertIn("awaits",view.potw_hero(8,[]))
        self.assertEqual(data.member_leaderboard({"teams":{}}),[])

class RaceTests(unittest.TestCase):
    def events(self):
        return [dict(player_id=1,name="Alpha",stat="goals_scored",raw_points=5,
                    pts_swing_a=5,pts_swing_b=15,score_a_after=5,score_b_after=15),
                dict(player_id=2,name="Bravo",stat="goals_scored",raw_points=5,
                    pts_swing_a=30,pts_swing_b=5,score_a_after=35,score_b_after=20)]

    def test_comeback_and_shared_exposure_commentary(self):
        insights=race.race_insights(self.events(),"Home","Away")
        text=" ".join(body for title,body in insights)
        self.assertIn("Bravo has swung 25 net points toward Home",text)
        self.assertIn("trailed by as much as 10",text)
        self.assertIn("now leads by 15",text)

    def test_ties_do_not_count_as_lead_changes(self):
        events=self.events()
        events.append(dict(player_id=3,name="Equaliser",pts_swing_a=0,pts_swing_b=15,score_a_after=35,score_b_after=35))
        text=" ".join(body for title,body in race.race_insights(events,"Home","Away"))
        self.assertIn("1 lead change",text)
        self.assertIn("Level on player scoring",text)

    def test_headline_variety_is_stable_and_situation_specific(self):
        self.assertEqual(race.race_insights(self.events(),"Home","Away"),race.race_insights(self.events(),"Home","Away"))
        choices={race.phrase("tight",str(i),side="Home") for i in range(100)}
        self.assertGreaterEqual(len(choices),6)
        self.assertTrue(all("Home" in headline for headline in choices))
        self.assertGreaterEqual(sum(len(bank) for bank in race.HEADLINES.values()),60)
        self.assertIn(race.race_insights(self.events(),"Home","Away")[2][0],race.HEADLINES["comeback"])

    def test_replay_contains_two_charts_and_complete_final_frame(self):
        try:
            import plotly
        except ImportError:
            self.skipTest("Plotly is required for figure validation")
        fig=race.build_race_figure(self.events(),"Home","Away")
        self.assertEqual(len(fig.data),6)
        self.assertEqual(len(fig.frames),3)
        self.assertEqual(list(fig.frames[-1].data[2].y),[0,-10,15])
        self.assertEqual(list(fig.frames[0].data[3].x),[])
        self.assertEqual(fig.data[4].yaxis,"y2")
        self.assertIn("Replay",fig.layout.updatemenus[0].buttons[0].label)
        fig.to_json()

if __name__=='__main__': unittest.main()
