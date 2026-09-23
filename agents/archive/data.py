"""What the Internet Archive holds, from its own two keyless JSON APIs.

Verified live on 2026-09-23 (no key):

  https://archive.org/advancedsearch.php?q=title:(apollo+11)&fl[]=identifier&output=json
  https://archive.org/metadata/MisharyRasyidPerJuz

Two details the payloads make plain and this reader keeps. Search returns a `numFound` that is
the whole archive's count (1,626 items for "apollo 11"), so the return says how many exist and
not just the handful shown. And an item's `files` are mostly derivatives - torrents, thumbnails,
Columbia Peaks - so the file list keeps the files a person can actually open and says how many
were left out rather than pretending the item is small.
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib import parse, request

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/"

DATASET = "Internet Archive (archive.org advancedsearch.php + /metadata)"

DEFAULT_USER_AGENT = "awesome-acps-archive/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: The archive's own mediatype names, and the words people use for them.
MEDIATYPES = {
    "audio": ("audio", "song", "songs", "music", "recording", "recordings", "album",
              "concert", "radio", "podcast"),
    "movies": ("video", "videos", "film", "films", "movie", "movies", "tv", "television",
               "newsreel", "footage"),
    "texts": ("book", "books", "text", "texts", "paper", "papers", "document", "documents",
              "manual", "magazine", "magazines", "journal"),
    "image": ("image", "images", "photo", "photos", "picture", "pictures", "poster",
              "posters", "map", "maps"),
    #: No "game" here on purpose: "who won the game?" is sports talk, and a mediatype filter of
    #: software would be a confident wrong answer. A search without the filter still finds games.
    "software": ("software", "program", "programs", "app", "apps", "emulator", "rom",
                 "roms"),
    "data": ("dataset", "datasets", "data", "csv"),
    "etree": ("concert recording", "live music"),
}

#: Formats that are a person opening the file, not machinery.
PLAYABLE = ("VBR MP3", "MP3", "64Kbps MP3", "128Kbps MP3", "Ogg Vorbis", "Flac", "FLAC",
            "WAVE", "AIFF", "Apple Lossless Audio", "MPEG4", "h.264", "h.264 IA", "512Kb MPEG4",
            "Ogg Video", "WebM", "Matroska", "DivX", "MPEG2", "MPEG1", "Windows Media",
            "Text PDF", "Image Container PDF", "EPUB", "Kindle", "Full Text", "DjVuTXT",
            "Abbyy GZ", "PNG", "JPEG", "JPEG 2000", "GIF", "TIFF", "SVG", "Single Page Processed JP2 ZIP")

#: Formats that exist for the archive's own machinery.
MACHINERY = ("Metadata", "Columbia Peaks", "Archive BitTorrent", "Item Tile", "Thumbnail",
             "JPEG Thumb", "JSON", "XML", "ZIP", "Spectrogram", "Essentia", "TOC", "Log",
             "Minified JSON", "Word Coordinates", "Djvu XML", "OCR Page Index")


class ArchiveError(RuntimeError):
    """The Internet Archive could not be read."""


def mediatype_from_text(text: str) -> tuple[str | None, str]:
    """(mediatype, the word that named it) - the longest word wins.

    Returning the word as well lets the caller strip only that word from the title search,
    so "old radio dramas" keeps 'radio' (it is mid-phrase and carries meaning) while
    "Apollo 11 recordings" drops the trailing 'recordings' that would break the match.
    """
    lowered = str(text or "").lower()
    best_word, best_type = "", None
    for mediatype, words in MEDIATYPES.items():
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", lowered) and len(word) > len(best_word):
                best_word, best_type = word, mediatype
    return best_type, best_word


def human_size(size) -> str:
    """Bytes as something a person reads."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def plain_text(html: str) -> str:
    """Item descriptions arrive as HTML ('<br /><br />Digitized by NASA.'). Strip the tags."""
    text = re.sub(r"<[^>]{1,80}>", " ", str(html or ""))
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return re.sub(r"\s{2,}", " ", text).strip()


