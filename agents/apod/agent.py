"""APOD - NASA's picture of the day, sent as a picture.

`chart` proved an ACP agent can answer with an image block; this is the second one, and it is the
first agent in any editor that answers "show me NASA's picture of the day" with the picture
attached rather than a link to it. The bytes are fetched from apod.nasa.gov, sent as an
`image` content block, and the day's own title, credit and NASA's explanation go with it.

Three days out of the week NASA publishes a video instead of a picture. Those answers send the
thumbnail NASA publishes with a line saying that is what it is, and never claim a video frame
was the day's photograph.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    DATASET,
    DEMO_KEY,
    MAX_IMAGE_BYTES,
    MAX_RANDOM,
    ApodData,
    ApodError,
    rate_line,
)

HELP = (
    "I read NASA's Astronomy Picture of the Day (API key: DEMO_KEY unless you set NASA_API_KEY) "
    "and I send the picture, not a link. Ask me:\n"
    "  - show me NASA's picture of the day\n"
    "  - what is today's astronomy picture?\n"
    "  - the apod for 1996-01-01\n"
    "  - NASA's picture for July 20 1996\n"
    "  - show me 3 random space pictures\n"
    "Each answer carries the day's title, its credit line and NASA's own explanation. Days NASA "
    "published as a video are sent as their thumbnail, and the answer says so. The archive starts "
    "on 1995-06-16, so a day before that has no picture to show."
)

PERMISSION_KEY = "apod-read-nasa"

SKILLS = ("apod-picture", "apod-random", "apod-nodate", "apod-help")

#: One image per answer, so a random set does not arrive as ten megabytes.
ATTACH_LIMIT = 1

_RANDOM = re.compile(r"\b(random|any|some|surprise|shuffle|pick)\b", re.IGNORECASE)
_COUNT = re.compile(r"\b(\d{1,2})\b")
_TODAY = re.compile(r"\b(today|tonight|current|latest|now)\b", re.IGNORECASE)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_SLASH = re.compile(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b")
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"), start=1)}
_MONTH_DAY_YEAR = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE)
_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r"),?\s+(\d{4})\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
_PICTURE = re.compile(r"\b(picture|photo|image|apod|astronomy picture|space picture)\b",
                      re.IGNORECASE)

#: APOD started on 1995-06-16, and asking for a day before that is a question with no answer.
FIRST_DAY = datetime.date(1995, 6, 16)


def date_or_none(year: int, month: int, day: int) -> datetime.date | None:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def named_date(text: str, today: datetime.date | None = None) -> datetime.date | None:
    """The day a question names, whatever it is - including days APOD cannot answer for."""
    work = str(text or "").strip()
    for pattern in (_DATE_ISO, _DATE_SLASH):
        found = pattern.search(work)
        if found:
            year, month, day = (int(bit) for bit in found.groups())
            return date_or_none(year, month, day)
    found = _MONTH_DAY_YEAR.search(work)
    if found:
        # "July 20 1969": the month is named first, the day is second.
        return date_or_none(int(found.group(3)), _MONTHS.get(found.group(1).lower(), 0),
                            int(found.group(2)))
    found = _DAY_MONTH_YEAR.search(work)
    if found:
        # "20 July 1969": the day comes first, the month is second.
        return date_or_none(int(found.group(3)), _MONTHS.get(found.group(2).lower(), 0),
                            int(found.group(1)))
    if _YESTERDAY.search(work):
        return (today or datetime.date.today()) - datetime.timedelta(days=1)
    return None


def date_from_text(text: str, today: datetime.date | None = None) -> str | None:
    """A day APOD can answer for, or None. A day it cannot is reported, never swapped."""
    when = named_date(text, today)
    if when is None:
        return None
    return when.isoformat() if not date_problem(text, today) else None


def date_problem(text: str, today: datetime.date | None = None) -> str | None:
    """Why the day a question names cannot be shown, or None when it can."""
    work = str(text or "").strip()
    when = named_date(work, today)
    if when is None:
        for pattern in (_DATE_ISO, _DATE_SLASH, _MONTH_DAY_YEAR, _DAY_MONTH_YEAR):
            found = pattern.search(work)
            if found:
                # A date was named and does not exist as a date - 'February 30' is not a day,
                # and answering with some other day's picture would be a swap, not an answer.
                return (f"{found.group(0).strip()} is not a day on the calendar, and "
                        f"{found.group(0).strip()} has no picture")
        return None
    if when > (today or datetime.date.today()):
        return f"{when.isoformat()} is in the future"
    if when < FIRST_DAY:
        return f"APOD's first picture is {FIRST_DAY.isoformat()}, so there is no picture for " \
               f"{when.isoformat()}"
    return None


def count_from_text(text: str) -> int:
    """How many random pictures were asked for, within what this agent will send."""
    wanted = _COUNT.search(str(text or ""))
    if not wanted:
        return 1
    return max(1, min(int(wanted.group(1)), MAX_RANDOM))


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "apod-help", {}
    params: dict = {}
    if _RANDOM.search(stripped):
        params["count"] = count_from_text(stripped)
        return "apod-random", params
    problem = date_problem(stripped)
    if problem:
        return "apod-nodate", {"why": problem}
    date = date_from_text(stripped)
    if date:
        params["date"] = date
        return "apod-picture", params
    if _TODAY.search(stripped) or _PICTURE.search(stripped):
        return "apod-picture", params
    return "apod-help", {}


def render(entry: dict) -> str:
    """The day's card: what it is, who gets the credit, and NASA's text whole."""
    lines = [f"{entry['date']} - {entry['title']}"]
    lines.append(f"  - media: {entry['media_type']}")
    lines.append(f"  - {entry['credit']}")
    if entry["media_type"] != "image":
        lines.append(f"  - NASA published this day as a {entry['media_type']}: {entry['url']}")
    lines.append(f"  - APOD's own page for this day: {entry['page']}")
    if entry["hdurl"] and entry["hdurl"] != entry["url"]:
        lines.append(f"  - full resolution: {entry['hdurl']}")
    return "\n".join(lines)


class ApodAgent(AcpAgent):
    name = "apod"
    title = "APOD - NASA's picture of the day, sent as a picture"
    version = "1.0.0"

    def __init__(self, connection=None, data: ApodData | None = None) -> None:
        super().__init__(connection)
        self.data = data or ApodData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "APOD session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read NASA's picture of the day", "medium"),
            ("Send the picture itself, then NASA's words with the credit", "medium"),
        ])

        if skill == "apod-help":
            ctx.stream_text(HELP)
            ctx.message("\n" + rate_line() + ".")
            return STOP_END_TURN

        if skill == "apod-nodate":
            # A day with no picture is answered with the reason, never with a different day's
            # picture dressed up as the one that was asked for.
            ctx.stream_text(f"I cannot show that one: {params['why']}.\n"
                            f"  - the whole archive is at "
                            f"https://apod.nasa.gov/apod/archivepix.html\n"
                            f"  - a day I can show looks like 'the apod for 1996-01-01'")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        asking = (f"{params['count']} random space pictures" if skill == "apod-random"
                  else f"the picture for {params.get('date') or 'today'}")
        ctx.tool_call(tool, f"Read NASA for {asking}", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow APOD to read and send NASA's picture?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read NASA's picture of the day first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "apod-random":
                entries = self.data.random(count=params.get("count") or 1)
            else:
                entries = [self.data.day(date=params.get("date"))]
        except ApodError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that: {exc}")
            return STOP_END_TURN

        sent, linked, sizes = 0, [], []
        for entry in entries:
            ctx.stream_text(render(entry) + "\n")
            attached = False
            if sent < ATTACH_LIMIT:
                try:
                    payload = self.data.attachable(entry)
                except ApodError as exc:
                    linked.append(f"{entry['date']}: {exc}")
                    payload = None
                if payload:
                    blob, mime = payload
                    ctx.image(blob, mime)
                    sizes.append(f"{len(blob) / 1000:.0f} KB {mime.split('/')[-1]}")
                    attached = True
                    sent += 1
            if not attached:
                linked.append(f"{entry['date']}: {entry['image_url'] or entry['url']}")
            if entry["explanation"]:
                ctx.message(f"\nNASA's explanation for {entry['date']}:\n{entry['explanation']}")
            if entry["media_type"] != "image" and attached:
                ctx.message(f"\nThat picture is {entry['image_kind']}: NASA published a "
                            f"{entry['media_type']} for {entry['date']}, and the link above is the "
                            f"video itself.")

        if sent:
            done = f"sent {sizes[0]}"
        elif linked:
            done = "linked instead of attached"
        else:
            done = "nothing to attach"
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(done))
        if linked:
            ctx.message("\nNot attached (a link is above or here):\n  - "
                        + "\n  - ".join(linked))
        note = (f"\nNothing left this machine except the request to NASA. Images over "
                f"{MAX_IMAGE_BYTES / 1_000_000:.0f} MB are linked rather than attached, and the "
                f"text above is NASA's own, printed whole.")
        if self.data.key == DEMO_KEY:
            note += f" {rate_line()}."
        ctx.message(note)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ApodAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
