# IML Scorecards v16

This is a performance and reliability update of the attached v15 app. It retains the existing scorecard/report engine and analytics features, with lazy views, reusable endpoint snapshots, six-worker API batches, safer failure handling and optional external CSV projections.

## Install and run

For an existing deployment, replace its `streamlit_app.py` with `streamlit_app_v16.py` (rename it to `streamlit_app.py`). Keep `fpl_h2h_v14.py` alongside it. The ZIP includes the engine from your previously supplied file, unchanged, for convenience. If you have a newer engine, preserve it and verify compatibility before deployment.

```bash
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

Use a current Streamlit release that supports `width="stretch"` in dataframe, chart and download widgets. Existing league IDs and club mappings are retained; verify them against your current-season registry before relying on fixtures.

## Interface redesign

- **Matchups** opens by default. The score stays visible while switching between Overview, Gameweek race, Key players and Team details. Overview includes manager contributions; exports are inside an expander.
- **Players** groups the searchable player explorer, matchup outlook and template XI with low-owned alternatives. Search, position and club filters share the same player detail selector. Core projections appear first; detailed model metrics are optional.
- **Managers** separates rankings from individual manager reviews. Captain/bench decisions and gameweek history are expandable.
- The cumulative gameweek race is the only chart. Repeated bar/scatter charts were removed; historical points and minutes use tables. Squads, chips, CSV forecasts and report downloads remain available.
- Settings use expandable panels, with restrained spacing and theme-aware score cards.

## What changed

- Only the selected analytics panel, weekly matchup and scorecard view execute. Manager Lab loads one manager's squads rather than every manager's historical lab. PDF generation is opt-in.
- Independent roster, picks, player-summary and manager-history requests run in batches with up to six workers. Workers perform plain HTTP/data work, not Streamlit UI operations.
- API responses are stored per endpoint in SQLite, reused across reruns and overlapping player pools. Failed HTTP calls are not stored as empty squads or histories. Partial squad coverage is displayed, and missing squads are excluded from ownership denominators. Incomplete league registries raise rather than cache an incomplete roster.
- League name and roster share one endpoint response. The cached player map now hashes its bootstrap input. The gameweek defaults to the current/next event.
- Projection horizons count gameweeks, include all double-gameweek fixtures and assign zero to blanks. Bootstrap xG/xA remain available for players without detailed histories. Availability flags scale the fallback forecast. The fixture ticker avoids applying difficulty twice.
- Manager Lab aggregates every fixture within a GW and uses the FPL API's effective multipliers for captaincy, bench boost and completed autosubs. Captain regret counts the extra captain multiplier correctly. It fetches histories for players in the manager's past squads as well as transfer players.
- Live outcome estimates use NumPy, share each player's outcome between the two teams, stop simulating completed fixtures, include transfer hits and IML captain-manager multipliers, and apply the ±5-point draw band. Remaining expectation is scaled by fixture clock rather than subtracting points already earned.
- Changing captain/settings invalidates the previously displayed scorecard. Scorecards show their fetch time; click Analyse again to refresh. Data diagnostics show cumulative endpoint requests, disk hits, failures and worker time.

## External projection CSV

Upload in the sidebar. Required columns are `player_id,gw,xpts`; optional `xmins` is validated but not consumed by the current model. Use **official FPL element IDs for this season**, not provider IDs or names. Each row must contain the **whole GW total**, including all fixtures in a double gameweek. Include explicit zero rows for blanks if your provider supplies them.

```csv
player_id,gw,xpts,xmins
123,8,6.4,84
123,9,10.2,168
456,8,4.1,75
```

The importer rejects duplicate player/GW pairs, unknown IDs, fractional IDs/gameweeks, missing/non-finite values and implausible ranges. Missing rows fall back to the existing heuristic; affected players are labelled `CSV + fallback`. Removing the upload restores the fallback model. CSV projections override horizon totals and the fixture ticker; within a double GW they are divided equally between fixtures for the rough live estimate.

This is a provider-neutral importer, not a direct FFHub/FPL Review connection. Convert a provider's authorised export into the format above. No scraping, API key, subscription or LLM is required by this app. The example IDs are illustrative and must be replaced with real current-season IDs. Calibration and freshness of uploaded forecasts depend on the source.

## Cache behaviour

The default cache is `.iml_cache/fpl.sqlite3` beside the running application. Set `IML_CACHE_PATH` to choose another location. Roster/history/transfers: 15 minutes; picks: 3 minutes; player histories: 6 hours; fixtures: 1 minute; bootstrap: 1 hour. Existing engine live-score/fixture-kickoff wrappers remain in use.

Local SQLite snapshots are a cache, not a permanent competition database or captaincy-submission store. Hosted filesystems can be ephemeral. The app still fetches on cache misses; this release does not implement a scheduled background collector or precompute projection snapshots. Delete the cache between seasons; endpoint IDs can be reused. Read-only cache paths fall back to network access. Requests have a 12-second timeout; failed records can be retried by revisiting the view.

## Validation and limits

Run the included offline checks:

```bash
python -m unittest discover -s . -p 'test_v16.py' -v
```

24 checks passed, including actual engine compatibility for triple captain plus IML captain-manager doubling, bounded concurrent loading, cache reuse/failure handling, CSV validation, double/blank GWs, captain regret, completed fixtures, shared exposure and deterministic forecasts. Python compilation also passed.

The tests load application functions through Python's AST to avoid launching Streamlit; they do not constitute a full browser/UI or live-service test. Streamlit and Plotly were unavailable in the editing environment, and production FPL HTTP access was not verified. No measured production speedup is claimed. Verify the updated app in a staging deployment with real league rosters before replacing your live deployment.

The fallback remains an uncalibrated weighted heuristic, not a trained component model. Live forecasts approximate points remaining from fixture clocks and positive gamma samples; they do not predict individual playing time, future autosubs, negative events, conditional clean-sheet survival or bonus revisions. Outcome percentages are explicitly provisional. Historical GW selection is not a leakage-free backtest, because bootstrap/player histories are current. Transfer/chip ROI remains descriptive and is not causal attribution.

## Separate scorecard application

Run the dedicated app alongside the existing analytics app:

```bash
streamlit run scorecard_app.py --server.port 8502 --theme.base dark --theme.primaryColor "#1c9f6d" --theme.backgroundColor "#090e16" --theme.secondaryBackgroundColor "#101722" --theme.textColor "#eaf0f8"
```

**TOTW race** shows every loaded club's gameweek total and fixture cards. **Match scorecard** shows the match score, four manager contributions per club, the gameweek race, and optional player swings. Select submitted IML captain managers in the sidebar; unselected clubs are explicitly provisional and display their base total. Choices save automatically to browser localStorage and reload on refresh and later visits in the same browser. They are personal browser preferences, scoped by season, gameweek and demo/live mode. Each edit updates only that club; opening a view never overwrites saved selections.

The overall sheet is sorted by gameweek points, not season H2H standings. Failed club loads are excluded and reported, rather than replaced with zero. Scores use the existing engine, including effective FPL captaincy, chips and transfer hits; an IML captain doubles the complete net contribution. Data snapshots refresh after 60 seconds on the next rerun, or with Refresh scores. There is no unattended polling.

Enable **Design preview** to view clearly labelled fictional scores without contacting FPL. `scorecard_preview.html` is an offline visual sample using the same HTML/CSS. Run `python -X utf8 -m unittest discover -s . -p "test_*.py" -v` for the original regressions and separate scorecard checks. Live HTTP and full Streamlit browser rendering still require validation in an environment with the app dependencies installed.

The separate scorecard uses a dark matchday broadcast theme. Match pages include the signed lead chart with shaded team advantage and major-player stars above the cumulative points race. Play/pause and event scrubbing replay a maximum of about 80 frames; chart data retains every scoring event. Commentary is calculated from actual net player exposure: largest swing, lead changes, maximum gaps and recoveries. Engine chronology sorts by fixture kickoff and stat priority; it is not an exact timestamp replay. Race commentary excludes transfer hits. Demo commentary and event paths are explicitly fictional. Decorative animation respects reduced-motion settings; replay runs only when requested.

### Captain preferences in your browser

A small built-in Streamlit component stores captain choices in `localStorage` under `iml:captains:v1:<season>:<mode>:<gw>`. No database, account, new Python package or frontend build step is required. Include the `captain_picker` folder when deploying. Returning to the same site in the same browser restores choices, including across server restarts and redeployments with the same site origin. Other browsers/devices have their own preferences. Clearing site data resets choices; private browsing or blocked browser storage may not retain them.

The scorecard waits for browser preferences to load, avoiding briefly displaying incorrect base scores. Invalid roster IDs are not applied; the picker asks for a replacement while preserving the stored value until explicitly edited. Storage errors are visible and fall back to choices for the current visit. Switching gameweeks or refreshing scores does not erase saved choices. Run `node captain_picker/test_storage.cjs` to check the JavaScript storage behavior.

Commentary uses over 60 situational headlines plus light banter. Tight leads, wider gaps, tied scores, net player swings, comebacks and repeated lead changes have separate vocabulary. Stable hashing keeps headlines consistent for unchanged match data. All factual claims continue to come from the scoring timeline; no LLM or external commentary service is used.

### Club crests

The separate scorecard uses bundled club crest SVGs in `assets/crests`, sourced from the Premier League's club-page image CDN. The manifest records each club's asset and source URL. Deploy this folder with the app. Crests appear in the league sheet, fixture cards and match scoreboard, embedded directly in the rendered HTML. No runtime network request is needed for them. Club names remain readable if a custom club has no mapped crest.

### TOTW and POTW races

The league scorecard is now **TOTW race** (Team of the Week): each club's gameweek total, including the selected IML captain multiplier. **POTW race** (Player of the Week) compares individual IML members by gameweek score after transfer hits. It includes normal FPL captaincy and chip effects, but excludes the IML team captain multiplier. Equal scores share competition ranks (1, 1, 3). The race shows leader spotlights, a full member table, name/club filters and its own CSV export. Filters preserve overall race ranks. POTW does not need captain preferences to load. Members of clubs with incomplete data are excluded, with the same coverage warning as TOTW.

### Scorecard loading improvements

- The selected match fetches only its two clubs (8 member squads), while TOTW/POTW fetch the full league. The sidebar captain picker merges stored preferences for all clubs, so viewing a subset does not erase other choices.
- Endpoint snapshots are shared in memory across views and browser sessions on the same app process. Bootstrap and four-manager rosters are reused for 1 hour; member picks for 5 minutes; live points for 30 seconds; fixtures for 60 seconds. The rendered score snapshot remains cached for 60 seconds, so the view updates on rerun or Refresh scores, not through background polling.
- Refresh scores invalidates live points and fixtures, retaining squads/rosters. Data options → Reload squads and rosters performs a complete reload if those inputs need immediate updating. Picks, autosubs and chips can therefore lag by up to 5 minutes unless explicitly reloaded. Errors and incomplete responses are not cached as valid data.
- Independent bootstrap/live/fixture reads run together. Simultaneous requests for the same endpoint share one in-flight HTTP request. Cache entries are bounded (512 endpoints, 32 score snapshots and 16 race figures). No persistent database or extra service is added.
- Race kickoff metadata comes from the fixtures already loaded, removing another HTTP request. The complete static race is shown first. Enable gameweek replay loads the animation frames on demand; figure generation is cached independently of captain-picker reruns.

Offline request-count checks show 13 requests for a fresh single-match snapshot versus 103 for the full league. A full-league score refresh makes only 2 new requests while the roster/squad caches are valid. These are deterministic mocked-API checks, not measured production latency or an assurance of a specific page-load time. A cold TOTW/POTW load still needs the full league's squads and depends on FPL response times. Cached data is lost when the app process restarts.
