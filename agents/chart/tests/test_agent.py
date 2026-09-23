"""Tests for the chart agent: the PNG writer, the series windows, routing and turns.

    python3 agents/chart/tests/test_agent.py

No network: every series is injected, shaped like the live payloads verified on 2026-09-23.
The image is checked structurally - signature, IHDR size, and the number of distinct colours in
the inflated pixels - because a chart that renders to a blank rectangle is a passing test that
helps nobody.
"""

from __future__ import annotations

import base64
import collections
import datetime
import json
import queue
import struct
import sys
import threading
import unittest
import zlib
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

import render  # noqa: E402
from agent import place_from_text, route  # noqa: E402
from data import (  # noqa: E402
    ChartData,
    ChartError,
    local_now,
    sample,
)

PLACE = {"results": [{"name": "Seattle", "latitude": 47.6062, "longitude": -122.3321,
                      "country": "United States", "admin1": "Washington",
                      "timezone": "America/Los_Angeles"}]}

OFFSET = -7 * 3600  # Seattle in September

#: The fixture starts three hours before the place's now, so the first hour a reader keeps is
#: always the fourth one - no test here depends on how many seconds past the hour it is run.
LEAD_HOURS = 3


def hourly_payload(hours: int = 40, temperature=15.0, rain=0.0, aqi=None, offset=OFFSET):
    """An Open-Meteo payload leading up to the place's current hour, like the live one."""
    start = (local_now(offset).replace(minute=0, second=0, microsecond=0)
             - datetime.timedelta(hours=LEAD_HOURS))
    times = [(start + datetime.timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")
             for index in range(hours)]
    body = {"hourly": {"time": times,
                       "temperature_2m": [temperature + index for index in range(hours)],
                       "precipitation": [rain] * hours},
            "utc_offset_seconds": offset}
    if aqi is not None:
        series = [50.0] * LEAD_HOURS + list(aqi)
        body["hourly"]["us_aqi"] = series + [50.0] * (hours - len(series))
    return body


def window_start(payload: dict) -> int:
    """The index the reader starts from: the first hour of the place's current hour."""
    now = local_now(payload["utc_offset_seconds"]).strftime("%Y-%m-%dT%H:00")
    for index, stamp in enumerate(payload["hourly"]["time"]):
        if stamp >= now:
            return index
    return 0


TREASURY = {"data": [
    {"record_date": "2026-09-21", "tot_pub_debt_out_amt": "40112118151111.92"},
    {"record_date": "2026-09-18", "tot_pub_debt_out_amt": "40000000000000.00"},
    {"record_date": "2026-09-17", "tot_pub_debt_out_amt": "39900000000000.00"},
]}

USGS = {"type": "FeatureCollection", "features": [
    {"properties": {"mag": 4.5, "time": None, "place": "somewhere"}},
]}


def usgs_with_events(offsets_minutes):
    now = datetime.datetime.now(datetime.timezone.utc)
    features = []
    for minutes in offsets_minutes:
        stamp = now - datetime.timedelta(minutes=minutes)
        features.append({"properties": {"mag": 3.0, "time": stamp.timestamp() * 1000,
                                        "place": "test"}})
    return {"type": "FeatureCollection", "features": features}


class FakeFeed:
    """Serves the four data shapes and records every (url, params) pair."""

    def __init__(self, fail: str | None = None, aqi=(180.0, 210.0, 160.0), temperature=15.0,
                 rain=0.0, quakes=None, treasury=None, place=PLACE, forecast_hours=40):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.forecast_hours = forecast_hours
        self.aqi = aqi
        self.temperature = temperature
        self.rain = rain
        self.quakes = quakes if quakes is not None else usgs_with_events([5, 70, 130])
        self.treasury = treasury if treasury is not None else TREASURY
        self.place = place

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail and self.fail in url:
            raise ChartError("the data source is unreachable")
        if "geocoding" in url:
            return json.dumps(self.place)
        if "air-quality" in url:
            return json.dumps(hourly_payload(hours=self.forecast_hours, aqi=self.aqi))
        if "open-meteo.com/v1/forecast" in url:
            return json.dumps(hourly_payload(hours=self.forecast_hours,
                                            temperature=self.temperature, rain=self.rain))
        if "fiscaldata" in url:
            return json.dumps(self.treasury)
        if "earthquake" in url:
            return json.dumps(self.quakes)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw) -> tuple[ChartData, FakeFeed]:
    feed = FakeFeed(**kw)
    return ChartData(fetch=feed), feed


