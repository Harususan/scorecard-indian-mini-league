"""Offline request-count and cache correctness checks; no live-service benchmark."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch
import scorecard_data as data
import scorecard_race as race

class SpeedTests(unittest.TestCase):
    def setUp(self):
        data.clear_endpoint_cache(include_squads=True)
    def tearDown(self):
        data.clear_endpoint_cache(include_squads=True)
    def response(self, path):
        if path == "bootstrap-static/":
            return {"elements":[{"id":10,"first_name":"Test","second_name":"Player","web_name":"Test","element_type":3,"team":1}],"events":[]}
        if path.startswith("event/"):
            return {"elements":[{"id":10,"stats":{"total_points":5}}]}
        if path.startswith("fixtures/"): return []
        if path.startswith("leagues-classic/"):
            club=int(path.split('/')[1])
            return {"standings":{"results":[{"entry":club*10+i,"player_name":f"Manager {i}"} for i in range(4)]}}
        return {"picks":[{"element":10,"position":1,"is_captain":True,"is_vice_captain":False}],"entry_history":{}}
    def test_live_refresh_reuses_rosters_and_squads(self):
        clubs={f"Club {i}":{"league_id":str(i)} for i in range(20)}
        with patch.object(data,"TEAMS",clubs),patch.object(data,"_request_json",side_effect=self.response) as request:
            first=data.load_snapshot(8)
            self.assertEqual(len(first["teams"]),20)
            self.assertEqual(request.call_count,103)
            data.clear_endpoint_cache()
            second=data.load_snapshot(8)
            self.assertEqual(request.call_count,105) # Live + fixtures only.
            self.assertEqual(second["teams"],first["teams"])
            data.clear_endpoint_cache(include_squads=True)
            data.load_snapshot(8)
            self.assertEqual(request.call_count,208)
    def test_single_match_loads_only_two_clubs(self):
        clubs={f"Club {i}":{"league_id":str(i)} for i in range(20)}
        with patch.object(data,"TEAMS",clubs),patch.object(data,"_request_json",side_effect=self.response) as request:
            result=data.load_snapshot(8,("Club 0","Club 1"))
            self.assertEqual(set(result["teams"]),{"Club 0","Club 1"})
            self.assertEqual(request.call_count,13)
    def test_concurrent_sessions_share_misses_and_get_separate_payloads(self):
        def request(path):
            time.sleep(.03)
            return self.response(path)
        with patch.object(data,"_request_json",side_effect=request) as http:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(lambda _:data.fetch("bootstrap-static/"),range(8)))
            self.assertEqual(http.call_count,1)
            results[0]["elements"][0]["web_name"]="Changed"
            self.assertEqual(data.fetch("bootstrap-static/")["elements"][0]["web_name"],"Test")
    def test_failure_and_incomplete_responses_are_not_cached(self):
        with patch.object(data,"_request_json",return_value={}) as request:
            for _ in range(2):
                with self.assertRaises(ValueError): data.fetch("bootstrap-static/")
            self.assertEqual(request.call_count,2)
    def test_expired_endpoint_refetches(self):
        with patch.object(data,"_request_json",side_effect=self.response) as request:
            data.fetch("bootstrap-static/")
            with data._CACHE_LOCK:
                expiry,payload=data._ENDPOINT_CACHE["bootstrap-static/"]
                data._ENDPOINT_CACHE["bootstrap-static/"]=(0,payload)
            data.fetch("bootstrap-static/")
            self.assertEqual(request.call_count,2)
    def test_static_race_omits_animation_frames_without_changing_scores(self):
        try: import plotly
        except ImportError: self.skipTest("Plotly required")
        events=race.demo_events(250,280)
        animated=race.build_race_figure(events,"A","B")
        static=race.build_race_figure(events,"A","B",replay=False)
        self.assertFalse(static.frames)
        self.assertEqual(list(static.data[2].y),list(animated.data[2].y))
        self.assertLess(len(static.to_json()),len(animated.to_json()))

if __name__=="__main__":unittest.main()
