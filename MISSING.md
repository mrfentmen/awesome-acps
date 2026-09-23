# Missing ACP agents — research log

Living list of ACP agents that did **not** exist yet, what was checked and skipped and why,
and what is still missing. Everything below was verified against primary sources, not memory,
on the date each section states (the first sweep on **2026-09-22**, sessions 2-4 on
**2026-09-23**).

## What already exists

**The official agent list** — <https://agentclientprotocol.com/get-started/agents> — read
live on 2026-09-22. 39 entries: AgentPool, Augment Code, AutoDev, Blackbox AI, Bub, Claude
Agent, Claw Orchestrator, Cline, Codex CLI, Code Assistant, Construct, crow-cli, Cursor,
Docker cagent, fast-agent, Factory Droid, fount, Gemini CLI, GitHub Copilot, Goose, Hermes
Agent, Junie, Kaagum, Kimi CLI, Kiro CLI, localharness, Minion Code, Mistral Vibe,
OpenClaw, OpenCode, OpenHands, Pi, Poolside, Qoder CLI, Qwen Code, Raxol, siGit Code,
Stakpak, stdio Bus, VT Code.

Every one of them is a **coding agent** or a coding-agent harness/adapter. None serves a
public dataset domain.

**The ACP Registry** — <https://agentclientprotocol.com/get-started/registry>, announced by
Zed on 2026-01-28 (<https://zed.dev/blog/acp-registry>) — distributes the same kind of
agent: Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode, Gemini CLI, Goose and similar.

**GitHub, searched live this session** (`gh search repos` on 2026-09-22, ~60 repos). The
ACP ecosystem is clients, SDKs, adapters and coding agents — no domain agent anywhere:

| Repo | Stars | What it is |
|---|---|---|
| `agentclientprotocol/agent-client-protocol` | 4.3K | the spec |
| `openclaw/acpx` | 3.3K | headless CLI client for ACP sessions |
| `RAIT-09/obsidian-agent-client` | 2.4K | ACP client for Obsidian |
| `formulahendry/acp-ui` | 482 | cross-platform ACP client UI |
| `agentclientprotocol/registry` | 404 | the registry |
| `formulahendry/vscode-acp` | 386 | ACP client for VS Code |
| `nuskey8/UnityAgentClient` | 277 | ACP in Unity |
| `coder/acp-go-sdk`, `agentclientprotocol/kotlin-sdk`, `ninetwentyfour`/swift/Elixir/C# | 236→20 | SDKs |
| `xenodium/acp.el` | 172 | ACP in Emacs |
| `cola-io/codex-acp`, `shubzkothekar/antigravity-acp`, `roshan-c/cursor-acp`, `letta-ai/letta-acp`, `stefandevo/glm-acp-agent` | 144→13 | adapters that wrap a coding agent |
| `agentclientprotocol/acpr` | 9 | an experimental **meta** ACP agent |
| `nMaroulis/awesome-agent-client-protocol` | 3 | curated list of frameworks/tools/agents (all coding-side) |
| `GongRzhe/ACP-MCP-Server`, `Oortonaut/mcacp` | 24, 9 | bridges between ACP and MCP |

**For scale**: the MCP side has 400 servers in 34 categories on one ranked list
(`tolkonepiu/best-of-mcp-servers`) and 9,800+ on mcpservers.org. ACP has ~40 agents and
every one is a coding agent. **The empty shelf is domain agents** — that is this repo's
whole thesis, and it still holds after this session's search.

## Verified missing — built in this repo

Each was checked against the agent list, the registry and a GitHub search before building,
then built against a keyless public feed that was verified live the same day.

| Agent | What was missing | Feed (keyless, verified live) |
|---|---|---|
| `air` | no air quality agent in any editor protocol | Open-Meteo air quality — US AQI 158, PM2.5 92.4 for Delhi |
| `art` | no museum agent | AIC (132,828 works), Cleveland, The Met — AIC answered "Water Lilies" |
| `aurora` | no space-weather agent | NOAA SWPC — Kp estimate, 3-day forecast, OVATION grid, alerts |
| `bikes` | no bike-share agent | Citi Bike GBFS — 2,520 stations, 33,617 bikes joined live |
| `books` | no catalog agent | Open Library — 48,208 works matched "dune" |
| `buoys` | no buoy/marine-observation agent | NOAA NDBC realtime2 — 1,353 stations, live waves and water temp |
| `civic` | first non-coding ACP agent of any kind | NYC Open Data Socrata (`erm2-nwe9`, `bkwf-xfky`) |
| `floodwatch` | no flood/NWPS gauge agent | NWS NWPS gauge stats (observed, forecast, flood stages) + USGS site search |
| `forecast` | no weather agent, and no rain-window tool anywhere | Open-Meteo hourly rain probabilities |
| `fx` | no rates agent | Frankfurter/ECB — USD/EUR 0.87237 |
| `hazards` | no hazard-feed agent | NWS alerts + USGS quakes |
| `iss` | no space-station agent | open-notify position + sunset + cloud cover |
| `labels` | no drug-label agent | openFDA — Lipitor label, 5 sections |
| `launches` | no launch-schedule agent | Launch Library 2 — 369 upcoming launches that day |
| `ledger` | no fiscal-data agent | US Treasury Fiscal Data (debt, interest, rates, auctions) |
| `local` | **no ACP agent anywhere reads the machine it runs on** | this machine only: `git rev-parse/status/log` and `lsof -iTCP -sTCP:LISTEN`, read-only |
| `nature` | no biodiversity-occurrence agent taken from GBIF itself | GBIF occurrence API — 817,344 records for *Danaus plexippus*, 87,502 in Canada |
| `rivers` | no stream-gauge agent | USGS OGC water API — 0.22 ft^3/s at 06730500 |
| `species` | no biodiversity agent | iNaturalist — 548,334 observations of *Danaus plexippus* |
| `surf` | no surf/sea-state agent (a buoy is a point; this answers anywhere) | Open-Meteo marine + wind — 1.7 m at 8.1 s at Pipeline |
| `vehicles` | no recall/complaint agent | NHTSA recalls, complaints, VIN decoding |
| `watch` | **no ACP agent streams a feed until cancelled** | USGS all_hour + NOAA Kp-1m + open-notify ISS, polled per round |
| `wiki` | no encyclopedia agent | Wikipedia summary + Wikidata entities + on-this-day |
| `wildfire` | no fire-incident agent | NIFC WFIGS current incidents |
| `a2a_bridge` | no way to use A2A servers from an editor | consumes any A2A agent card (sibling repo) |
| `chart` | **no agent anywhere answers with a picture** (zero image-content-block repos found) | Open-Meteo + Treasury + USGS, drawn as a real PNG in the standard library |
| `tides` | no tide agent, and no NOAA Tides and Currents agent | `mdapi` stations (3,499) + harmonic predictions + the gauge's live reading |
| `osm` | no points-of-interest agent; nothing reads OpenStreetMap | Nominatim for the place, Overpass for the POIs (fountains, chargers, ATMs) |
| `domains` | no whois/RDAP agent for registration or expiry | RDAP via `rdap.org` — example.com expires 2027-08-13 |
| `papers` | no literature agent that says which index answered | PubMed E-utilities + arXiv Atom + Crossref, all keyless |
| `firehose` | **no ACP agent is fed by a real push channel** | Wikimedia EventStreams (SSE) — live edits, pushed per event |
| `brief` | **no agent writes a file for a non-coding task** | five keyless feeds, then `fs/write_text_file` after permission |
| `alert` | **no agent waits on a *condition* rather than a value** | Open-Meteo AQI/temp, USGS quakes, NWPS flood stage, Treasury debt |
| `pollen` | no allergy/pollen agent (air quality is not pollen) | Open-Meteo air quality, 5 pollen species |
| `moon` | no moon-phase or dark-sky agent | USNO phases + sunrise-sunset.org |
| `crypto` | no cryptocurrency or DeFi agent | CoinGecko + DefiLlama |
| `stocks` | no equity-quote agent (RIP `finance`) | Yahoo Finance chart endpoint |
| `trending` | no "what is the world reading" agent (Wikipedia summaries are not traffic) | Wikimedia pageviews API |
| `archive` | no archive/library agent | Internet Archive search + item metadata |
| `food` | no food-label-by-product agent (drug labels are not food) | Open Food Facts |
| `chem` | no chemistry or generic-name agent | PubChem PUG-REST + RxNorm |
| `drought` | no drought-monitor agent | US Drought Monitor (CSV, state/county) |
| `groundwater` | no groundwater-observation agent | USGS Water Data OGC, observation wells |
| `snowpack` | no snow/water-supply agent | NRCS AWDB (SNOTEL) |
| `tsunami` | no tsunami-bulletin agent, and none that reports an expired bulletin | tsunami.gov CAP (NTWC + PTWC) |

## Session 2 — 2026-09-23: six more agents, and what broke on the way

Six agents were added (`nature`, `buoys`, `surf`, `floodwatch`, `local`, `watch`). Every feed
was probed with a real request before a line of code was written, and four upstream quirks
changed the design:

1. **The Open-Meteo flood API does not answer for a river.** It returns a model grid cell.
   It reported 0.45 m^3/s for the Mississippi at St. Louis and 1.34 m^3/s at Baton Rouge,
   where the real river runs in the thousands. `floodwatch` uses NWS NWPS instead, which
   publishes observed stage, forecast, and the action/minor/moderate/major stages - for
   EADM7 on the day: 13.21 ft observed, 16.6 ft forecast, action at 28 ft.
2. **NWPS has no name search usable per turn.** Its gauge list is ~13 MB and about 50 s to
   download, and its `?state=` filter is ignored. So a river name is resolved through the
   USGS site API first, and the answer names the gauge it landed on.
3. **The USGS name filter is case-sensitive.** `monitoring_location_name LIKE '%Willamette
   River%'` returns **zero** rows, because USGS stores `WILLAMETTE RIVER AT SALEM, OR`.
   `LOWER(...) LIKE '%willamette river%'` fixes it and now returns the main-stem gauges
   (Salem, Albany, Eugene) first. USGS also spells connectors its own way - no site is
   named "Colorado River at Austin" - so the search widens word by word instead of failing.
4. **GBIF cannot map common names.** Its own search ranks a clam above *Panthera tigris*
   for "tiger", `suggest` is Latin-only autocomplete, and Wikidata 429s under repeated use.
   iNaturalist's taxa endpoint is the resolver that works (exact name, then rank, then a
   record check), with GBIF still doing the counting.

