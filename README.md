# Awesome ACPs

A monorepo of **ACP agents** — the ones the registry does not have yet.

Every agent on the official ACP list is a coding agent or a coding-agent harness. These
are the other kind: agents that answer a real-world question inside Zed, JetBrains or any
ACP client, straight from a public dataset, with no API key and nothing invented.

- **45 agents** in [`agents/`](./agents)
- **1189 unit tests**, all offline, all passing
- **44 live probes** over real ACP stdio against real public data (see [Proof](#proof))

## The agents

| Agent | What you can ask it | Dataset (keyless) |
|---|---|---|
| [`air`](./agents/air) | "how is the air quality in Delhi right now?", "when does it get worse today?" | Open-Meteo air quality (CAMS) |
| [`alert`](./agents/alert) | "alert me when the air quality in Delhi passes 200", "tell me when gauge 07010000 reaches flood stage" (silent until it happens) | Open-Meteo + USGS + NWPS + Treasury |
| [`art`](./agents/art) | "paintings by Monet", "surprise me with a piece" | Art Institute of Chicago, Cleveland, The Met |
| [`aurora`](./agents/aurora) | "how are the geomagnetic conditions?", "odds of seeing the aurora in Tromso?" | NOAA SWPC (Kp, OVATION, alerts) |
| [`archive`](./agents/archive) | "find Apollo 11 recordings", "what is in the item Apollo11Audio?" | Internet Archive |
| [`bikes`](./agents/bikes) | "how many citibikes are available right now?", "nearest station to 40.71,-74.01?" | Citi Bike GBFS (NYC) |
| [`books`](./agents/books) | "find books about urban foxes", "what has Ursula K. Le Guin written?" | Open Library |
| [`brief`](./agents/brief) | "brief me on Denver" (asks, then writes `BRIEFING.md` into your workspace) | Open-Meteo + NWS + USGS + open-notify |
| [`buoys`](./agents/buoys) | "what are the conditions at buoy 41025?", "which buoys are near Hawaii?" | NOAA NDBC realtime |
| [`chart`](./agents/chart) | "chart the temperature in Seattle for the next 24 hours" (answers with a real PNG) | Open-Meteo + Treasury + USGS |
| [`chem`](./agents/chem) | "what is the formula for ibuprofen?", "what is Lipitor's generic name?" | PubChem + RxNorm |
| [`civic`](./agents/civic) | "did complaint 70483808 get fixed?", "complaints near 11235" | NYC Open Data |
| [`crypto`](./agents/crypto) | "what is the price of bitcoin?", "how much is locked in DeFi?" | CoinGecko + DefiLlama |
| [`domains`](./agents/domains) | "when does github.com expire?", "who is the registrar for example.org?" | RDAP (rdap.org) |
| [`drought`](./agents/drought) | "how dry is California?", "drought in Texas over the last 8 weeks" | US Drought Monitor |
| [`firehose`](./agents/firehose) | "watch the wiki firehose for 10 edits" (a push stream, not a poll) | Wikimedia EventStreams (SSE) |
| [`floodwatch`](./agents/floodwatch) | "is the Mississippi at St. Louis flooding?", "what is gauge EADM7 doing?" | NWS NWPS gauge + USGS site search |
| [`food`](./agents/food) | "how much sugar is in Nutella?", "is there milk in Oreos?" | Open Food Facts |
| [`forecast`](./agents/forecast) | "when will it rain in Seattle today?", "what does the week look like?" | Open-Meteo forecast |
| [`fx`](./agents/fx) | "what is 100 USD in EUR?", "how did USD-EUR move this month?" | ECB reference rates (Frankfurter) |
| [`groundwater`](./agents/groundwater) | "how deep is the water table in Kansas?", "water level at site 395847084085500" | USGS Water Data (OGC) |
| [`hazards`](./agents/hazards) | "any weather alerts in NY?", "earthquakes above M4.5 today?" | NWS alerts + USGS quakes |
| [`iss`](./agents/iss) | "where is the ISS right now?", "could I see it from Denver tonight?" | open-notify position + sunset + cloud cover |
| [`labels`](./agents/labels) | "what is lipitor?", "warnings for metformin" | openFDA drug labels |
| [`launches`](./agents/launches) | "what is the next launch?", "when is the next Starlink launch?" | Launch Library 2 |
| [`ledger`](./agents/ledger) | "what is the national debt right now?", "how much interest is paid?" | US Treasury Fiscal Data |
| [`local`](./agents/local) | "how much disk space is left?", "what is going on in this repo?", "what is on port 3000?" | this machine, read-only (`git`, `lsof`) |
| [`moon`](./agents/moon) | "when is the next full moon?", "when does it get dark in Denver?" | USNO + sunrise-sunset.org |
| [`nature`](./agents/nature) | "how many monarch butterflies are recorded in Canada?", "what is seen near 40.71,-74.01?" | GBIF occurrence records |
| [`osm`](./agents/osm) | "nearest drinking water to Bryant Park, New York", "pharmacies near Miami Beach" | OpenStreetMap (Nominatim + Overpass) |
| [`papers`](./agents/papers) | "papers about CRISPR sickle cell", "what is DOI 10.1038/nature12373?" | PubMed + arXiv + Crossref |
| [`pollen`](./agents/pollen) | "how bad is the pollen today in Denver?", "grass pollen this week in Berlin" | Open-Meteo air quality (pollen) |
| [`rivers`](./agents/rivers) | "what is gauge 06730500 doing?", "which gauges are near 40.71,-74.01?" | USGS water data (OGC API) |
| [`snowpack`](./agents/snowpack) | "how much water is in the California snowpack?", "snow depth in Colorado" | NRCS snow network (SNOTEL) |
| [`species`](./agents/species) | "how many monarch butterflies are there?", "what has been seen near 40.71,-74.01?" | iNaturalist |
| [`stocks`](./agents/stocks) | "what is AAPL doing right now?", "how did NVDA move this month?" | Yahoo Finance chart |
| [`surf`](./agents/surf) | "how are the waves at Pipeline?", "when is it worth surfing at Nazare?" | Open-Meteo marine + wind |
| [`tides`](./agents/tides) | "when is the next high tide in Boston?", "the tide table for Portland, Maine" | NOAA Tides and Currents |
| [`trending`](./agents/trending) | "what is trending on Wikipedia today?", "how many people read the Lizzie Borden article?" | Wikimedia pageviews |
| [`tsunami`](./agents/tsunami) | "is there a tsunami warning right now?", "any bulletin naming Hawaii?" | tsunami.gov CAP (NTWC + PTWC) |
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

# all 1189 unit tests (offline, no network)
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

Live probe of all 45 agents over real ACP stdio on 2026-09-23 (every answer came from the
dataset, not a fixture). `a2a_bridge` is probed only when `ACP_A2A_ENDPOINTS` points at a
running A2A server, so that run shows 44:

```
[PASS] air         (0.89s) - how is the air quality in Delhi right now?            tool: air-now
[PASS] alert       (1.48s) - alert me when the air quality in Delhi passes 200 for 1 check every 5 seconds tool: alert-watch
[PASS] archive     (0.87s) - find Apollo 11 recordings                             tool: archive-search
[PASS] art         (0.63s) - paintings by Monet                                    tool: art-search
[PASS] aurora      (0.59s) - how are the geomagnetic conditions right now?         tool: aurora-now
[PASS] bikes       (0.97s) - how many citibikes are available right now?           tool: bikes-status
[PASS] books       (0.84s) - find books about urban foxes                          tool: books-search
[PASS] brief       (7.77s) - brief me on Denver                                    tool: brief-gather
[PASS] buoys       (1.20s) - what are the conditions at buoy 41025?                tool: buoy-conditions
[PASS] chart       (1.64s) - chart the temperature in Seattle for the next 12 hours tool: chart-temperature
[PASS] chem        (1.78s) - what is the formula for ibuprofen?                    tool: chem-compound
[PASS] civic       (1.08s) - did complaint 70483808 get fixed?                     tool: complaint-status
[PASS] crypto      (0.53s) - what is the price of bitcoin right now?               tool: crypto-price
[PASS] domains     (0.49s) - when does example.com expire?                         tool: domain-info
[PASS] drought     (0.83s) - how dry is California?                                tool: drought-place
[PASS] firehose    (1.03s) - watch the wiki firehose for 2 edits                   tool: firehose-edits
[PASS] floodwatch  (1.89s) - what is the Mississippi River at St. Louis doing?     tool: flood-status
[PASS] food        (1.26s) - how much sugar is in Nutella?                         tool: food-find
[PASS] forecast    (0.77s) - when will it rain in Seattle today?                   tool: weather-rain
[PASS] fx          (0.54s) - what is 100 USD in EUR?                               tool: fx-rate
[PASS] groundwater (1.21s) - water level at site 395847084085500                   tool: groundwater-well
[PASS] hazards     (0.62s) - any weather alerts in NY right now?                   tool: alerts-active
[PASS] iss         (1.29s) - could I see the ISS from Denver tonight?              tool: iss-sky
[PASS] labels      (1.68s) - what is lipitor?                                      tool: label-info
[PASS] launches    (0.92s) - what is the next launch?                              tool: launches-upcoming
[PASS] ledger      (1.05s) - what is the national debt right now?                  tool: debt-outstanding
[PASS] local       (0.51s) - what is going on in this repo?                        tool: git
[PASS] moon        (1.06s) - when is the next full moon?                           tool: moon-phases
[PASS] nature      (3.36s) - how many monarch butterflies are recorded in Canada?  tool: nature-count
[PASS] osm         (1.48s) - nearest drinking water to Bryant Park, New York       tool: osm-near
[PASS] papers      (0.83s) - papers about CRISPR sickle cell                       tool: papers-search
[PASS] pollen      (1.34s) - how bad is the pollen today in Denver?                tool: pollen-now
[PASS] rivers      (0.77s) - what is gauge 06730500 doing right now?               tool: river-stage
[PASS] snowpack    (2.92s) - how much water is in the California snowpack?         tool: snowpack-state
[PASS] species     (1.05s) - how many observations of Danaus plexippus are there?  tool: species-count
[PASS] stocks      (0.61s) - what is AAPL doing right now?                         tool: stock-quote
[PASS] surf        (1.30s) - how are the waves at Pipeline right now?              tool: surf-now
[PASS] tides       (2.54s) - when is the next high tide in Boston?                 tool: tide-next
[PASS] trending    (0.61s) - what is trending on Wikipedia today?                  tool: trending-top
[PASS] tsunami     (0.47s) - is there a tsunami warning right now?                 tool: tsunami-status
[PASS] vehicles    (0.35s) - recalls for the 2015 Honda Civic                      tool: vehicle-recalls
[PASS] watch       (6.62s) - watch the aurora for 3 rounds every 3 seconds         tool: watch-aurora
[PASS] wiki        (0.43s) - who was Ada Lovelace?                                 tool: wiki-summary
[PASS] wildfire    (0.79s) - what wildfires are burning in California right now?   tool: wildfire-active
== 44/44 agent(s) answered a live question  (a2a_bridge skipped: no A2A server running)
```

The probe also fails an answer that is the agent's **own help text**. That check earns its
keep: it caught `stocks` replying "Ask me: - what is Apple trading at?" to "what is AAPL doing
right now?" - a green probe hiding an unanswered question.

The second check names a turn where the agent reported that its feed was down (`I could not
read ...`). Such a turn is a pass - reporting an outage is the right answer - but it is not
proof of live data, so the run says `upstream down for osm` instead of quietly counting it.
That is how the missing third Overpass mirror was found: the run above is the one after the
fix, where `osm` answered in 1.48s.

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
The next high tide for BOSTON, MA (8443970):
  - right now: 8.19 ft above MLLW (the gauge read 2026-09-22 23:00 station local time)
  - next HIGH 8.72 ft at 2026-09-23 09:39
Drinking water near Bryant Park (40.7538, -73.9835) within 800 m:
  - 60 m  (unnamed on OpenStreetMap) (wheelchair: yes)
  OpenStreetMap has 11 mapped within 800 m.
Watching the US Air Quality Index at a place - above 200. Up to 1 round(s), 5s apart (5s at most).
  Not met after 1 check(s) in 1s (last reading 153 AQI). Nothing is still checking now.
2,074 item(s) match 'Apollo 11' (audio) in the Internet Archive:
  Apollo11Audio - Apollo 11 - audio, NASA - 295,152 downloads
  (I showed the 3 most downloaded of 2,074)
Nutella (Nutella - 400 g e)  •  sugars: 56.8 g per 100 g  •  Nutri-Score: e (worst)  •  NOVA 4
  allergens: milk, nuts, soybeans  •  ingredients: Sugar, palm oil, HAZELNUTS 13%, cocoa.
CA - drought as of 2026-09-15 (week ending 2026-09-21):
  65.23% of the area is at least abnormally dry (D0 or worse)  •  34.77% has no drought at all
MI-3A OH (395847084085500) - Miami County, Ohio - well depth 130 ft
  depth to water: 9.95 ft below land surface on 2026-09-22 (Provisional)
CO - snow water equivalent (in), from every SNOTEL snow station in the state:
  118 station(s) reported a value; 41 of them have any snow water equivalent at all
No tsunami bulletin is in force right now.
  newest bulletin: Tsunami Information from PTWC Honolulu, expired 2026-09-18T14:25:30+00:00
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
- **No keys.** All 45 use keyless public APIs. Nothing to sign up for, nothing to leak.
  ([`local`](./agents/local) uses no API at all — it reads this machine.)

## Layout

```
acp_kit/            protocol layer: rpc.py (NDJSON JSON-RPC), agent.py, client.py
agents/<name>/      data.py (feed reader + validation)  agent.py (routing + skills + ACP)
agents/<name>/tests/test_agent.py
tests/test_kit.py   protocol tests
tests/test_probe.py what a green probe line still would not prove (help text, feed outage)
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
| `FIREHOSE_HTTP_TIMEOUT` | [`firehose`](./agents/firehose) | seconds to wait on the event stream before giving up (20) |
| `BRIEF_CACHE_TTL` | [`brief`](./agents/brief) | seconds one gathered section is reused (300) |
| `ALERT_HTTP_TIMEOUT` | [`alert`](./agents/alert) | seconds to wait on one condition check (25) |
| `ACP_A2A_ENDPOINTS` | [`a2a_bridge`](./agents/a2a_bridge) | `name=url,...` of A2A servers to reach |
| `ACP_BRIDGE_PUSH_PORT` | [`a2a_bridge`](./agents/a2a_bridge) | local port the A2A push webhook listens on |

## Honest limits

- Nearly everything here is read-only. The one exception is [`brief`](./agents/brief): it asks
  with `session/request_permission` and then writes a single `BRIEFING.md` through the editor's
  own `fs/write_text_file`, so the file lands in your workspace rather than on the side. Refuse,
  and nothing is written and the briefing is printed instead. [`local`](./agents/local) can only
  run `git` (reporting) and `lsof` (listing), never a write, a commit or a kill.
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
- [`chart`](./agents/chart) draws its PNG in the standard library (no image dependency) and is
  capped at 640x360. A series longer than the pixels allows is sampled, and the answer says
  which points were skipped rather than drawing a picture of something else.
- [`firehose`](./agents/firehose) is a push stream and cannot be anything else: it is capped at
  25 events and 60 seconds per turn, and its filters never hide what they filtered — the answer
  says "2 of 12 seen", so "no matches" is never mistaken for a quiet wiki.
- [`alert`](./agents/alert) is silent by design. It sends nothing until the condition is met,
  then one message; capped at 60 checks, 300s apart and 900s in total. It waits on the
  session's own cancel event, so cancelling stops it immediately. Quake watches fire on a
  *new* event id, not on a magnitude that was already there.
- [`osm`](./agents/osm) reads volunteer-mapped data: an empty answer means nobody has mapped
  it yet, not that it is not there.
- [`domains`](./agents/domains) reports a registry 404 as "no registration found" and
  deliberately does not call that "available" — only a registrar can promise that.
- [`papers`](./agents/papers) says which service answered (PubMed, arXiv or Crossref): a
  preprint is not a peer-reviewed paper, and the agent says so.
- [`tides`](./agents/tides) returns NOAA's harmonic predictions in the station's own local
  clock and labels them as predictions, not as a reading of the water now.
- [`brief`](./agents/brief) gathers each section independently, so one dead source is written
  into the file as "unavailable" instead of silently dropping the section.
- [`food`](./agents/food), [`chem`](./agents/chem), [`archive`](./agents/archive) and
  [`papers`](./agents/papers) read crowd-sourced or third-party catalogues: a missing nutrient,
  a missing file or a wrong entry is the catalogue's, and the answer names the entry (barcode,
  identifier, DOI) so it can be checked. Nutrients are per 100 g because most products carry no
  serving size, and an unrecorded value says "not recorded" rather than zero.
- [`drought`](./agents/drought) explains that the Drought Monitor's categories are cumulative
  (D1 is moderate **or worse**) and covers a state or county: the monitor has no national
  endpoint, and averaging states into one number would be an invention.
- [`groundwater`](./agents/groundwater) reports **depth to water below land surface**, so a
  bigger number is a deeper water table; a negative one is water above the ground. A state
  figure is the newest reading from each reporting well, with the spread, because a water table
  is not one number.
- [`snowpack`](./agents/snowpack) reports snow water equivalent in inches with NRCS's own median
  for that day. Off season every median is zero, which makes "percent of normal" 0 divided by 0,
  and the answer says that instead of printing 0%.
- [`tsunami`](./agents/tsunami) earns its keep by being able to say no: both US centres leave
  their last CAP bulletin on the web after it expires, so every answer compares the expiry with
  now and reports how long ago the last one lapsed. It reads the two US centres only.
- [`trending`](./agents/trending) reads Wikimedia pageviews, which lag a day, so "today" is
  never reported - and it says how many navigation titles (front page, search) it dropped from
  the raw top list.
- [`osm`](./agents/osm) depends on a shared Overpass instance and can take 10-40s; the probe run
  in this README caught a 39s turn. It is slow, not stuck. All three mirrors were tried on
  2026-09-23: `overpass-api.de` answered 504, `kumi.systems` timed out, and the third
  (`maps.mail.ru`, from QuickOSM's mirror list) answered in 22s, so a turn can now succeed
  where it used to report an outage.
- An agent that does not know a place says so by name instead of guessing coordinates.

## License

MIT — see [LICENSE](./LICENSE).
