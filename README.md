# Awesome ACPs

A monorepo of **ACP agents** — the ones the registry does not have yet.

Every agent on the official ACP list is a coding agent or a coding-agent harness. These
are the other kind: agents that answer a real-world question inside Zed, JetBrains or any
ACP client, straight from a public dataset, with no API key and nothing invented.

- **13 agents** in [`agents/`](./agents)
- **340 unit tests**, all offline, all passing
- **13 live probes** over real ACP stdio against real public data (see [Proof](#proof))

## The agents

| Agent | What you can ask it | Dataset (keyless) |
|---|---|---|
| [`air`](./agents/air) | "how is the air quality in Delhi right now?", "when does it get worse today?" | Open-Meteo air quality (CAMS) |
| [`aurora`](./agents/aurora) | "how are the geomagnetic conditions?", "odds of seeing the aurora in Tromso?" | NOAA SWPC (Kp, OVATION, alerts) |
| [`books`](./agents/books) | "find books about urban foxes", "what has Ursula K. Le Guin written?" | Open Library |
| [`civic`](./agents/civic) | "did complaint 70483808 get fixed?", "complaints near 11235" | NYC Open Data |
| [`forecast`](./agents/forecast) | "when will it rain in Seattle today?", "what does the week look like?" | Open-Meteo forecast |
| [`fx`](./agents/fx) | "what is 100 USD in EUR?", "how did USD-EUR move this month?" | ECB reference rates (Frankfurter) |
| [`hazards`](./agents/hazards) | "any weather alerts in NY?", "earthquakes above M4.5 today?" | NWS alerts + USGS quakes |
| [`launches`](./agents/launches) | "what is the next launch?", "when is the next Starlink launch?" | Launch Library 2 |
| [`ledger`](./agents/ledger) | "what is the national debt right now?", "how much interest is paid?" | US Treasury Fiscal Data |
| [`rivers`](./agents/rivers) | "what is gauge 06730500 doing?", "which gauges are near 40.71,-74.01?" | USGS water data (OGC API) |
| [`vehicles`](./agents/vehicles) | "recalls for the 2015 Honda Civic", "complaints about a VIN" | NHTSA |
| [`wildfire`](./agents/wildfire) | "what is burning in California?", "any fires within 150 miles of Denver?" | NIFC WFIGS incidents |
| [`a2a_bridge`](./agents/a2a_bridge) | "nyc311: did complaint 70483808 get fixed?" | any A2A server you point it at |

Plus [`acp_kit/`](./acp_kit) — the protocol layer all of them share (NDJSON JSON-RPC over
stdio, sessions, plans, tool calls, permissions, streaming, cancellation).

## Run one

```bash
# talk to an agent the way an editor does (spawn, initialize, prompt, print updates)
python3 tools/probe.py --agent "python3 agents/forecast/agent.py" \
    --prompt "when will it rain in Seattle today?"

# every agent, one live question each
python3 tools/probe_all.py

# all 340 unit tests (offline, no network)
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

Live probe of all 13 agents over real ACP stdio on 2026-09-22 (every answer came from the
dataset, not a fixture):

```
[PASS] air       (0.71s) - how is the air quality in Delhi right now?           tool: air-now
[PASS] aurora    (0.76s) - how are the geomagnetic conditions right now?        tool: aurora-now
[PASS] books     (0.76s) - find books about urban foxes                         tool: books-search
[PASS] civic     (0.87s) - did complaint 70483808 get fixed?                    tool: complaint-status
[PASS] forecast  (0.42s) - when will it rain in Seattle today?                  tool: weather-rain
[PASS] fx        (0.31s) - what is 100 USD in EUR?                              tool: fx-rate
[PASS] hazards   (0.37s) - any weather alerts in NY right now?                  tool: alerts-active
[PASS] launches  (0.34s) - what is the next launch?                             tool: launches-upcoming
[PASS] ledger    (1.40s) - what is the national debt right now?                 tool: debt-outstanding
[PASS] rivers    (0.74s) - what is gauge 06730500 doing right now?              tool: river-stage
[PASS] vehicles  (0.46s) - recalls for the 2015 Honda Civic                     tool: vehicle-recalls
[PASS] wildfire  (0.74s) - what wildfires are burning in California right now?  tool: wildfire-active
[PASS] a2a_bridge(0.30s) - did complaint 70483808 get fixed? (via A2A over HTTP) tool: nyc311:complaint-status
== 13/13 agents answered a live question
```

A few answers verbatim:

```
100 USD = 87.237 EUR            • ECB reference rate USD/EUR = 0.87237 on 2026-09-22
Latest USGS readings at USGS-06730500: discharge (streamflow) (00060): 0.22 ft^3/s at 2026-09-22T19:00:00+00:00
161 record(s) match 'urban foxes'   • Urban foxes - Harris, Stephen (1986), /works/OL4913260W
Next launch: Long March 8A | Unknown Payload - 2026-09-23T13:30:00Z [Go] - at Commercial LC-1, Wenchang
Complaint 70483808: Noise - Residential (Loud Music/Party) — status Closed. Filed 2026-09-20.
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
- **No keys.** All 13 use keyless public APIs. Nothing to sign up for, nothing to leak.

## Layout

```
acp_kit/            protocol layer: rpc.py (NDJSON JSON-RPC), agent.py, client.py
agents/<name>/      data.py (feed reader + validation)  agent.py (routing + skills + ACP)
agents/<name>/tests/test_agent.py
tests/test_kit.py   protocol tests
tools/probe.py      one agent, one prompt, real stdio
tools/probe_all.py  every agent, one live question each
scripts/            test-all.sh, probe-all.sh
MISSING.md          the research log: what exists, what was missing, what was built
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
- Feeds have cache TTLs (5–60 minutes) so repeated questions do not hammer public APIs;
  Launch Library's free host allows 15 requests/hour, so that one caches for 15 minutes.
- Fast-moving data is labelled as such in the answer: launch windows move, model output is
  not a measurement, ECB rates skip weekends and holidays, and a reference rate is not a
  tradeable rate.
- An agent that does not know a place says so by name instead of guessing coordinates.

## License

MIT — see [LICENSE](./LICENSE).