The same session reused three findings from the first one: open-notify's HTTPS endpoint
hangs while HTTP answers in 0.2 s (`watch`), NDBC's `MM` means a sensor sent nothing rather
than zero (`buoys`), and Citi Bike station ids are mostly UUIDs, not numbers (`bikes`).

Two ACP capabilities beyond question-and-answer are now in use for the first time in this
repo: `session/update` as a live feed, one message per round and each round its own message
id (`watch`), and `session/cancel` as a real stop that interrupts the sleep between rounds
rather than after it (`watch`). The third is the session's own working directory: `local`
reads the folder the editor was launched in, so "what is going on in this repo" needs no
path at all.

## The gap map — candidate feeds, all probed live on 2026-09-22

One `urllib` GET per endpoint, no key, 12 s timeout. 77 endpoints were probed; the ones
that answered are below, grouped by the agent they suggest. Rows marked **built** became
agents in a later session; the rest are still unbuilt.

### Nature, ocean, sky

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `nature` | `api.gbif.org/v1/occurrence/search` | 200 — 9,535,182 occurrences for "quercus" — **built**, see the 2026-09-23 session below |
| `buoys` | `ndbc.noaa.gov/data/realtime2/41025.txt` | 200 — live wave height/period, wind, water temp for 1,353 stations — **built** |
| `surf` | `marine-api.open-meteo.com/v1/marine` | 200 — wave height, direction, period for any coast — **built** |
| `floodwatch` | `flood-api.open-meteo.com/v1/flood` | 200, **but useless**: see "the flood API was the wrong river" below |
| `radiation` | `api.safecast.org/measurements.json` | 200 — 52.0 cpm at the latest volunteer sensor |
| `stargazing` | `7timer.info/bin/api.pl?product=astro` | 200 — cloudcover 9, seeing 7, transparency 3 for the next nights |
| `almanac` | `api.sunrise-sunset.org/json` | 200 — sunset 2026-09-22T22:54:52Z for New York — **built** into `moon` |

