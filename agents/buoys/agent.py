"""Buoys — the NDBC sea report inside your editor.

Every ACP agent on the vendors' list writes code. This one reads the sea: NDBC's moored buoys
and coastal stations, reporting wave height, wind, water temperature and pressure right now,
as text files straight off the National Data Buoy Center. Keyless, and a different shelf from
the `surf` agent here (Open-Meteo models waves at any point on Earth; NDBC is the buoy that
is physically in that stretch of water throwing numbers home).

Deterministic on purpose: routing is rules, the numbers are the buoy's own, a missing reading
is reported as missing (NDBC writes `MM`, which is not zero), and a station id it cannot find
is refused by name. It reports a plan, opens one tool call per lookup, asks permission before
its first read, streams the answer, and closes the tool call with a one-line summary.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, BuoysError, FIELD_NOTES, NdbcData, station_id  # noqa: E402

HELP = (
    "I read NDBC, the US National Data Buoy Center (keyless). Ask me:\n"
    "  • what are the conditions at buoy 41025?\n"
    "  • how big are the waves at the Monterey buoy?\n"
    "  • what buoys are near Monterey?\n"
    "  • last few reports from 46042\n"
    "I report what the buoy sent, in NDBC's own units, and I say 'not reported' when a sensor "
    "is down rather than inventing a zero."
)

PERMISSION_KEY = "buoys-read-public-ndbc"

SKILLS = ("buoy-conditions", "buoy-find", "buoy-recent", "help")

_FIND_WORDS = re.compile(
    r"\b(which|what|list|show|find|near|nearby|closest|stations?|buoys?)\b.{0,40}"
    r"\b(buoys?|stations?|list|near|nearby|off|around)\b", re.IGNORECASE)
_FIND_ONLY = re.compile(r"\b(which buoys|what buoys|list buoys|list stations|all stations|"
                        r"which stations|what stations|find buoys|find stations)\b", re.IGNORECASE)
_RECENT_WORDS = re.compile(r"\b(last few|past few|recent|latest few|history|trend|series|"
                           r"last \d+)\b", re.IGNORECASE)
_CONDITION_WORDS = re.compile(r"\b(conditions?|waves?|wave height|swell|wind|gust|water temp|"
                              r"sea temp|pressure|how are|how big|how rough)\b", re.IGNORECASE)

_TRAILING_NOISE = (" right now", " now", " today", " at the moment", " currently")

#: 16-point compass, so 292 becomes 'WNW' and a reader does not have to think in degrees.
_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(degrees) -> str | None:
    """Degrees true to a 16-point compass label: 292 -> 'WNW'. None for a missing value."""
    try:
        value = float(degrees)
    except (TypeError, ValueError):
        return None
    return _COMPASS[int(value % 360 / 22.5 + 0.5) % 16]


def station_from_text(text: str) -> str | None:
    """A station id written in a sentence, or None."""
    try:
        return station_id(text)
    except ValueError:
        return None


def place_from_text(text: str) -> str | None:
    """The place or buoy name a question is about, or None."""
    work = str(text).strip()
    for pattern in (
        r"\b(?:at|from|for|off|near|by)\s+(?:the\s+)?(.+?)(?=\s+(?:buoy|station|right|now|today)\b|[?.!,]|$)",
        r"\b(?:buoy|station)\s+(.+?)(?=\s+(?:right|now|today)\b|[?.!,]|$)",
        r"\bhow\s+(?:big|rough|high)\s+(?:are\s+)?(?:the\s+)?waves?\s+(?:at|near|off)\s+(.+?)(?=[?.!,]|$)",
    ):
        match = re.search(pattern, work, re.IGNORECASE)
        if match:
            phrase = match.group(1).strip(" .,?!")
            phrase = re.sub(r"^(the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
            for noise in _TRAILING_NOISE:
                if phrase.lower().endswith(noise):
                    phrase = phrase[: -len(noise)]
            words = [word for word in re.findall(r"[A-Za-z][\w.'-]*", phrase)
                     if word.lower() not in {"buoy", "station", "the", "a", "an", "report", "conditions"}]
            if words:
                return " ".join(words[:4])
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    station = station_from_text(text)
    if station:
        params["station"] = station
    place = place_from_text(text)
    if place:
        params["query"] = place

    if _FIND_ONLY.search(text) or (_FIND_WORDS.search(text) and not station):
        return "buoy-find", params
    if _RECENT_WORDS.search(text):
        return "buoy-recent", params
    if station or place:
        return "buoy-conditions", params
    if _CONDITION_WORDS.search(text):
        return "buoy-conditions", params
    return "help", {}


def _number(value, digits: int = 1) -> str:
    """Trim the padding without eating a real digit: 11.0 -> '11', but 20 degrees stays '20'."""
    if value is None:
        return "not reported"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class BuoysAgent(AcpAgent):
    name = "buoys"
    title = "Buoys — NDBC sea and wave reports"
    version = "1.0.0"

    def __init__(self, connection=None, data: NdbcData | None = None) -> None:
        super().__init__(connection)
        self.data = data or NdbcData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Buoys session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Find the NDBC station", "medium"),
            ("Read its realtime feed and report the readings", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read one product: NDBC's station list and its realtime buoy files. "
                        "Times are UTC and the units are the buoy's own.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read NDBC {skill.removeprefix('buoy-')}", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Buoys to read NDBC's public marine feeds?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public NDBC feeds before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (BuoysError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the NDBC feed: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "buoy-conditions":
            return self._conditions(params)
        if skill == "buoy-find":
            return self._find(params)
        if skill == "buoy-recent":
            return self._recent(params)
        return HELP, {"summary": "help", "dataset": None}

    def _station_or_ask(self, params: dict) -> tuple[str | None, str | None, tuple[str, dict] | None]:
        """(station id, how it was chosen, None) or (None, None, an answer to send instead)."""
        if params.get("station"):
            try:
                return station_id(params["station"]), "id", None
            except ValueError as exc:
                return None, None, (f"{exc}.", {"summary": "bad station", "dataset": DATASET})
        if params.get("query"):
            matches = self.data.find_stations(params["query"])
            if matches:
                top = matches[0]
                return top["id"], f"named {top['name']}", None
            return None, None, (
                f"I found no NDBC station matching {params['query']!r}. Try a station id like "
                "41025, or a place the buoys are named after, like Monterey or Diamond Shoals.",
                {"summary": "no station matched", "dataset": DATASET})
        return None, None, (
            "I need a station: an id like 41025, or a place, like the Monterey buoy.",
            {"summary": "no station given", "dataset": DATASET})

    def _conditions(self, params: dict) -> tuple[str, dict]:
        station, how, ask = self._station_or_ask(params)
        if ask:
            return ask
        result = self.data.conditions(station)
        info, readings, units = result["station"], result["readings"], result["units"]

        wave = readings.get("WVHT")
        period = readings.get("DPD")
        wind = readings.get("WSPD")
        gust = readings.get("GST")
        direction = readings.get("WDIR")
        artifact = {
            "summary": f"{DATASET}: {info['name']} at {result['time_utc']}, wave height "
                       f"{_number(wave)} m, wind {_number(wind)} m/s",
            "dataset": DATASET,
            "station": info,
            "time_utc": result["time_utc"],
            "readings": readings,
            "units": units,
        }
        lines = [f"{info['name']} ({info['id']}, {info['type']}), report at {result['time_utc']}"
                 + (f" - found by {how}" if how and how != "id" else "") + ":"]
        if wave is not None:
            period_text = f" with a dominant period of {_number(period)} s" if period else ""
            lines.append(f"  • Waves: {_number(wave)} m significant height{period_text}")
        else:
            lines.append("  • Waves: not reported by this station")
        if wind is not None:
            arrow = compass(direction)
            lines.append(f"  • Wind: {_number(wind)} m/s"
                         + (f" from {arrow} ({_number(direction, 0)} degrees)" if arrow else "")
                         + (f", gusting {_number(gust)} m/s" if gust is not None else ""))
        else:
            lines.append("  • Wind: not reported by this station")
        water = readings.get("WTMP")
        air = readings.get("ATMP")
        lines.append(f"  • Water temperature: " + (f"{_number(water)} {units.get('WTMP') or 'degC'}"
                                                   if water is not None else "not reported"))
        lines.append(f"  • Air temperature: " + (f"{_number(air)} {units.get('ATMP') or 'degC'}"
                                                 if air is not None else "not reported"))
        pressure = readings.get("PRES")
        if pressure is not None:
            lines.append(f"  • Pressure: {_number(pressure)} hPa")
        lines.append("\nSource: " + DATASET + ", realtime station file, read live. Times are UTC. "
                     "Significant wave height is the average of the highest third of the waves, "
                     "not the biggest one; 'not reported' means the sensor sent nothing (NDBC "
                     "writes MM, which is not a zero).")
        return "\n".join(lines), artifact

    def _find(self, params: dict) -> tuple[str, dict]:
        query = params.get("query") or params.get("station") or ""
        matches = self.data.find_stations(query, limit=8) if query else self.data.stations()[:8]
        artifact = {
            "summary": f"{DATASET}: {len(matches)} station(s) matching {query!r}"
                       if query else f"{DATASET}: first {len(matches)} active stations",
            "dataset": DATASET,
            "query": query,
            "stations": matches,
        }
        if not matches:
            return (f"No NDBC station matches {query!r}. Try a place name like Monterey, or a "
                    "station id like 41025.", {"summary": "no station matched", "dataset": DATASET})
        lines = [f"NDBC stations matching {query!r}:" if query else "Some active NDBC stations:"]
        for row in matches[:8]:
            where = ""
            if row.get("latitude") is not None and row.get("longitude") is not None:
                where = f" at {row['latitude']:.3f}, {row['longitude']:.3f}"
            lines.append(f"  • {row['id']} - {row['name']} [{row['type']}]{where}")
        lines.append("\nAsk for the conditions at any of those ids. "
                     f"Source: {DATASET}, station list, read live.")
        return "\n".join(lines), artifact

    def _recent(self, params: dict) -> tuple[str, dict]:
        station, how, ask = self._station_or_ask(params)
        if ask:
            return ask
        rows = 6
        result = self.data.latest(station, rows=rows)
        info = self.data.station(station)
        artifact = {
            "summary": f"{DATASET}: last {len(result['reports'])} reports from {info['name']}",
            "dataset": DATASET,
            "station": info,
            "reports": result["reports"],
        }
        lines = [f"The last {len(result['reports'])} reports from {info['name']} ({station}"
                 + (f", found by {how}" if how and how != "id" else "") + "):"]
        for report in result["reports"]:
            values = report["values"]
            parts = [f"waves {_number(values.get('WVHT'))} m",
                     f"wind {_number(values.get('WSPD'))} m/s",
                     f"water {_number(values.get('WTMP'))} degC"]
            pressure = values.get("PRES")
            if pressure is not None:
                parts.append(f"pressure {_number(pressure)} hPa")
            lines.append(f"  • {report['time_utc']}: " + ", ".join(parts))
        lines.append("\nSource: " + DATASET + ", realtime station file, read live. "
                     "Newest first, all times UTC.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        BuoysAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