def colour_count(blob: bytes) -> int:
    """Distinct colours in a PNG's pixels, so a blank chart cannot pass."""
    width, height = struct.unpack(">II", blob[16:24])
    offset, idat = 8, b""
    while offset < len(blob):
        length = struct.unpack(">I", blob[offset:offset + 4])[0]
        tag = blob[offset + 4:offset + 8]
        if tag == b"IDAT":
            idat += blob[offset + 8:offset + 8 + length]
        offset += 12 + length
    raw = zlib.decompress(idat)
    seen = collections.Counter()
    stride = width * 3 + 1
    for row in range(height):
        line = raw[row * stride + 1:(row + 1) * stride]
        for index in range(0, len(line), 3):
            seen[line[index:index + 3]] += 1
    return len(seen)


class RenderTests(unittest.TestCase):
    def test_png_has_a_real_header(self):
        canvas = render.Canvas(40, 20)
        canvas.rect(4, 4, 30, 12, render.SERIES)
        blob = canvas.to_png()
        self.assertEqual(blob[:8], render.PNG_SIGNATURE)
        self.assertEqual(render.png_size(blob), (40, 20))
        self.assertGreater(len(blob), 60)

    def test_a_mismatched_buffer_is_refused(self):
        with self.assertRaises(ValueError):
            render.png_bytes(10, 10, bytearray(10))

    def test_pixels_outside_the_canvas_are_ignored(self):
        canvas = render.Canvas(8, 8)
        canvas.set_pixel(-3, -3, render.INK)
        canvas.set_pixel(99, 99, render.INK)
        self.assertEqual(colour_count(canvas.to_png()), 1)

    def test_drawing_lines_and_text_changes_the_image(self):
        canvas = render.Canvas(120, 40)
        before = colour_count(canvas.to_png())
        canvas.line(0, 0, 119, 39, render.SERIES)
        canvas.text(10, 10, "AQI 213", render.INK)
        after = colour_count(canvas.to_png())
        self.assertGreater(after, before)
        self.assertGreaterEqual(after, 3)

    def test_unknown_characters_do_not_crash_the_font(self):
        canvas = render.Canvas(40, 16)
        canvas.text(0, 0, "\u00e9\u4e2d", render.INK)
        self.assertEqual(render.glyph("\u00e9"), render.glyph("?"))

    def test_format_value_reads_like_a_person_wrote_it(self):
        self.assertEqual(render.format_value(40112118151111.92), "40.1T")
        self.assertEqual(render.format_value(153.0, " AQI"), "153 AQI")
        self.assertEqual(render.format_value(3.0), "3")
        self.assertEqual(render.format_value(10.5, "\u00b0C"), "10.50\u00b0C")
        self.assertEqual(render.format_value(0.8, "mm"), "0.80mm")
        self.assertEqual(render.format_value(None), "n/a")

    def test_a_line_chart_is_a_real_picture(self):
        points = [("00:00", 12.0), ("06:00", 9.5), ("12:00", 18.25), ("18:00", 14.0)]
        blob = render.line_chart("Temperature in Seattle", "Open-Meteo, next 24 hours", points)
        self.assertEqual(render.png_size(blob), (render.WIDTH, render.HEIGHT))
        self.assertGreater(colour_count(blob), 4)

    def test_a_flat_series_still_draws(self):
        blob = render.line_chart("Flat", "one value, forty times", [("a", 5.0), ("b", 5.0)])
        self.assertEqual(render.png_size(blob), (render.WIDTH, render.HEIGHT))

    def test_a_bar_chart_is_a_real_picture(self):
        blob = render.bar_chart("Rain per hour", "Open-Meteo", [("00:00", 0.0), ("01:00", 1.2)])
        self.assertGreater(colour_count(blob), 3)

    def test_charts_refuse_what_they_cannot_draw(self):
        with self.assertRaises(ValueError):
            render.line_chart("Nothing", "no points", [])
        with self.assertRaises(ValueError):
            render.bar_chart("Negative", "counts only", [("a", -3.0)])


