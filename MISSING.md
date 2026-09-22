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

**The A2A side** (checked while building the sibling repo `mrfentmen/a2a`): no city, agency
or utility publishes an agent card, and no A2A server serves water, tide or flood data. The
registries there are vendor tooling, not public-data agents.

Conclusion that drove this repo: **the empty shelf is domain agents.** Everything below was
missing and was built here.

## Verified missing — built in this repo

Each was checked against the agent list above and the registry before building, then built
against a keyless public feed that was verified live the same day.

| Agent | What was missing | Feed (keyless, verified live) |
|---|---|---|
| `air` | no air quality agent in any editor protocol | `air-quality-api.open-meteo.com` — returned US AQI 158, PM2.5 92.4 for Delhi |
| `aurora` | no space-weather agent | `services.swpc.noaa.gov` — Kp estimate, 3-day forecast, OVATION grid, alert feed |
| `books` | no catalog agent | `openlibrary.org` — 48,208 works matched "dune"; `OL893414W` carried a description |
| `civic` | first non-coding ACP agent of any kind | NYC Open Data Socrata (`erm2-nwe9`, `bkwf-xfky`) |
| `forecast` | no weather agent, and no rain-window tool anywhere | `api.open-meteo.com` — hourly probabilities for any point |
| `fx` | no rates agent | `api.frankfurter.dev` — ECB reference rates, USD/EUR 0.87237 |
| `hazards` | no hazard-feed agent | `api.weather.gov/alerts/active` + `earthquake.usgs.gov/fdsnws` |
| `launches` | no launch-schedule agent | `ll.thespacedevs.com` — 369 upcoming launches on the day |
| `ledger` | no fiscal-data agent | `api.fiscaldata.treasury.gov` (Debt to the Penny, interest, rates, auctions) |
| `rivers` | no stream-gauge agent | `api.waterdata.usgs.gov/ogcapi` — USGS-06730500, 0.22 ft^3/s |
| `vehicles` | no recall/complaint agent | NHTSA recalls, complaints and VIN decoding |
| `wildfire` | no fire-incident agent | NIFC WFIGS current incident layer |
| `a2a_bridge` | no way to use A2A servers from an editor, and no push relay into a session | consumes any A2A agent card (sibling repo) |

## Still missing — candidates, APIs already checked live

Checked 2026-09-22 with a plain `urllib` GET, no key:

| Candidate | Feed | Response on the day |
|---|---|---|
| `species` | `api.inaturalist.org/v1/observations` | 200, `total_results` for *Danaus plexippus* |
| `iss` | `api.wheretheiss.at/v1/satellites/25544` | 200, latitude/longitude/velocity |
| `wiki` | `en.wikipedia.org/api/rest_v1/page/summary/...` | 200, summary + wikibase id |
| `bikes` | GBFS `gbfs.citibikenyc.com/gbfs/en/station_status.json` | 200, station statuses |
| `aircraft` | `opensky-network.org/api/states/all` (anonymous) | 200 over a NYC bounding box — fair-use limits apply |
| `labels` | `api.fda.gov/drug/label.json` | 200, structured product labels |
| `tides` | `api.tidesandcurrents.noaa.gov/api/prod/datagetter` | 200, hi/lo predictions for station 8518750 |
| `apod` | `api.nasa.gov/planetary/apod` | works with `DEMO_KEY` — needs a free key, so it is not keyless |

## Skipped — already a thing

Do not build; the space is taken.

- Coding agents in general: Claude Code, Gemini CLI, Codex CLI, GitHub Copilot CLI, Cursor,
  Cline, Goose, OpenCode, OpenHands, Qwen Code, Kimi CLI, Junie, Kiro, Mistral Vibe, Amp —
  all on the official list or in the registry.
- Coding harnesses and adapters: Docker cagent, fast-agent, stdio Bus, AgentPool, Hermes.
- Weather *inside an MCP client*: several MCP weather servers exist (this account's
  `awesome-mcps` repo has `7timer-mcp`, `aviation-weather-mcp`, `brightsky-mcp`). None of
  them is an ACP agent that an editor can spawn, which is why `forecast` was built here.
- Books *inside an MCP client*: `books-mcp` and `openlibrary` adapters exist on the MCP
  side; nothing on the ACP side, so `books` was built here.
- Things this repo already covers, so a new agent would duplicate: earthquakes (inside
  `hazards`) and NYC water (inside `civic`). Drug and food recalls are covered on the MCP
  side; the ACP side is still a candidate, not a duplicate.

## Dropped — no usable public API

- Live ship positions (AIS) — no keyless public feed.
- Utility outage maps — interactive sites, no documented API.
- Package-locker and delivery tracking — account-gated, no public API.
- Anything requiring a credit card or a signed agreement — against the "no keys" rule of
  this repo.

## How to add to this list

Add a row with the feed URL, what it returned on the day you checked, and what was searched
to prove the agent does not exist yet (the agent list, the registry, and a plain web search
for the domain name plus "ACP" or "agent client protocol").
