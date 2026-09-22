# Missing ACP agents — research log

Living list of ACP agents that did **not** exist yet, what was checked and skipped and why,
and what is still missing. Everything below was verified on **2026-09-22** against primary
sources, not memory.

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
| `civic` | first non-coding ACP agent of any kind | NYC Open Data Socrata (`erm2-nwe9`, `bkwf-xfky`) |
| `forecast` | no weather agent, and no rain-window tool anywhere | Open-Meteo hourly rain probabilities |
| `fx` | no rates agent | Frankfurter/ECB — USD/EUR 0.87237 |
| `hazards` | no hazard-feed agent | NWS alerts + USGS quakes |
| `iss` | no space-station agent | open-notify position + sunset + cloud cover |
| `labels` | no drug-label agent | openFDA — Lipitor label, 5 sections |
| `launches` | no launch-schedule agent | Launch Library 2 — 369 upcoming launches that day |
| `ledger` | no fiscal-data agent | US Treasury Fiscal Data (debt, interest, rates, auctions) |
| `rivers` | no stream-gauge agent | USGS OGC water API — 0.22 ft^3/s at 06730500 |
| `species` | no biodiversity agent | iNaturalist — 548,334 observations of *Danaus plexippus* |
| `vehicles` | no recall/complaint agent | NHTSA recalls, complaints, VIN decoding |
| `wiki` | no encyclopedia agent | Wikipedia summary + Wikidata entities + on-this-day |
| `wildfire` | no fire-incident agent | NIFC WFIGS current incidents |
| `a2a_bridge` | no way to use A2A servers from an editor | consumes any A2A agent card (sibling repo) |

## The gap map — candidate feeds, all probed live on 2026-09-22

One `urllib` GET per endpoint, no key, 12 s timeout. 77 endpoints were probed; the ones
that answered are below, grouped by the agent they suggest. Nothing here is built yet.

### Nature, ocean, sky

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `nature` | `api.gbif.org/v1/occurrence/search` | 200 — 9,535,182 occurrences for "quercus" (worldwide, beyond iNaturalist) |
| `buoys` | `ndbc.noaa.gov/data/realtime2/41025.txt` | 200 — live wave height/period, wind, water temp for 4,100 stations |
| `surf` | `marine-api.open-meteo.com/v1/marine` | 200 — wave height, direction, period for any coast |
| `floodwatch` | `flood-api.open-meteo.com/v1/flood` | 200 — daily river-discharge forecast (complements `rivers`) |
| `radiation` | `api.safecast.org/measurements.json` | 200 — 52.0 cpm at the latest volunteer sensor |
| `stargazing` | `7timer.info/bin/api.pl?product=astro` | 200 — cloudcover 9, seeing 7, transparency 3 for the next nights |
| `almanac` | `api.sunrise-sunset.org/json` | 200 — sunset 2026-09-22T22:54:52Z for New York |

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
| `crypto` | `api.coingecko.com/api/v3/simple/price` | 200 — BTC $86,239 |
| `crypto2` | `api.coinbase.com/v2/exchange-rates` | 200 — BTC in ~250 currencies |
| `companies` | `api.gleif.org/api/v1/lei-records` | 200 — 370 LEI records matched "Apple" (legal entity identity) |
| `development` | `api.worldbank.org/v2/country/US/indicator/...` | 200 — GDP and 16k other series |
| `spending` | `api.usaspending.gov/api/v2/...` | 200 — federal agencies, awards |
| `courts` | `courtlistener.com/api/rest/v4/search` | 200 — 117,101 opinions matched "privacy" (keyless v4!) |

### Health and science

| Candidate | Feed | Probe result on the day |
|---|---|---|
| `trials` | `clinicaltrials.gov/api/v2/studies` | 200 — melanoma trials, structured protocol |
| `drugnames` | `rxnav.nlm.nih.gov/REST/drugs.json` | 200 — "lipitor" → atorvastatin 80 MG |
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
| `nutrition` | `world.openfoodfacts.org/api/v2/product/...` | 200 — 3M+ products, allergens + Nutri-Score |
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

1. **Local-first agents (no network at all).** ACP is an *editor* protocol: an agent that
   answers "how much disk is left", "what changed in this repo today", "which ports are
   listening", "what is this branch behind on" is natural — and no ACP agent on the list
   reads the machine it is running on. Needs ACP `terminal` capability and permission
   design; genuinely novel.
2. **Watch agents, not answer agents.** `session/update` can stream. An agent that watches
   a feed (fire perimeters, air quality, aurora, grid carbon) and pushes updates into the
   session until cancelled exists nowhere.
3. **Composite briefings.** One question, several feeds: "brief me for Denver today" =
   air + alerts + fires + launches + grid. Everything needed is built; the shape is new.
4. **City packs.** New York alone has hundreds of Socrata datasets (subway, restaurant
   grades, trees, taxi, parking, noise). `civic` touches two. A `nyc` agent with ten
   skills is a different product from ten agents.
5. **Agency agents.** NWS, USGS, NOAA, NASA, Census — none publishes an ACP agent, and
   only NWS has an A2A server (built in the sibling repo). Each has the feeds ready.
6. **Write-capable agents behind permission.** Notes, changelogs, TODO triage. Every agent
   here is read-only by design; ACP has `fs/write_text_file` and nothing uses it.

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