### Space

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `nasamedia` | `images-api.nasa.gov/search` | 200 — 140,000+ images/video/audio, keyless (unlike most NASA APIs) |
| (satellites) | `celestrak.org/NORAD/elements/gp.php` | not probed — TLE elements are keyless and would pair with `iss` |

### Transport and cities

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `boston` | `api-v3.mbta.com/predictions` | 200 — live predictions at Park St, keyless |
| `bayarea` | `api.bart.gov/api/etd.aspx` | 200 — BART departures (the published demo key is public) |
| `uk-postcode` | `api.postcodes.io/postcodes/SW1A1AA` | 200 — coordinates + NHS region |
| `zip` | `api.zippopotam.us/us/11235` | 200 — ZIP → Brooklyn |
| `airports` | `aviationweather.gov/api/data/metar` | 200 — KJFK METAR 19.4 °C |
| `planes` | `opensky-network.org/api/states/all` | 200 — live aircraft states over a bbox (anonymous, fair-use) |
| `ferries` / `trains` | MBTA + BART above | same shape, different agency |

### Money and companies

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `bitcoin` | `mempool.space/api/v1/fees/recommended` | 200 — fastestFee 6 sat/vB, plus blocks and tx lookup |
| `crypto` | `api.coingecko.com/api/v3/simple/price` | 200 — BTC $86,239 — **built** (with DefiLlama below) |
| `crypto2` | `api.coinbase.com/v2/exchange-rates` | 200 — BTC in ~250 currencies |
| `companies` | `api.gleif.org/api/v1/lei-records` | 200 — 370 LEI records matched "Apple" (legal entity identity) |
| `development` | `api.worldbank.org/v2/country/US/indicator/...` | 200 — GDP and 16k other series |
| `spending` | `api.usaspending.gov/api/v2/...` | 200 — federal agencies, awards |
| `courts` | `courtlistener.com/api/rest/v4/search` | 200 — 117,101 opinions matched "privacy" (keyless v4!) |