class SampleTests(unittest.TestCase):
    def test_a_short_series_is_untouched(self):
        labels, values, step = sample(["a", "b"], [1.0, 2.0], limit=10)
        self.assertEqual((labels, values, step), (["a", "b"], [1.0, 2.0], 1))

    def test_a_long_series_is_thinned_with_a_step_that_can_be_reported(self):
        labels = [str(index) for index in range(500)]
        values = [float(index) for index in range(500)]
        kept_labels, kept_values, step = sample(labels, values, limit=100)
        self.assertGreater(step, 1)
        self.assertLessEqual(len(kept_values), 101)
        self.assertEqual(kept_labels[0], "0")

    def test_align_last_keeps_the_real_final_point(self):
        labels = [str(index) for index in range(500)]
        values = [float(index) for index in range(500)]
        kept_labels, kept_values, _step = sample(labels, values, limit=100, align_last=True)
        self.assertEqual(kept_values[-1], values[-1])
        self.assertEqual(kept_labels[-1], labels[-1])

    def test_local_now_applies_the_offset(self):
        here = datetime.datetime.utcnow()
        there = local_now(9 * 3600)
        self.assertAlmostEqual((there - here).total_seconds(), 9 * 3600, delta=60)
        self.assertIsInstance(local_now(None), datetime.datetime)


class SeriesTests(unittest.TestCase):
    def test_geocoding_names_the_place(self):
        data, _ = make_data()
        place = data.geocode("Seattle")
        self.assertEqual(place["name"], "Seattle")
        self.assertAlmostEqual(place["latitude"], 47.6062)
        self.assertEqual(place["country"], "United States")

    def test_an_unknown_place_is_refused(self):
        data, _ = make_data(place={"results": []})
        with self.assertRaises(ValueError):
            data.geocode("Xyzzyville")
        with self.assertRaises(ValueError):
            data.geocode("")

    def test_the_temperature_window_starts_at_the_places_now(self):
        data, _ = make_data()
        series = data.series("temperature", place="Seattle", hours=24)
        payload = hourly_payload()
        start = window_start(payload)
        self.assertEqual(start, LEAD_HOURS)  # the hours before the place's now are dropped
        self.assertEqual(series["raw_times"][0], payload["hourly"]["time"][start])
        self.assertEqual(len(series["values"]), 24)
        self.assertEqual(series["values"][0], payload["hourly"]["temperature_2m"][start])
        self.assertEqual(series["chart"], "line")
        self.assertIn("Seattle", series["title"])
        self.assertIn("hourly forecast", series["subtitle"])

    def test_rain_is_a_bar_chart_of_precipitation(self):
        data, _ = make_data(rain=0.6)
        series = data.series("rain", place="Tokyo", hours=12)
        self.assertEqual(series["chart"], "bar")
        self.assertEqual(series["unit"], "mm")
        self.assertEqual(series["values"][0], 0.6)

    def test_air_quality_reads_us_aqi(self):
        data, _ = make_data(aqi=(180.0, 210.0, 160.0))
        series = data.series("air", place="Delhi", hours=3)
        self.assertEqual(series["values"], [180.0, 210.0, 160.0])
        self.assertIn("US AQI", series["note"])

    def test_a_missing_aqi_hour_becomes_zero(self):
        data, _ = make_data(aqi=(None, 40.0))
        series = data.series("air", place="Delhi", hours=2)
        self.assertEqual(series["values"], [0.0, 40.0])

    def test_debt_is_oldest_first_and_in_dollars(self):
        data, _ = make_data()
        series = data.series("debt", days=3)
        self.assertEqual(series["values"][0], 39900000000000.0)
        self.assertEqual(series["values"][-1], 40112118151111.92)
        self.assertEqual(series["labels"], ["09-17", "09-18", "09-21"])

    def test_quakes_are_counted_per_hour_and_ignore_events_without_a_time(self):
        data, _ = make_data(quakes=usgs_with_events([5, 70, 130]))
        series = data.series("quakes", hours=6)
        self.assertEqual(series["chart"], "bar")
        self.assertEqual(len(series["values"]), 6)
        self.assertEqual(sum(series["values"]), 3.0)
        self.assertEqual(series["values"][-1], 1.0)  # the most recent hour

    def test_a_kind_needing_a_place_asks_for_one(self):
        data, _ = make_data()
        with self.assertRaises(ValueError):
            data.series("temperature")
        with self.assertRaises(ValueError):
            data.series("the price of cheese")

    def test_an_empty_forecast_is_an_error_not_a_flat_line(self):
        data, _ = make_data()
        data._fetch = lambda url, params: json.dumps({"hourly": {"time": []}})
        with self.assertRaises(ChartError):
            data.weather({"name": "Seattle", "latitude": 1.0, "longitude": 2.0}, hours=6)

    def test_a_dead_source_is_one_error_type(self):
        data, _ = make_data(fail="earthquake")
        with self.assertRaises(ChartError):
            data.quakes(hours=6)


