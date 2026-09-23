# Awesome ACPs

A monorepo of **ACP agents** — the ones the registry does not have yet.

Every agent on the official ACP list is a coding agent or a coding-agent harness. These
are the other kind: agents that answer a real-world question inside Zed, JetBrains or any
ACP client, straight from a public dataset, with no API key and nothing invented.

- **25 agents** in [`agents/`](./agents)
- **661 unit tests**, all offline, all passing
- **24 live probes** over real ACP stdio against real public data (see [Proof](#proof))

## The agents

| Agent | What you can ask it | Dataset (keyless) |
|---|---|---|
| [`air`](./agents/air) | "how is the air quality in Delhi right now?", "when does it get worse today?" | Open-Meteo air quality (CAMS) |
| [`art`](./agents/art) | "paintings by Monet", "surprise me with a piece" | Art Institute of Chicago, Cleveland, The Met |
| [`aurora`](./agents/aurora) | "how are the geomagnetic conditions?", "odds of seeing the aurora in Tromso?" | NOAA SWPC (Kp, OVATION, alerts) |
| [`bikes`](./agents/bikes) | "how many citibikes are available right now?", "nearest station to 40.71,-74.01?" | Citi Bike GBFS (NYC) |
| [`books`](./agents/books) | "find books about urban foxes", "what has Ursula K. Le Guin written?" | Open Library |
| [`buoys`](./agents/buoys) | "what are the conditions at buoy 41025?", "which buoys are near Hawaii?" | NOAA NDBC realtime |
| [`civic`](./agents/civic) | "did complaint 70483808 get fixed?", "complaints near 11235" | NYC Open Data |
| [`floodwatch`](./agents/floodwatch) | "is the Mississippi at St. Louis flooding?", "what is gauge EADM7 doing?" | NWS NWPS gauge + USGS site search |
| [`forecast`](./agents/forecast) | "when will it rain in Seattle today?", "what does the week look like?" | Open-Meteo forecast |
| [`fx`](./agents/fx) | "what is 100 USD in EUR?", "how did USD-EUR move this month?" | ECB reference rates (Frankfurter) |
| [`hazards`](./agents/hazards) | "any weather alerts in NY?", "earthquakes above M4.5 today?" | NWS alerts + USGS quakes |
| [`iss`](./agents/iss) | "where is the ISS right now?", "could I see it from Denver tonight?" | open-notify position + sunset + cloud cover |
| [`labels`](./agents/labels) | "what is lipitor?", "warnings for metformin" | openFDA drug labels |
| [`launches`](./agents/launches) | "what is the next launch?", "when is the next Starlink launch?" | Launch Library 2 |
| [`ledger`](./agents/ledger) | "what is the national debt right now?", "how much interest is paid?" | US Treasury Fiscal Data |
| [`local`](./agents/local) | "how much disk space is left?", "what is going on in this repo?", "what is on port 3000?" | this machine, read-only (`git`, `lsof`) |
| [`nature`](./agents/nature) | "how many monarch butterflies are recorded in Canada?", "what is seen near 40.71,-74.01?" | GBIF occurrence records |
| [`rivers`](./agents/rivers) | "what is gauge 06730500 doing?", "which gauges are near 40.71,-74.01?" | USGS water data (OGC API) |
| [`species`](./agents/species) | "how many monarch butterflies are there?", "what has been seen near 40.71,-74.01?" | iNaturalist |
| [`surf`](./agents/surf) | "how are the waves at Pipeline?", "when is it worth surfing at Nazare?" | Open-Meteo marine + wind |
| [`vehicles`](./agents/vehicles) | "recalls for the 2015 Honda Civic", "complaints about a VIN" | NHTSA |
| [`watch`](./agents/watch) | "watch the earthquakes", "watch the aurora for 4 rounds every 30s" (pushes until cancelled) | USGS quakes + NOAA Kp + open-notify |
| [`wiki`](./agents/wiki) | "who was Ada Lovelace?", "what happened on this day?" | Wikipedia + Wikidata |
| [`wildfire`](./agents/wildfire) | "what is burning in California?", "any fires within 150 miles of Denver?" | NIFC WFIGS incidents |
| [`a2a_bridge`](./agents/a2a_bridge) | "nyc311: did complaint 70483808 get fixed?" | any A2A server you point it at |

Plus [`acp_kit/`](./acp_kit) — the protocol layer all of them share (NDJSON JSON-RPC over
stdio, sessions, plans, tool calls, permissions, streaming, cancellation).

What is still missing, what was checked and skipped, and the verified backlog live in
[`MISSING.md`](./MISSING.md).

## Run one

```bash
# talk to an agent the way an editor does (spawn, initialize, prompt, print updates)
python3 tools/probe.py --agent "python3 agents/forecast/agent.py" \
    --prompt "when will it rain in Seattle today?"

# every agent, one live question each
python3 tools/probe_all.py

# all 661 unit tests (offline, no network)
bash scripts/test-all.sh
```

Wire one into Zed or JetBrains — any ACP client takes a command:

```json
{
  "agent_servers": {
    "Forecast": { "command": "python3", "args": ["/abs/path/agents/forecast/agent.py"] }
  }
}
```

## Proof

Live probe of all 25 agents over real ACP stdio on 2026-09-23 (every answer came from the
dataset, not a fixture). `a2a_bridge` is probed only when `ACP_A2A_ENDPOINTS` points at a
running A2A server, so that run shows 24:

```
[PASS] air        (0.92s) - how is the air quality in Delhi right now?             tool: air-now
[PASS] art        (0.79s) - paintings by Monet                                    tool: art-search
[PASS] aurora     (0.90s) - how are the geomagnetic conditions right now?         tool: aurora-now
[PASS] bikes      (1.14s) - how many citibikes are available right now?           tool: bikes-status
[PASS] books      (6.77s) - find books about urban foxes                          tool: books-search
[PASS] buoys      (1.31s) - what are the conditions at buoy 41025?                tool: buoy-conditions
[PASS] civic      (1.44s) - did complaint 70483808 get fixed?                     tool: complaint-status
[PASS] floodwatch (1.60s) - what is the Mississippi River at St. Louis doing?      tool: flood-status
[PASS] forecast   (0.90s) - when will it rain in Seattle today?                   tool: weather-rain
[PASS] fx         (0.79s) - what is 100 USD in EUR?                               tool: fx-rate
[PASS] hazards    (0.89s) - any weather alerts in NY right now?                   tool: alerts-active
[PASS] iss        (3.48s) - could I see the ISS from Denver tonight?              tool: iss-sky
[PASS] labels     (1.68s) - what is lipitor?                                      tool: label-info
[PASS] launches   (1.42s) - what is the next launch?                              tool: launches-upcoming
[PASS] ledger     (1.17s) - what is the national debt right now?                  tool: debt-outstanding
[PASS] local      (0.40s) - what is going on in this repo?                        tool: git
[PASS] nature     (3.74s) - how many monarch butterflies are recorded in Canada?  tool: nature-count
[PASS] rivers     (0.96s) - what is gauge 06730500 doing right now?               tool: river-stage
[PASS] species    (1.05s) - how many observations of Danaus plexippus are there?  tool: species-count
[PASS] surf       (1.56s) - how are the waves at Pipeline right now?              tool: surf-now
[PASS] vehicles   (0.75s) - recalls for the 2015 Honda Civic                      tool: vehicle-recalls
[PASS] watch      (6.74s) - watch the aurora for 3 rounds every 3 seconds         tool: watch-aurora
[PASS] wiki       (0.64s) - who was Ada Lovelace?                                 tool: wiki-summary
[PASS] wildfire   (0.87s) - what wildfires are burning in California right now?   tool: wildfire-active
== 24/24 agent(s) answered a live question  (a2a_bridge skipped: no A2A server running)
```

A few answers verbatim from that run:

```
GBIF holds 817,344 occurrence records for Danaus plexippus (Linnaeus, 1758)  • In Canada: 87,502 (10.7%)
Diamond Shoals, NC (41025, buoy), report at 2026-09-23T00:40Z:
  Waves: 1.6 m significant height • Wind: 8 m/s from NNE (20 degrees), gusting 10 m/s • Water 28.7 degC
EADM7 - Mississippi River at St. Louis (MO, St. Louis City), forecast by NCRFC:
  Observed: 13.21 ft, flow 236 kcfs - no flooding • Flood stages here: action 28 ft, minor 30 ft
Pipeline (21.66,-158.05) at 2026-09-22T15:15 (Pacific/Honolulu):
  Waves: 1.7 m significant height (fun, waist to chest high), 8.1 s period from NE - windswell
  Read: onshore - wind is coming out of the same quarter as the swell
Repository .../awesome-acps on main, tracking origin/main, 0 ahead and 0 behind
  Working tree: 0 staged, 1 modified, 6 untracked • Last commit: 57df985 by mrfentmen
Watching the one-minute planetary K index, 3 rounds 3 s apart:
  Round 1/3 [2026-09-23T01:10:00Z] Kp 0.00 - below storm level - first reading
  Round 2/3 [2026-09-23T01:10:00Z] Kp 0.00 - below storm level - no change since the last round
Watching the space station, 3 rounds 3 s apart:
  Round 2/3 station at 51.39 N, 123.31 W - moved 21 km in 3s (6.9 km/s)
```

## What every agent guarantees

- **Real data, no mocks.** Each agent reads a live public feed. If the feed fails, the
  answer says which feed failed — it never falls back to an invented number.
- **Deterministic routing.** Intent → skill is a pure `route(text)` function, unit-tested.
  No model decides what to do, so behaviour does not drift.
- **Permission first.** The first read asks the client for permission
  (`session/request_permission`), and the answer stays refused if you refuse.
- **Streamed answers.** One message id, many chunks, plan first, tool call open → completed
  with a one-line summary of the record that was read. [`watch`](./agents/watch) goes further:
  one message per round, pushed on an interval, and `session/cancel` stops it immediately.
- **Provenance.** Every answer names the dataset id and its units, and states the limits
  (a model run, a reference rate, one gauge at one time, catalog counts not sales).
- **No keys.** All 25 use keyless public APIs. Nothing to sign up for, nothing to leak.
  ([`local`](./agents/local) uses no API at all — it reads this machine.)

## Layout

```
acp_kit/            protocol layer: rpc.py (NDJSON JSON-RPC), agent.py, client.py
agents/<name>/      data.py (feed reader + validation)  agent.py (routing + skills + ACP)
agents/<name>/tests/test_agent.py
tests/test_kit.py   protocol tests
tools/probe.py      one agent, one prompt, real stdio
tools/probe_all.py  every agent, one live question each
tools/check_syntax.py  fails on Python 3.12-only syntax, so the 3.10 CI job stays honest
scripts/            test-all.sh, probe-all.sh
.github/workflows/  matrix CI: the offline suite on Python 3.10 and 3.12
MISSING.md          the research log: what exists, what was missing, the verified backlog
```

## Write another one

Two files and a test file:

1. `data.py` — a class with a `_get(url, params, ttl)` transport, `check_*` validators,
   and one method per read. Raise one exception type on any upstream problem.
2. `agent.py` — a pure `route(text) -> (skill, params)`, an `AcpAgent` subclass with one
   `_skill()` per intent, and a `HELP` string. Report a plan, ask permission once,
   stream the answer, finish the tool call with the dataset id.
3. `tests/test_agent.py` — reader tests with an injected `fetch`, routing tests, and turn
   tests over a real `AcpClient` + `Connection` pair with canned payloads.

`python3 tools/probe_all.py <name>` then proves it against the live feed.

## Environment

Nothing is required: every agent runs with no key and no config. These optional knobs exist
for when you point one at a server of your own, or want a different cache window:

| Variable | Used by | Effect |
|---|---|---|
| `ACP_LOG_LEVEL` | every agent | Python log level for the agent's stderr (`INFO` by default) |
| `<AGENT>_USER_AGENT` | every agent | the `User-Agent` it sends (e.g. `HAZARDS_USER_AGENT`, `WATCH_AGENT_USER_AGENT`). Set a contactable one if you run these anywhere public |
| `<AGENT>_HTTP_TIMEOUT` | every agent | seconds to wait on a feed, default 20-25 |
| `<AGENT>_CACHE_TTL` | every agent | seconds a read is reused, default 60-900 depending on the feed's own limits |
| `WATCH_AGENT_ROUNDS`, `WATCH_AGENT_INTERVAL` | [`watch`](./agents/watch) | the default rounds and gap between pushed updates (3 and 15s) |
| `LOCAL_AGENT_TIMEOUT` | [`local`](./agents/local) | seconds before a `git` or `lsof` read is given up on (10) |
| `ACP_A2A_ENDPOINTS` | [`a2a_bridge`](./agents/a2a_bridge) | `name=url,...` of A2A servers to reach |
| `ACP_BRIDGE_PUSH_PORT` | [`a2a_bridge`](./agents/a2a_bridge) | local port the A2A push webhook listens on |

## Honest limits

- Read-only. No agent here writes anything anywhere. [`local`](./agents/local) can only run
  `git` (reporting) and `lsof` (listing), never a write, a commit or a kill.
- Feeds have cache TTLs (1–60 minutes) so repeated questions do not hammer public APIs;
  Launch Library's free host allows 15 requests/hour, so that one caches for 15 minutes,
  and Citi Bike's status file (2,520 stations) caches for one minute.
- `iss` calls open-notify over plain HTTP because its HTTPS endpoint timed out on
  2026-09-22 while HTTP answered in 0.2 s; it is a public position and nothing private.
- `bikes` speaks only for Citi Bike (New York City, Jersey City, Hoboken); `wiki` quotes
  CC BY-SA text; `labels` is not medical advice.
- Fast-moving data is labelled as such in the answer: launch windows move, model output is
  not a measurement, ECB rates skip weekends and holidays, and a reference rate is not a
  tradeable rate.
- [`watch`](./agents/watch) polls, because USGS, NOAA SWPC and open-notify publish no push
  channel to subscribe to. It is capped at 20 rounds and 180 seconds, it sleeps on the
  session's own cancel event, and nothing keeps running after the turn.
- [`floodwatch`](./agents/floodwatch) reads NWS NWPS gauge statistics (observed, forecast and
  the published flood stages). NWPS has no name search that is quick enough to use per turn
  (its gauge list is ~13 MB), so a river name is resolved through USGS first, and the answer
  says which gauge it landed on. A name that matches no gauge is reported, not guessed at.
- [`nature`](./agents/nature) resolves a common name through iNaturalist and then reads
  counts from GBIF, because GBIF's own search cannot map common names. Broad ranks are only
  accepted when records actually exist for them; otherwise the agent says it could not pin
  the name down.
- [`surf`](./agents/surf) infers offshore/onshore by comparing wind with wave direction; it
  does not know how any particular beach faces.
- [`buoys`](./agents/buoys) reports "not reported" when a sensor sent nothing — that is not a
  zero.
- An agent that does not know a place says so by name instead of guessing coordinates.

## License

MIT — see [LICENSE](./LICENSE).
