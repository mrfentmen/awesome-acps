# Awesome ACPs

A monorepo of **ACP agents** — the ones the registry does not have yet.

Every agent on the official ACP list is a coding agent or a coding-agent harness. These
are the other kind: agents that answer a real-world question inside Zed, JetBrains or any
ACP client, straight from a public dataset, with no API key and nothing invented.

- **19 agents** in [`agents/`](./agents)
- **455 unit tests**, all offline, all passing
- **18 live probes** over real ACP stdio against real public data (see [Proof](#proof))

## The agents

| Agent | What you can ask it | Dataset (keyless) |
|---|---|---|
| [`air`](./agents/air) | "how is the air quality in Delhi right now?", "when does it get worse today?" | Open-Meteo air quality (CAMS) |
| [`art`](./agents/art) | "paintings by Monet", "surprise me with a piece" | Art Institute of Chicago, Cleveland, The Met |
| [`aurora`](./agents/aurora) | "how are the geomagnetic conditions?", "odds of seeing the aurora in Tromso?" | NOAA SWPC (Kp, OVATION, alerts) |
| [`bikes`](./agents/bikes) | "how many citibikes are available right now?", "nearest station to 40.71,-74.01?" | Citi Bike GBFS (NYC) |
| [`books`](./agents/books) | "find books about urban foxes", "what has Ursula K. Le Guin written?" | Open Library |
| [`civic`](./agents/civic) | "did complaint 70483808 get fixed?", "complaints near 11235" | NYC Open Data |
| [`forecast`](./agents/forecast) | "when will it rain in Seattle today?", "what does the week look like?" | Open-Meteo forecast |
| [`fx`](./agents/fx) | "what is 100 USD in EUR?", "how did USD-EUR move this month?" | ECB reference rates (Frankfurter) |
| [`hazards`](./agents/hazards) | "any weather alerts in NY?", "earthquakes above M4.5 today?" | NWS alerts + USGS quakes |
| [`iss`](./agents/iss) | "where is the ISS right now?", "could I see it from Denver tonight?" | open-notify position + sunset + cloud cover |
| [`labels`](./agents/labels) | "what is lipitor?", "warnings for metformin" | openFDA drug labels |
| [`launches`](./agents/launches) | "what is the next launch?", "when is the next Starlink launch?" | Launch Library 2 |
| [`ledger`](./agents/ledger) | "what is the national debt right now?", "how much interest is paid?" | US Treasury Fiscal Data |
| [`rivers`](./agents/rivers) | "what is gauge 06730500 doing?", "which gauges are near 40.71,-74.01?" | USGS water data (OGC API) |
| [`species`](./agents/species) | "how many monarch butterflies are there?", "what has been seen near 40.71,-74.01?" | iNaturalist |
| [`vehicles`](./agents/vehicles) | "recalls for the 2015 Honda Civic", "complaints about a VIN" | NHTSA |
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

# all 455 unit tests (offline, no network)
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

Live probe of all 19 agents over real ACP stdio on 2026-09-22 (every answer came from the
dataset, not a fixture). `a2a_bridge` is probed only when `ACP_A2A_ENDPOINTS` points at a
running A2A server, so that run shows 18:

```
[PASS] air      (0.91s) - how is the air quality in Delhi right now?                tool: air-now
[PASS] art      (0.46s) - paintings by Monet                                       tool: art-search
[PASS] aurora   (0.60s) - how are the geomagnetic conditions right now?            tool: aurora-now
[PASS] bikes    (0.74s) - how many citibikes are available right now?              tool: bikes-status
[PASS] books    (0.65s) - find books about urban foxes                             tool: books-search
[PASS] civic    (0.76s) - did complaint 70483808 get fixed?                        tool: complaint-status
[PASS] forecast (0.77s) - when will it rain in Seattle today?                      tool: weather-rain
[PASS] fx       (0.47s) - what is 100 USD in EUR?                                  tool: fx-rate
[PASS] hazards  (0.41s) - any weather alerts in NY right now?                      tool: alerts-active
[PASS] iss      (0.93s) - could I see the ISS from Denver tonight?                 tool: iss-sky
[PASS] labels   (2.21s) - what is lipitor?                                         tool: label-info
[PASS] launches (0.99s) - what is the next launch?                                 tool: launches-upcoming
[PASS] ledger   (1.04s) - what is the national debt right now?                     tool: debt-outstanding
[PASS] rivers   (0.91s) - what is gauge 06730500 doing right now?                  tool: river-stage
[PASS] species  (0.69s) - how many observations of Danaus plexippus are there?     tool: species-count
[PASS] vehicles (0.47s) - recalls for the 2015 Honda Civic                         tool: vehicle-recalls
[PASS] wiki     (0.45s) - who was Ada Lovelace?                                    tool: wiki-summary
[PASS] wildfire (0.61s) - what wildfires are burning in California right now?      tool: wildfire-active
== 18/18 agent(s) answered a live question  (a2a_bridge skipped: no A2A server running)
```

A few answers verbatim from that run:

```
Ada Lovelace - English mathematician (1815-1852)   • Wikidata entity: Q7259
Works matching 'Monet' (Art Institute of Chicago): Water Lilies - Claude Monet (French, 1840-1926) - 1906
Citi Bike (New York City, Jersey City, Hoboken) right now: 33,617 bikes across 2,520 stations
Sky check for 39.74,-104.99: the station is 776 km away, in your part of the sky, but not overhead;
  sunset 2026-09-23T00:58:33Z, cloud cover now 100%
iNaturalist holds 548,334 observations of danaus plexippus, newest 2026-09-22 in Wichita, KS
Lipitor (ATORVASTATIN CALCIUM) - Viatris Specialty LLC • label effective 2024-04-15, 5 sections on file
```

## What every agent guarantees

- **Real data, no mocks.** Each agent reads a live public feed. If the feed fails, the
  answer says which feed failed — it never falls back to an invented number.
- **Deterministic routing.** Intent → skill is a pure `route(text)` function, unit-tested.
  No model decides what to do, so behaviour does not drift.
- **Permission first.** The first read asks the client for permission
  (`session/request_permission`), and the answer stays refused if you refuse.
- **Streamed answers.** One message id, many chunks, plan first, tool call open → completed
  with a one-line summary of the record that was read.
- **Provenance.** Every answer names the dataset id and its units, and states the limits
  (a model run, a reference rate, one gauge at one time, catalog counts not sales).
- **No keys.** All 19 use keyless public APIs. Nothing to sign up for, nothing to leak.

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

## Honest limits

- Read-only. No agent here writes anything anywhere.
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
- An agent that does not know a place says so by name instead of guessing coordinates.

## License

MIT — see [LICENSE](./LICENSE).