class RouteTests(unittest.TestCase):
    def test_temperature_with_a_place(self):
        skill, params = route("chart the temperature in Seattle for the next 24 hours")
        self.assertEqual(skill, "chart-temperature")
        self.assertEqual(params["place"], "Seattle")
        self.assertEqual(params["hours"], 24)

    def test_rain(self):
        self.assertEqual(route("chart the rain in Tokyo tomorrow")[0], "chart-rain")

    def test_air_quality(self):
        skill, params = route("chart the air quality in Delhi")
        self.assertEqual(skill, "chart-air")
        self.assertEqual(params["place"], "Delhi")

    def test_debt_days(self):
        skill, params = route("chart the national debt for the last 90 days")
        self.assertEqual(skill, "chart-debt")
        self.assertEqual(params["days"], 90)
        self.assertNotIn("place", params)

    def test_quakes(self):
        skill, params = route("chart the earthquakes in the past 12 hours")
        self.assertEqual(skill, "chart-quakes")
        self.assertEqual(params["hours"], 12)

    def test_a_weather_chart_without_a_place_shows_the_examples(self):
        self.assertEqual(route("chart the temperature")[0], "help")
        self.assertEqual(route("chart stuff")[0], "help")

    def test_place_extraction(self):
        self.assertEqual(place_from_text("chart the temperature in Seattle for the next 24 hours"),
                         "Seattle")
        self.assertEqual(place_from_text("chart the rain in Tokyo tomorrow"), "Tokyo")
        self.assertIsNone(place_from_text("chart the national debt"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class QueueReader:
    def __init__(self):
        self._items: queue.Queue = queue.Queue()

    def push(self, text):
        self._items.put(text)

    def close(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader):
        self.reader = reader

    def write(self, text):
        self.reader.push(text)

    def flush(self):
        pass

    def close(self):
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


def images_of(result: dict) -> list[dict]:
    return [update["content"] for update in result["updates"]
            if update.get("sessionUpdate") == "agent_message_chunk"
            and update.get("content", {}).get("type") == "image"]


class TurnTests(unittest.TestCase):
    def turn(self, text: str, permission: str = "allow-once", **kw):
        from agent import ChartAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = ChartAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_temperature_chart_arrives_as_an_image_block(self):
        result, client, _ = self.turn("chart the temperature in Seattle for the next 24 hours")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Temperature in Seattle, Washington, United States", result["text"])
        self.assertIn("warmest", result["text"])
        self.assertIn("640x360 PNG", result["text"])
        self.assertIn("Open-Meteo", result["text"])
        images = images_of(result)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mimeType"], "image/png")
        blob = base64.b64decode(images[0]["data"])
        self.assertEqual(blob[:8], render.PNG_SIGNATURE)
        self.assertEqual(render.png_size(blob), (640, 360))
        self.assertGreater(colour_count(blob), 4)
        self.assertEqual(len(client.permission_requests), 1)

    def test_a_debt_chart_needs_no_place(self):
        result, _, _ = self.turn("chart the national debt for the last 90 days")
        self.assertIn("US national debt", result["text"])
        self.assertIn("40.1T", result["text"])
        self.assertIn("change over the window", result["text"])
        self.assertEqual(len(images_of(result)), 1)

    def test_an_earthquake_chart_counts_the_feed(self):
        result, _, _ = self.turn("chart the earthquakes in the past 6 hours")
        self.assertIn("total counted: 3 event(s) at M2.5 or above", result["text"])
        self.assertIn("itself a subset", result["text"])
        self.assertEqual(len(images_of(result)), 1)

    def test_the_summary_and_the_picture_come_from_one_read(self):
        result, _, feed = self.turn("chart the rain in Tokyo tomorrow")
        self.assertEqual(len([call for call in feed.calls if "forecast" in call[0]]), 1)
        self.assertEqual(len(images_of(result)), 1)

    def test_a_long_series_says_it_was_sampled(self):
        result, _, _ = self.turn("chart the temperature in Seattle for the next 7 days",
                                 forecast_hours=200)
        self.assertIn("168 hours", result["text"])
        self.assertIn("sampled every 2 points", result["text"])
        self.assertIn("84 of 168 points drawn", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("image content block", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("chart the temperature in Seattle", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])
        self.assertEqual(images_of(result), [])

    def test_a_dead_source_is_reported_with_no_image(self):
        result, _, _ = self.turn("chart the earthquakes in the past 6 hours", fail="earthquake")
        self.assertIn("could not read the data", result["text"])
        self.assertEqual(images_of(result), [])

    def test_an_unknown_place_is_reported_with_no_image(self):
        result, _, _ = self.turn("chart the temperature in Xyzzyville", place={"results": []})
        self.assertIn("no place called", result["text"])
        self.assertEqual(images_of(result), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