### Health and science

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `trials` | `clinicaltrials.gov/api/v2/studies` | 200 — melanoma trials, structured protocol |
| `drugnames` | `rxnav.nlm.nih.gov/REST/drugs.json` | 200 — "lipitor" → atorvastatin 80 MG — **built** into `chem` |
| `dailymed` | `dailymed.nlm.nih.gov/.../spls.json` | 200 — LIPITOR SPL (a second label source to `labels`) |
| `disease` | `disease.sh/v3/covid-19/all` | 200 — global case stats |
| `cves` | `api.first.org/data/v1/epss` | 200 — EPSS exploit probability for a CVE |
| `deps` | `api.osv.dev/v1/query` | 200 — lodash ReDoS advisory (POST, keyless) |

### Culture and consumers

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `tv` | `api.tvmaze.com/search/shows` | 200 — "Severance", full episode lists, air dates |
| `music` | `api.deezer.com/search` | 200 — "Creep" with 30 s previews |
| `music2` | `itunes.apple.com/search` | 200 — albums/podcasts, artwork |
| `recipes` | `themealdb.com/api/json/v1/1/search.php` | 200 — Chicken Handi |
| `drinks` | `thecocktaildb.com/api/json/v1/1/search.php` | 200 — Margarita |
| `nutrition` | `world.openfoodfacts.org/api/v2/product/...` | 200 — 3M+ products, allergens + Nutri-Score — **built** as `food` |
| `poetry` | `poetrydb.org/author/Emily%20Dickinson` | 200 — full poems |
| `comics` | `xkcd.com/info.0.json` | 200 — latest comic #3301 |
| `mtg` | `api.scryfall.com/cards/named` | 200 — Lightning Bolt, oracle text + prices |
| `pokedex` | `pokeapi.co/api/v2/pokemon/pikachu` | 200 — full Pokédex |
| `trivia` | `opentdb.com/api.php` | 200 — quiz bank |
| `brewery` | `api.openbrewerydb.org/v1/breweries` | 200 — global brewery directory |

### Sport and games

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `f1` | `api.jolpi.ca/ergast/f1/2026/next/` | 200 — next race (the community successor to Ergast) |
| `football` | `api.openligadb.de/getmatchdata/bl1` | 200 — Bundesliga fixtures/results |
| `teams` | `thesportsdb.com/api/v1/json/3/...` | 200 — Arsenal, badges, stadiums |
| `nba` | `site.api.espn.com/.../nba/scoreboard` | 200 — live scoreboard |
| `chess` | `lichess.org/api/user/thibault` | 200 — ratings and game history |
| `deals` | `cheapshark.com/api/1.0/deals` | 200 — game deals across stores |
| `steam` | `store.steampowered.com/api/appdetails` | 200 — Dota 2 app metadata |

### Dev, news, utilities

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `packages` | pypi.org / registry.npmjs.org / crates.io | 200 — versions, licenses, downloads |
| `depsreport` | `api.deps.dev/v3alpha/...` | 200 — Google's dependency graph |
| `wires` | HN Firebase + `lobste.rs/hottest.json` + `dev.to/api` | 200 — what the tech world is reading |
| `ip` | `ipwho.is/8.8.8.8` | 200 — IP geolocation (no key) |
| `grid` | `api.carbonintensity.org.uk/intensity` | 200 — UK grid 226 gCO2/kWh, "high" |
| `holidays` | `date.nager.at/api/v3/PublicHolidays/2026/US` | 200 — public holidays, 100+ countries |
| `clock` | `timeapi.io/api/Time/current/zone` | 200 — any timezone |

## Dead or blocked on 2026-09-22 (do not build on these)

