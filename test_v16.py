"""Offline regression tests: python -m unittest discover -s . -p 'test_v16.py' -v"""
import ast
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
APP = ROOT / 'streamlit_app_v16.py'
if not APP.exists():
    APP = ROOT / 'streamlit_app.py'
# Load functions without launching the reactive Streamlit UI or requiring network.
tree = ast.parse(APP.read_text())
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
for node in functions:
    node.decorator_list = []
warns = []
engine = SimpleNamespace(BASE='https://fpl.invalid/api', POSITIONS={1:'GKP',2:'DEF',3:'MID',4:'FWD'})
ns = dict(globals(), engine=engine, st=SimpleNamespace(warning=warns.append),
          _DATA_LOCK=threading.Lock(), _DATA_STATS={'network':0,'disk_hits':0,'failures':0,'seconds':0})
exec(compile(ast.Module(body=functions, type_ignores=[]), str(APP), 'exec'), ns)

def call(name, *args, **kwargs): return ns[name](*args, **kwargs)

def bootstrap():
    return {'events':[{'id':1,'finished':True},{'id':2,'is_current':True}],
            'teams':[{'id':1,'name':'Team A'},{'id':2,'name':'Team B'}],
            'elements':[{'id':10,'team':1,'element_type':3,'web_name':'Player','minutes':90,
                'points_per_game':'5','form':'5','expected_goals':'0.5','expected_assists':'0.2'}]}

def fixture(event=2, finished=False, minutes=0, started=False):
    return {'id':event*10,'event':event,'team_h':1,'team_a':2,
            'team_h_difficulty':3,'team_a_difficulty':3,
            'finished':finished,'started':started,'minutes':minutes}

