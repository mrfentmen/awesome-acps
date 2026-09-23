"""NASA's Astronomy Picture of the Day - the picture itself, not a link to it.

Verified live on 2026-09-23:

  https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY&thumbs=true       -> one day
  https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY&count=3           -> a JSON list
  https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY&date=2026-09-01   -> one named day
  https://api.nasa.gov/planetary/apod?api_key=NOPE                       -> 403 API_KEY_INVALID

Four things this reader states rather than hides:

  * `DEMO_KEY` is what it uses when nobody sets `NASA_API_KEY`, and NASA rate-limits that key by
    IP address (30 requests an hour, 50 a day). A 429 is reported as a rate limit, not as an
    empty sky.
  * Some days are **videos**, not pictures. Those days have no image to send, so the thumbnail
    NASA publishes for them is sent instead and the answer says that is what it is.
  * An image is only attached when it is under `MAX_IMAGE_BYTES`. A 40 MB Hubble mosaic is not
    going to arrive base64-encoded inside a chat message, and a truncated one would be worse
    than a link.
  * The explanation is NASA's own text, printed whole with the day's copyright line, because the
    text is the part a person cannot get from the picture.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from urllib import error, parse, request

APOD = "https://api.nasa.gov/planetary/apod"
PAGE = "https://apod.nasa.gov/apod/ap{stamp}.html"

DATASET = "NASA Astronomy Picture of the Day (api.nasa.gov/planetary/apod)"

#: NASA's shared demo key. It works, and it is rate-limited per IP address.
DEMO_KEY = "DEMO_KEY"

#: An image bigger than this is linked, not attached: the answer says which and why.
MAX_IMAGE_BYTES = 4_000_000

#: How many random pictures one answer will carry.
MAX_RANDOM = 5

DEFAULT_USER_AGENT = "awesome-acps-apod/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: What NASA calls the media it publishes.
IMAGE_TYPES = ("image", "video")


class ApodError(RuntimeError):
    """The picture could not be read."""


def page_url(date: str) -> str:
    """APOD's own page for a day: ap260923.html for 2026-09-23."""
    try:
        when = datetime.date.fromisoformat(str(date)[:10])
    except ValueError:
        return "https://apod.nasa.gov/apod/astropix.html"
    return PAGE.format(stamp=when.strftime("%y%m%d"))


def iso_date(value: str | datetime.date | None) -> str:
    """A date as APOD writes it, or today when nothing usable was given."""
    if isinstance(value, datetime.date):
        return value.isoformat()
    try:
        return datetime.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return datetime.date.today().isoformat()


def clean_text(text: str) -> str:
    """NASA's prose with its line breaks and double spaces tidied, never cut."""
    joined = re.sub(r"\s+", " ", str(text or "")).strip()
    return joined


def credit(copyright: str | None) -> str:
    """The day's copyright as a line a person can read, or NASA's own default."""
    if not copyright:
        return "NASA / public domain (no separate copyright line on this day)"
    who = re.sub(r"\s+", " ", str(copyright)).strip()
    return who if who.lower().startswith(("copyright", "©")) else f"copyright {who}"