| Feed | What happened | Rescue path |
|---|---|---|
| `api.jikan.moe` (MyAnimeList) | 504, upstream MAL unavailable | AniList GraphQL, or retry later |
| `musicbrainz.org/ws/2` | 503, "server busy" | retry with 1 req/s + a real User-Agent; Deezer/iTunes are live alternatives |
| `googleapis.com/books` | 429, shared-IP quota exhausted | Open Library / Gutendex |
| `gutendex.com` | timeout on the day | retry, or Project Gutenberg mirrors |
| `api.pokemontcg.io` | 502 behind Cloudflare | retry; PokéAPI answered |
| `boardgamegeek.com/xmlapi2` | 401 — BGG now requires auth | dropped under the no-keys rule |
| `restcountries.com/v3.1` | deprecated, returns a move notice | move to the current API version or drop |
| `v6.db.transport.rest` | 503 on the day | MBTA/BART answered; retry transport.rest later |
| `crt.sh` | timeout | other certificate-transparency mirrors, or skip |
| `data.sec.gov` | 403 without a declared contact User-Agent | needs a deliberate UA decision, not a silent fix |
| `cloudflare-dns.com/dns-query` | 400 — endpoint needs `Accept: application/dns-json` | trivial fix if a DNS agent is wanted |

## Where the next agents should come from — shapes, not just feeds

Feed-by-feed agents are the first shelf. These are the empty ones after it:

1. ~~**Local-first agents (no network at all).**~~ **Built on 2026-09-23 as
   [`local`](./agents/local)**: disk, git and listening ports for the session's own working
   directory, read-only, through an allow-list of two commands. The remaining gaps in this
   shape: ports *owned* by a project (not the whole machine), dependency freshness, and
   anything needing the ACP `terminal` capability.
2. ~~**Watch agents, not answer agents.**~~ **Built on 2026-09-23 as
   [`watch`](./agents/watch)**: it pushes one message per round and stops on
   `session/cancel`. Still unbuilt in this shape: watch a *changing condition* (air quality
   over a threshold, a fire perimeter growing) and watch a feed that genuinely pushes
   (websocket, SSE) instead of being polled.
3. **Composite briefings.** One question, several feeds: "brief me for Denver today" =
   air + alerts + fires + launches + grid. Everything needed is built; the shape is new.
4. **City packs.** New York alone has hundreds of Socrata datasets (subway, restaurant
   grades, trees, taxi, parking, noise). `civic` touches two. A `nyc` agent with ten
   skills is a different product from ten agents.
5. **Agency agents.** NWS, USGS, NOAA, NASA, Census — none publishes an ACP agent, and
   only NWS has an A2A server (built in the sibling repo). Each has the feeds ready.
6. **Write-capable agents behind permission.** Notes, changelogs, TODO triage. Every agent
   here is read-only by design; ACP has `fs/write_text_file` and nothing uses it.
7. **Agents that answer with a picture.** ACP content blocks include `image` and `audio`,
   and the kit already negotiates `supports_images`. No agent anywhere emits one — a GitHub
   search for `acp agent image content block` returns **zero repos**. A `chart` agent that
   renders a plot of an open dataset into the editor would be the first of its kind.
8. **Agents that are also servers.** `a2a_bridge` *consumes* an A2A agent card. The
   reciprocal — a domain ACP agent that also publishes an A2A card or an MCP server, so
   another agent can call it — does not exist.

## Session 3 — 2026-09-23: shape research and a fresh feed sweep

The agent list and registry were read live again: **40 agents, still every one a coding
agent or a coding harness**. A GitHub sweep (`gh search repos`, 60+ results) found the new
activity is all plumbing — `acp-inspector`, `codex-acp-gateway`, `acp-openai-bridge`,
`acpferry`, `acpctl`, `acpsub`, `acp-bridge` — plus more adapters for coding agents. No
domain agent appeared. The thesis still holds.

### The unused protocol surface (proved by search, not assumed)

The kit already implements the pieces below and **no agent in this repo, or anywhere else,
uses them for a domain**:

| Surface | Status in the wild |
|---|---|
| `session/request_permission` | only coding agents, to approve a shell command or an edit |
| `fs/write_text_file` | only coding agents; the hits are internal notes (`acp.md`, `CLAUDE.md`), not products |
| `image` content block | **zero repos** — nobody returns a picture over ACP |
| `audio` content block | none found |
| `terminal/create` | coding agents only |
| `session/cancel` | proven here by `watch`; unused for *conditional* watching |

### 47 fresh endpoints probed, 33 answered (2026-09-23)