def playable_file(entry: dict) -> bool:
    """True when this file is content rather than the archive's machinery."""
    name = str(entry.get("name") or "")
    fmt = str(entry.get("format") or "")
    if not name or name.startswith("__ia_thumb"):
        return False
    if any(marker in fmt for marker in MACHINERY):
        return False
    if fmt in PLAYABLE:
        return True
    # Unknown format: keep it unless it is plainly a sidecar.
    return not name.endswith((".xml", ".json", ".torrent", ".sqlite", ".gz", ".ffp", ".md5",
                              ".cue", ".log", ".asc", ".txt" ))


class ArchiveData:
    """Search the archive, and look inside one item."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 30.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("ARCHIVE_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("ARCHIVE_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("ARCHIVE_USER_AGENT")
                           or DEFAULT_USER_AGENT)
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict | None = None) -> str:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise ArchiveError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        now = time.time()
        hit = self._cache.get(query)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise ArchiveError(
                    f"the archive returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ArchiveError("the archive returned an unexpected payload")
        self._cache[query] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- reads -------------------------------------------------------------

    def search(self, query: str, mediatype: str | None = None, limit: int = 5,
               sort: str = "downloads desc") -> dict:
        """Items matching the query, most downloaded first."""
        text = str(query or "").strip()
        if not text:
            raise ValueError("tell me what to look for, for example Apollo 11 recordings")
        wanted = max(1, min(int(limit), 25))
        q = f'title:({text})'
        if mediatype:
            q = f'({q} OR description:({text})) AND mediatype:({mediatype})'
        params = [("q", q), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "mediatype"),
                  ("fl[]", "downloads"), ("fl[]", "year"), ("fl[]", "creator"),
                  ("rows", str(wanted)), ("page", "1"), ("output", "json"),
                  ("sort[]", sort)]
        payload = self._json(SEARCH_URL, params, ttl=1800.0)
        response = payload.get("response") or {}
        docs = response.get("docs") or []
        if not docs:
            raise ArchiveError(f"nothing in the archive matches {text!r}")
        rows = []
        for doc in docs:
            creator = doc.get("creator")
            if isinstance(creator, list):
                creator = creator[0] if creator else None
            rows.append({
                "identifier": doc.get("identifier"),
                "title": str(doc.get("title") or doc.get("identifier") or "(untitled)"),
                "mediatype": doc.get("mediatype"),
                "year": doc.get("year"),
                "creator": creator,
                "downloads": doc.get("downloads"),
                "url": f"https://archive.org/details/{doc.get('identifier')}",
            })
        return {"query": text, "mediatype": mediatype, "rows": rows,
                "total": response.get("numFound"), "source": DATASET,
                "sort": "most downloaded first" if "downloads" in sort else sort}

    def item(self, identifier: str, limit: int = 8) -> dict:
        """One item: who made it, when, and the files a person can open."""
        name = str(identifier or "").strip()
        if not name:
            raise ValueError("tell me which item, for example the identifier Apollo11Audio")
        if "archive.org/details/" in name:
            name = name.split("archive.org/details/", 1)[1].strip("/")
        payload = self._json(METADATA_URL + parse.quote(name), ttl=3600.0)
        metadata = payload.get("metadata") or {}
        if not metadata:
            raise ArchiveError(f"the archive has no item called {name!r}")
        creator = metadata.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(str(part) for part in creator)
        files = payload.get("files") or []
        content = [entry for entry in files if playable_file(entry)]
        total = sum(float(entry.get("size") or 0) for entry in content)
        wanted = max(1, min(int(limit), 40))
        description = plain_text(metadata.get("description"))
        when = metadata.get("date") or metadata.get("publicdate") or metadata.get("addeddate")
        return {
            "identifier": metadata.get("identifier") or name,
            "title": str(metadata.get("title") or name),
            "creator": creator,
            "date": str(when)[:10] if when else None,
            "mediatype": metadata.get("mediatype"),
            "collection": metadata.get("collection"),
            "subject": metadata.get("subject"),
            "description": description,
            "rows": [{"name": str(entry.get("name")), "format": entry.get("format"),
                      "size": entry.get("size"), "size_text": human_size(entry.get("size"))}
                     for entry in content[:wanted]],
            "files_total": len(files), "files_kept": len(content),
            "files_skipped": len(files) - len(content),
            "total_size": total, "total_size_text": human_size(total),
            "url": f"https://archive.org/details/{metadata.get('identifier') or name}",
            "source": DATASET,
        }
