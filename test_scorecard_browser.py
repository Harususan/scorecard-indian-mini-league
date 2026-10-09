"""Browser preference result validation without starting Streamlit."""
import ast
from pathlib import Path
import unittest
from scorecard_data import season_key

source=Path(__file__).with_name("scorecard_browser.py").read_text(encoding="utf-8")
node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=="validate_choices")
ns={}
exec(compile(ast.Module(body=[node],type_ignores=[]),"scorecard_browser.py","exec"),ns)
validate=ns["validate_choices"]

class BrowserPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.teams={"Arsenal":{"managers":[{"id":"101"}]},"Chelsea":{"managers":[{"id":"202"}]}}
    def test_waits_for_browser_before_displaying_scores(self):
        self.assertIsNone(validate(None,self.teams,"season:live:8"))
        self.assertIsNone(validate({"ready":True,"namespace":"season:live:9","choices":{}},self.teams,"season:live:8"))
    def test_restores_valid_and_rejects_stale_captains(self):
        result={"namespace":"season:live:8","ready":True,"choices":{"Arsenal":"101","Chelsea":"old-id"}}
        self.assertEqual(validate(result,self.teams,"season:live:8"),{"Arsenal":"101","Chelsea":None})
    def test_empty_preferences_are_valid(self):
        result={"namespace":"season:live:8","ready":True,"choices":{}}
        self.assertEqual(validate(result,self.teams,"season:live:8"),{"Arsenal":None,"Chelsea":None})
    def test_season_uses_first_deadline(self):
        self.assertEqual(season_key({"events":[{"deadline_time":"2027-01-03T12:00:00Z"},{"deadline_time":"2026-08-14T12:00:00Z"}]}),"2026-2027")

if __name__=="__main__":unittest.main()