Same method as before: one keyless `urllib` GET each, 15 s timeout, declared User-Agent.
None of these rows was in the backlog above — this is new ground.

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `tides` | `api.tidesandcurrents.noaa.gov/mdapi/.../stations.json` | 200 — **3,499 tide-prediction stations** |
| `asteroids` | `ssd.jpl.nasa.gov/api/horizons.api` | 200 — NASA JPL Horizons ephemeris, keyless |
| `radar` | `api.rainviewer.com/public/weather-maps.json` | 200 — live weather-radar frames |
| `geocode` | `nominatim.openstreetmap.org/search` | 200 — address to coordinates (ODbL) |
| `places` | `overpass-api.de/api/interpreter` | 200 — **any OSM POI**: fountains, benches, EV chargers, ATMs |
| `airports2` | `davidmegginson.github.io/ourairports-data/airports.csv` | 200 — the full airport table |
| `domains` | `rdap.org/domain/example.com` | 200 — registration, registrar, expiry (whois replacement) |
| `markets` | `query1.finance.yahoo.com/v8/finance/chart/AAPL` | 200 — live quote + history, keyless — **built** as `stocks` |
| `defi` | `api.llama.fi/protocols` | 200 — DeFi TVL across protocols — **built** into `crypto` |
| `btc2` | `blockchain.info/ticker` | 200 — BTC in ~250 currencies |
| `rules` | `federalregister.gov/api/v1/documents.json` | 200 — the latest federal rules |
| `congress` | `govtrack.us/api/v2/role?current=true` | 200 — 541 sitting members |
| `labor` | `api.bls.gov/publicAPI/v2/timeseries/data/...` | 200 — US labor statistics |
| `eustats` | `ec.europa.eu/eurostat/.../prc_hicp_manr` | 200 — EU inflation series |
| `preprints` | `export.arxiv.org/api/query` | 200 — arXiv metadata |
| `papers` | `eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | 200 — PubMed, 1,258 hits for "urban fox" |
| `citations` | `api.crossref.org/works` | 200 — 789,000 results with DOIs |
| `healthstats` | `ghoapi.azureedge.net/api/Indicator` | 200 — WHO global health indicators |
| `trending` | `wikimedia.org/api/rest_v1/metrics/pageviews/top` | 200 — what the world is reading today — **built** |
| `archive` | `archive.org/advancedsearch.php` | 200 — Internet Archive search — **built** |
| `japanese` | `jisho.org/api/v1/search/words` | 200 — Japanese dictionary + readings |
| `lyrics` | `api.lyrics.ovh/v1/Coldplay/Yellow` | 200 — full lyrics |
| `radio` | `de1.api.radio-browser.info/json/stations/search` | 200 — the global radio-station directory |
| `spaceflight` | `api.spaceflightnewsapi.net/v4/articles` | 200 — 36,157 space-news articles |
| `satellites` | `celestrak.org/NORAD/elements/gp.php?GROUP=stations` | 200 — **live TLEs** for the whole fleet (the row above was never probed) |
| `bluesky` | `public.api.bsky.app/xrpc/app.bsky.actor.getProfile` | 200 — keyless AT Protocol reads |
| `fediverse` | `lemmy.world/api/v3/post/list` | 200 — Lemmy posts |
| `weather2` | `api.met.no/weatherapi/locationforecast/2.0/compact` | 200 — MET Norway, a second source to Open-Meteo |
| `grid` | `api.carbonintensity.org.uk/intensity` | 200 — UK grid 176 gCO2/kWh (re-confirmed, still unbuilt) |
| `apod` | `api.nasa.gov/planetary/apod?api_key=DEMO_KEY` | 200 — the shared `DEMO_KEY` works, so APOD is usable |
| `ip` | `ipwho.is/8.8.8.8` | 200 (re-confirmed) |
| `images` | `commons.wikimedia.org/w/api.php` | 200 — Commons file search with direct image URLs |
| `novelty` | `dog.ceo/api/breeds/image/random` | 200 — random photo; noted only because it is keyless |

### Dead on 2026-09-23 (do not build)

| Feed | What happened |
|---|---|
| `reddit.com/*.json` | 403 — blocked from this host |
| `api.openalex.org` | 429 — rate-limited |
| `api.semanticscholar.org` | 429 — rate-limited |
| `api.coincap.io` | DNS failure |
| `mastodon.social/api/v1/timelines/public` | 422 — needs the right instance and params |
| `volcano.si.edu` | 403 |
| `xeno-canto.org/api/2` | 404 — the v2 path has moved or needs a key |
| `noaadata.apps.nsidc.org/.../geojson` | 404 |
| `stooq.com`, `numbersapi.com`, `catalog.data.gov` | 404 — wrong paths on the day |
| `graphql.anilist.co` | 404 without a POST body |
| `cloudflare-dns.com/dns-query` | 400 — needs `Accept: application/dns-json` |
| `api.tidesandcurrents.noaa.gov/api/prod/datagetter` | 400 — parameter error; the `mdapi` stations endpoint is the way in |

## Session 4 — 2026-09-23: the unused protocol surface, now used (eight agents)

Session 3 proved by search that four ACP surfaces existed with no domain use. This session
built the agents that use them, so the gaps in that table are now closed by working code:

| Surface that had no domain use | Agent that now uses it |
|---|---|
| `image` content block (**zero repos**) | `chart` — every answer carries a 640x360 PNG, drawn with `zlib` and `struct` alone |
| `fs/write_text_file` with `session/request_permission` | `brief` — asks, then writes `BRIEFING.md` into the workspace the editor opened |
| a push channel, not a poll | `firehose` — Wikimedia SSE, one `session/update` per event |
| `session/cancel` for a *conditional* watch | `alert` — silent until the condition holds; cancel breaks the wait, not the sleep after it |
| `embeddedContext` / `resource_link` text | already handled by the kit; still unused by any agent |
| `audio` content block | still unused; no keyless audio feed worth streaming was found |

Feeds that answered cleanly and are now built: NOAA tide stations, Nominatim + Overpass,
RDAP, PubMed/arXiv/Crossref, Wikimedia EventStreams, Treasury debt, NWS NWPS flood stage.

### What the live data changed in the design

- **Open-Meteo's flood API is not a river.** It reported 0.45 m3/s for the Mississippi at
  St. Louis (the real river is thousands). Dropped it for `floodwatch` and `alert`; NWS NWPS
  gauges are the real thing.
- **USGS site search is case-sensitive.** `LIKE '%Willamette River%'` returns zero rows
  because USGS stores `WILLAMETTE RIVER AT SALEM`. Both sides are lower-cased now.
- **GBIF cannot map common names** ("tiger" ranked a clam above *Panthera tigris*). `nature`
  resolves through iNaturalist first. The same lesson made `tides` reject its own first
  scorer, which had picked "Battery Creek, SC" over "NEW YORK (The Battery)".
- **NOAA defaults `begin_date` to three days ago**, so a naive tide request returns stale
  predictions that look current.
- **`tideType` is empty for all 3,499 stations**, so any reference/subordinate label would
  have been invented. Removed, and the answer reports high versus low instead.
- **arXiv answers HTTP 406 to a burst** and ignores every `sortBy` except `submittedDate`;
  accepted as a real limit, with one retry and a specific message.
- **NWS rejects `limit` together with `point`** (HTTP 400), so the first alerts in the
  payload are used rather than asking for a page size.
- **arXiv itself has no flood-river search**, and NWPS's gauge list is ~13 MB (about 50 s),
  so a river name is resolved through USGS and only the gauge id is sent to NWPS.

### Known bug in older agents, deliberately not touched this session

Seven agents — `air`, `bikes`, `forecast`, `hazards`, `iss`, `rivers`, `species` — parse
coordinates with `\b(-?\d{1,2}...)`. The `\b` blocks the minus sign, so `-33.87,151.21`
(Sydney) is read as `33.87,151.21`: the wrong hemisphere, silently. 13 files are affected
and no test covers it. `surf`, built later, uses the correct pattern. Fixing the seven is a
separate, testable change and is not done here.

## Session 5 — 2026-09-23: twelve more feeds, all probed live (twelve agents)

Session 4's research probed 95 feeds and 71 answered. This session built the twelve that
were most useful and cheapest, then ran the whole repo against live data. 33 → 45 agents,
1189 tests, 44/44 live probes.

| Agent | Ask it | Feed that answered (probed 2026-09-23) |
|---|---|---|
| `pollen` | "how bad is the pollen today in Denver?" | Open-Meteo air quality, 5 species |
| `moon` | "next full moon?", "when does it get dark in Denver?" | USNO phases + sunrise-sunset.org |
| `crypto` | "price of bitcoin?", "how much is locked in DeFi?" | CoinGecko + DefiLlama |
| `stocks` | "what is AAPL doing right now?" | Yahoo Finance chart |
| `trending` | "what is trending on Wikipedia today?" | Wikimedia pageviews |
| `archive` | "find Apollo 11 recordings" | Internet Archive advancedsearch + metadata |
| `food` | "how much sugar is in Nutella?" | Open Food Facts (search v2 + product v2) |
| `chem` | "formula for ibuprofen?", "Lipitor's generic name?" | PubChem + RxNorm |
| `drought` | "how dry is California?" | US Drought Monitor (CSV) |
| `groundwater` | "how deep is the water table in Kansas?" | USGS Water Data OGC |
| `snowpack` | "how much water is in the Sierra snow?" | NRCS AWDB (SNOTEL) |
| `tsunami` | "is there a tsunami warning right now?" | tsunami.gov CAP (NTWC + PTWC) |

### What the live data changed in the design

- **Open Food Facts' legacy search endpoint is down.** `cgi/search.pl` no longer answers, so
  `food` searches through the newer search-a-licious host and reads nutrients from the product
  endpoint. Two round trips, not one.
- **Crowd-sourced numbers can be wrong.** One Oreo entry in Open Food Facts claims 49 kcal per
  100 g. The agent now rounds values, names the barcode it used, and says the catalogue is
  volunteer-filled rather than presenting a number as measured.
- **The Drought Monitor is CSV, not JSON** (the JSON view is a separate, rate-limited service),
  and it has no national endpoint — so the agent answers a state or a county and refuses to
  average states into one invented number.
- **Snowpack's off-season median is zero**, which makes "percent of normal" 0 divided by 0. The
  answer says that instead of printing 0%.
- **USGS groundwater is depth below land surface**, so a bigger number is a deeper water table
  and a negative one is water above the ground. A state figure is the newest reading per well
  with the spread, because a water table is not one number.
- **tsunami.gov leaves expired bulletins on the web**, so every answer compares the CAP expiry
  against now and reports how long ago the last one lapsed. That is what lets it say "no".
- **Wikimedia pageviews lag a day**, so `trending` never calls anything "today"; it also counts
  the navigation titles (main page, search) it drops.
- **A probe can pass on nothing.** `stocks` answered with its own help text and the probe counted
  it as a pass. The probe now compares the answer with the agent's help text and fails a "hollow
  pass". The `stocks` symbol parser was then fixed to read "what is AAPL doing".
- **A probe can pass on an outage.** Two runs in a row had `osm` answer "Overpass did not answer
  on either mirror" and still print `[PASS]`. The probe now flags any answer containing
  `I could not read ...` as `upstream down for <agent>`: a pass, named as one that proves the
  outage is handled rather than the dataset. Probing the mirrors directly showed the real
  trouble: `overpass-api.de` 504, `kumi.systems` read timeout, and a third mirror from
  QuickOSM's list answering in 22s - so `osm` now tries three mirrors, and the next run
  answered in 1.48s.
- **A test that fails for five minutes of every hour.** `chart`'s quake fixture built events at
  "now minus 5, 70, 130 minutes", so between :00 and :05 the newest event fell into the
  *previous* hour bucket and the suite failed (seen at 08:02 UTC, green at 08:06). Simulating
  all 3,600 second-offsets of an hour showed the old fixture breaking 300 of them and the
  hour-anchored one breaking none. The same test now also covers what its name claims: an event
  with no timestamp is ignored, not counted.

### Still open from session 4's list

`flights`, `airport`, `clinicaltrials`, `company`, `solar`, `lobbying`, `nonprofit`, `votes`,
`recalls`, `music`, `podcast`, `radio`, `asteroids`, `sports` and the new *shapes* (`attach`,
`apod`, `gallery`, `memory`, `orchestrator`, `a2a-out`) are researched and unbuilt. `attach`
is the one that matters: the kit handles `embeddedContext` and no agent anywhere reads an
attached file.

## Skipped — already a thing

- Coding agents in general: Claude Code, Gemini CLI, Codex CLI, GitHub Copilot CLI, Cursor,
  Cline, Goose, OpenCode, OpenHands, Qwen Code, Kimi CLI, Junie, Kiro, Mistral Vibe, Amp —
  all on the official list or in the registry.
- Coding harnesses, clients, SDKs and adapters: Docker cagent, fast-agent, stdio Bus,
  AgentPool, acpx, obsidian-agent-client, vscode-acp, acp-ui, acp.el, acp-go-sdk, Unity.
- Meta agents that route to other agents: `agentclientprotocol/acpr` exists (experimental).
- MCP bridges: `GongRzhe/ACP-MCP-Server` and `Oortonaut/mcacp` exist.
- Weather/books *inside an MCP client*: several MCP servers exist. None is an ACP agent an
  editor can spawn, which is why `forecast` and `books` live here.
- Things this repo already covers, so a new agent would duplicate: earthquakes (`hazards`),
  NYC water (`civic`), drug labels (`labels`), bike share (`bikes`), streams (`rivers`).

## Dropped — no usable public API

- Live ship positions (AIS) — no keyless public feed.
- Utility outage maps — interactive sites, no documented API.
- Package-locker and delivery tracking — account-gated.
- Live flight *booking* data, hotel prices — commercial keys only.
- Anything requiring a credit card or a signed agreement — against the "no keys" rule.

## How to add to this list

Probe the feed with a plain `urllib` GET before writing a row: record the HTTP status, the
sample payload, and the date. Prove the agent does not exist yet by checking the official
agent list, the registry, a GitHub search, and a plain web search for the domain plus "ACP"
or "agent client protocol". Then build it in this repo's three-file shape and run
`python3 tools/probe_all.py <name>`.