class ApodData:
    """One day's picture, a few random ones, and the image bytes behind a url."""

    def __init__(self, fetch=None, fetch_bytes=None, key: str | None = None,
                 timeout: float = 30.0, user_agent: str | None = None) -> None:
        self.key = key or os.environ.get("NASA_API_KEY") or DEMO_KEY
        self.timeout = float(os.environ.get("APOD_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("APOD_USER_AGENT") or DEFAULT_USER_AGENT
        self.fetch = fetch or self._http
        self.fetch_bytes = fetch_bytes or self._http_bytes

    # -- transport ---------------------------------------------------------

    def _open(self, url: str):
        req = request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            return request.urlopen(req, timeout=self.timeout)
        except error.HTTPError as exc:
            raise ApodError(self._say(exc)) from exc
        except error.URLError as exc:
            raise ApodError(f"NASA is unreachable ({exc.reason})") from exc
        except TimeoutError as exc:
            raise ApodError(f"NASA did not answer within {self.timeout:.0f}s") from exc

    def _say(self, exc: "error.HTTPError") -> str:
        """NASA's refusal in words, including the one that is about the key, not the sky."""
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            found = re.search(r'"message"\s*:\s*"([^"]+)"', body)
            detail = found.group(1) if found else ""
        except Exception:  # noqa: BLE001 - a missing body must not hide the status
            detail = ""
        if exc.code == 429:
            return (f"NASA answered 429: the key {self.key} is over its rate limit for this "
                    f"address (DEMO_KEY allows 30 requests an hour, 50 a day). {detail}".strip())
        if exc.code in (401, 403):
            return (f"NASA answered {exc.code}: it refused the key {self.key}. {detail} "
                    "Set NASA_API_KEY to a key from api.nasa.gov").strip()
        return f"NASA answered {exc.code} for {exc.url}. {detail}".strip()

    def _http(self, url: str) -> str:
        with self._open(url) as reply:
            return reply.read().decode("utf-8", errors="replace")

    def _http_bytes(self, url: str) -> bytes:
        with self._open(url) as reply:
            length = reply.headers.get("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ApodError(f"the image at {url} is {int(length) / 1_000_000:.1f} MB, over the "
                                f"{MAX_IMAGE_BYTES / 1_000_000:.0f} MB this agent will attach")
            return reply.read(MAX_IMAGE_BYTES + 1)

    # -- the feed ----------------------------------------------------------

    def url_for(self, date: str | None = None, count: int | None = None) -> str:
        params = {"api_key": self.key, "thumbs": "true"}
        if count:
            params["count"] = str(max(1, min(int(count), MAX_RANDOM)))
        elif date:
            params["date"] = iso_date(date)
        return f"{APOD}?{parse.urlencode(params)}"

    def day(self, date: str | None = None) -> dict:
        """One day's entry, with the media type and the credit named."""
        payload = self.fetch(self.url_for(date=date if date else None))
        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise ApodError(f"NASA's answer was not JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise ApodError("NASA answered with a list where one day was expected")
        return self._one(raw)

    def random(self, count: int = 1) -> list[dict]:
        """A few random days. NASA sends a list, and repeats are possible across calls."""
        payload = self.fetch(self.url_for(count=count))
        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise ApodError(f"NASA's answer was not JSON ({exc})") from exc
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ApodError("NASA answered with no pictures for that request")
        return [self._one(entry) for entry in raw if isinstance(entry, dict)]

    @staticmethod
    def _one(raw: dict) -> dict:
        media = str(raw.get("media_type") or "image").lower()
        picture = str(raw.get("url") or "")
        thumbnail = str(raw.get("thumbnail_url") or "")
        return {
            "date": iso_date(raw.get("date")),
            "title": clean_text(raw.get("title")) or "(NASA sent no title)",
            "explanation": clean_text(raw.get("explanation")),
            "media_type": media if media in IMAGE_TYPES else "other",
            "url": picture,
            "hdurl": str(raw.get("hdurl") or ""),
            "thumbnail": thumbnail,
            "credit": credit(raw.get("copyright")),
            "page": page_url(raw.get("date")),
            "service_version": str(raw.get("service_version") or ""),
            # The still image to attach, if this day has one. A video day has a thumbnail
            # instead, and the answer says so rather than pretending it is the picture.
            "image_url": picture if media == "image" else (thumbnail or ""),
            "image_kind": ("the picture" if media == "image"
                           else "NASA's thumbnail for the video" if thumbnail
                           else ""),
        }

    def attachable(self, entry: dict) -> tuple[bytes, str] | None:
        """The bytes for an entry, or None when there is nothing this agent will attach."""
        url = entry.get("image_url")
        if not url:
            return None
        return self.fetch_bytes(url), mime_for(url)


def mime_for(url: str) -> str:
    """The image type from the file name, defaulting to JPEG because APOD is nearly all JPEG."""
    path = str(url or "").lower().split("?")[0]
    for ending, mime in ((".png", "image/png"), (".gif", "image/gif"), (".webp", "image/webp"),
                         (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".tif", "image/tiff")):
        if path.endswith(ending):
            return mime
    return "image/jpeg"


def rate_line() -> str:
    """The one sentence every answer carries about the key it used."""
    return ("NASA's DEMO_KEY is rate-limited by address (30 an hour, 50 a day); set "
            "NASA_API_KEY for more")