class Regressions(unittest.TestCase):
    def test_safe_float_nonfinite(self):
        self.assertEqual(call('_safe_float', 'nan', 7),7)
        self.assertEqual(call('_safe_float', 'inf', 7),7)
    def test_blank_projection_zero(self):
        row=call('build_player_analytics',bootstrap(),[],start_gw=2,horizon=3)[0]
        self.assertEqual(row['projected_next'],0)
        self.assertEqual(row['projected_ppg'],0)
    def test_horizon_gameweeks_not_fixture_count(self):
        fs=[fixture(2),fixture(2),fixture(4),fixture(5)]
        row=call('build_player_analytics',bootstrap(),fs,start_gw=2,horizon=3)[0]
        self.assertEqual([f['event'] for f in row['fixtures_next']],[2,2,4])
        self.assertAlmostEqual(row['projected_ppg'],row['projected_next']/3)
    def test_bootstrap_xgi_fallback(self):
        row=call('build_player_analytics',bootstrap(),[fixture()],start_gw=2)[0]
        self.assertAlmostEqual(row['xgi90'],0.7)
    def test_injured_availability(self):
        b=bootstrap();b['elements'][0]['chance_of_playing_next_round']=0
        row=call('build_player_analytics',b,[fixture()],start_gw=2)[0]
        self.assertEqual(row['projected_next'],0)
    def test_csv_valid(self):
        self.assertEqual(call('parse_projection_csv',b'player_id,gw,xpts,xmins\n10,2,8,170\n',[10]),{(10,2):8})
    def test_csv_bad_cases(self):
        cases=['10,2,nan','10,2,inf','10.5,2,5','10,0,5','11,2,5','10,2,101','10,2,5\n10,2,6']
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                call('parse_projection_csv',('player_id,gw,xpts\n'+rows+'\n').encode(),[10])
    def test_csv_missing_columns(self):
        with self.assertRaises(ValueError): call('parse_projection_csv',b'id,gw,xpts\n10,2,5\n',[10])
    def test_external_partial_horizon(self):
        row=call('build_player_analytics',bootstrap(),[fixture(2),fixture(3)],start_gw=2,horizon=3)[0]
        original=row['projections_by_gw'][3]
        call('apply_external_projections',[row],{(10,2):12},2,3)
        self.assertAlmostEqual(row['projected_next'],12+original)
        self.assertEqual(row['projection_source'],'CSV + fallback')
        self.assertEqual(row['fixtures_next'][0]['projection'],12)
    def test_dgw_manager_score(self):
        h={10:{'history':[{'round':2,'total_points':4},{'round':2,'total_points':6}]}}
        m={'picks':[{'element':10,'position':1,'multiplier':2}]}
        self.assertEqual(call('simulate_manager_score',m,2,h,{}),(20,0,0))
    def test_autosub_and_benchboost_multipliers(self):
        h={1:{'history':[{'round':2,'total_points':0}]},2:{'history':[{'round':2,'total_points':8}]}}
        m={'picks':[{'element':1,'position':2,'multiplier':0},{'element':2,'position':13,'multiplier':1}]}
        self.assertEqual(call('simulate_manager_score',m,2,h,{})[:2],(8,0))
        m['active_chip']='bboost'
        self.assertEqual(call('simulate_manager_score',m,2,h,{})[:2],(8,0))
    def test_captain_regret_extra_multiplier(self):
        h={1:{'history':[{'round':2,'total_points':3}]},2:{'history':[{'round':2,'total_points':10}]}}
        m={'picks':[{'element':1,'position':1,'multiplier':2},{'element':2,'position':2,'multiplier':1}]}
        self.assertEqual(call('simulate_manager_score',m,2,h,{})[2],7)
    def test_completed_gw_no_future_and_draw_band(self):
        rows=call('build_player_analytics',bootstrap(),[fixture(finished=True)],start_gw=2)
        pa=[{'picks':[{'element':10,'position':1,'count':1}]}]
        outcome=call('h2h_live_win_probability',pa,[],{}, {10:5},{10:rows[0]},gw=2)
        self.assertEqual(outcome[:3],(0,100,0));self.assertEqual(outcome[3],[])
    def test_completed_margin_win(self):
        pa=[{'picks':[{'element':10,'position':1,'count':1}]}]
        result=call('h2h_live_win_probability',pa,[],{}, {10:6},{},gw=2)
        self.assertEqual(result[:3],(100,0,0))
    def test_shared_exposure_cancels(self):
        rows=call('build_player_analytics',bootstrap(),[fixture()],start_gw=2)
        team=[{'picks':[{'element':10,'position':1,'count':2}]}]
        result=call('h2h_live_win_probability',team,team,{}, {},{10:rows[0]},gw=2)
        self.assertEqual(result[:3],(0,100,0));self.assertEqual(result[3],[])
    def test_live_clock_scales_not_points_subtraction(self):
        row=call('build_player_analytics',bootstrap(),[fixture(started=True,minutes=45)],start_gw=2)[0]
        team=[{'picks':[{'element':10,'position':1,'count':1}]}]
        result=call('h2h_live_win_probability',team,[],{}, {10:20},{10:row},gw=2)
        self.assertAlmostEqual(result[3][0][3],row['fixtures_next'][0]['projection']/2)
    def test_hits_and_iml_multiplier(self):
        team=[{'picks':[],'transfer_hit':-4,'team_multiplier':2}]
        result=call('h2h_live_win_probability',team,[],{}, {},{},gw=2)
        self.assertEqual(result[:3],(0,0,100))
    def test_partial_ownership_denominator(self):
        ns['TEAMS']={'A':{},'B':{}}
        registry=[{'manager_id':'1','team':'A'},{'manager_id':'2','team':'B'}]
        picks={'1':{'picks':[{'element':10,'position':1}]}}
        analytics=[{'id':10,'projected_ppg':5,'projected_next':5}]
        rows,_=call('league_ownership_table',registry,picks,analytics)
        self.assertEqual(rows[0]['ownership_pct'],100)
    def test_batch_partial_reports_and_no_fake_record(self):
        def loader(key):
            if key==2:raise OSError('failure')
            return {'id':key}
        result=call('_batch_load',[1,2,1],loader)
        self.assertEqual(result,{1:{'id':1}});self.assertTrue(warns)
    def test_disk_reuse_and_failure_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,IML_CACHE_PATH=str(Path(tmp)/'data.sqlite3')):
            response=io.StringIO('{"id":1}')
            with patch.object(urllib.request,'urlopen',return_value=response) as fetch:
                self.assertEqual(call('_api_json','test/',ttl=600),{'id':1})
                self.assertEqual(call('_api_json','test/',ttl=600),{'id':1})
                self.assertEqual(fetch.call_count,1)
            with patch.object(urllib.request,'urlopen',side_effect=OSError('offline')):
                with self.assertRaises(OSError):call('_api_json','failed/')
            self.assertIsNone(call('_snapshot_read','https://fpl.invalid/api/failed/',600))
    def test_existing_engine_team_pick_compatibility(self):
        spec = importlib.util.spec_from_file_location('iml_test_engine', ROOT / 'fpl_h2h_v14.py')
        real_engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real_engine)
        data={'7':{'picks':[{'element':10,'position':1,'is_captain':True,'multiplier':3},
                           {'element':11,'position':2,'is_vice_captain':True,'multiplier':1}],
                   'active_chip':'3xc','entry_history':{'event_transfers_cost':4}}}
        with patch.dict(ns,engine=real_engine,cached_all_current_picks=lambda gw,ids:data):
            picks=call('cached_team_picks',('7',),0,2,'A',{},[])
            self.assertEqual([p['count'] for p in picks[0]['picks']],[6,2])
            self.assertEqual(call('team_total',picks,0,{10:5,11:2}),26)
            result=call('h2h_live_win_probability',picks,[],{}, {10:5,11:2},{},gw=2)
            self.assertEqual(result[:3],(100,0,0))
    def test_batch_limit_and_parallel_execution(self):
        active = peak = 0
        lock = threading.Lock()
        def loader(key):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            time.sleep(0.01)
            with lock: active -= 1
            return key
        result = call('_batch_load', range(24), loader)
        self.assertEqual(len(result), 24)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 6)
    def test_same_seed_reproducible_probabilities(self):
        row=call('build_player_analytics',bootstrap(),[fixture()],start_gw=2)[0]
        team=[{'picks':[{'element':10,'position':1,'count':2}]}]
        a=call('h2h_live_win_probability',team,[],{}, {},{10:row},gw=2)
        b=call('h2h_live_win_probability',team,[],{}, {},{10:row},gw=2)
        self.assertEqual(a[:3], b[:3]); self.assertAlmostEqual(sum(a[:3]), 100)
    def test_lazy_panel_structure(self):
        source=APP.read_text()
        self.assertNotIn('with league_tabs[',source)
        self.assertNotIn('fixture_tabs = st.tabs',source)
        self.assertIn('if active_panel == panel_names[2]:',source)

if __name__=='__main__':unittest.main()
