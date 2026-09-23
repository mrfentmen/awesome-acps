"""A PNG writer, a bitmap font and a chart renderer - standard library only.

No ACP agent anywhere answers with an image, and none of them draw: this repo has no
dependency on matplotlib or Pillow and adding one is not allowed, so this module writes the PNG
itself. A PNG is a signature, an IHDR chunk, one zlib-compressed IDAT chunk and an IEND chunk
(zlib and struct are in the standard library), and the font is a 5x7 bitmap per character.
That is enough for a real chart: axes, gridlines, labels and a title.

The renderer is deliberately boring: fixed layout, fixed palette, no configuration a caller
could get wrong. `line_chart` and `bar_chart` return PNG bytes ready to hand to the editor.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: A small palette, so every chart in this agent looks the same.
INK = (31, 41, 51)
MUTED = (100, 116, 139)
GRID = (226, 232, 240)
AXIS = (148, 163, 184)
SERIES = (37, 99, 235)
SERIES_LIGHT = (147, 197, 253)
FILL = (219, 234, 254)
BACKGROUND = (255, 255, 255)
ALERT = (217, 119, 6)

#: 5 wide, 7 tall, rows separated by '/'. Enough for axis labels and a title.
FONT = {
    " ": "...../...../...../...../...../...../.....",
    "0": ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "1": "..#../.##../..#../..#../..#../..#../.###.",
    "2": ".###./#...#/....#/...#./..#../.#.../#####",
    "3": "####./....#/....#/.###./....#/....#/####.",
    "4": "...#./..##./.#.#./#..#./#####/...#./...#.",
    "5": "#####/#..../####./....#/....#/#...#/.###.",
    "6": "..##./.#.../#..../####./#...#/#...#/.###.",
    "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9": ".###./#...#/#...#/.####/....#/...#./.##..",
    "A": ".###./#...#/#...#/#####/#...#/#...#/#...#",
    "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".###./#...#/#..../#..../#..../#...#/.###.",
    "D": "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E": "#####/#..../#..../####./#..../#..../#####",
    "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".###./#...#/#..../#.###/#...#/#...#/.###.",
    "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I": ".###./..#../..#../..#../..#../..#../.###.",
    "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "L": "#..../#..../#..../#..../#..../#..../#####",
    "M": "#...#/##.##/#.#.#/#...#/#...#/#...#/#...#",
    "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "O": ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".###./#...#/#..../.###./....#/#...#/.###.",
    "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#",
    "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z": "#####/....#/...#./..#../.#.../#..../#####",
    ".": "...../...../...../...../...../...../..#..",
    ",": "...../...../...../...../...../..#../.#...",
    ":": "...../..#../..#../...../..#../..#../.....",
    ";": "...../..#../..#../...../..#../.#.../.....",
    "-": "...../...../...../#####/...../...../.....",
    "+": "...../..#../..#../#####/..#../..#../.....",
    "=": "...../...../#####/...../#####/...../.....",
    "/": "....#/....#/...#./..#../.#.../#..../#....",
    "%": "#...#/...#./..#../..#../.#.../#...#/.....",
    "(": "..#../.#.../.#.../.#.../.#.../.#.../..#..",
    ")": "..#../...#./...#./...#./...#./...#./..#..",
    "'": "..#../..#../...../...../...../...../.....",
    "?": ".###./#...#/....#/...#./..#../...../..#..",
    "!": "..#../..#../..#../..#../..#../...../..#..",
    "#": "#.#.#/#####/#.#.#/#####/#.#.#/#.#.#/.....",
    "\u00b0": ".##../#..#./.##../...../...../...../.....",
    "*": "...../#.#.#/.###./#####/.###./#.#.#/.....",
    "_": "...../...../...../...../...../...../#####",
    "&": ".##../#..#./.##../#.#.#/#..#./#.#.#/.##.#",
    "@": ".###./#...#/#.###/#.#.#/#.###/#..../.###.",
    "$": "..#../.####/#.#../.###./..#.#/####./..#..",
    "<": "...#./..#../.#.../#..../.#.../..#../...#.",
    ">": ".#.../..#../...#./....#/...#./..#../.#...",
    "|": "..#../..#../..#../..#../..#../..#../..#..",
}


def glyph(char: str) -> str:
    """The bitmap row string for one character, or None when the font has no glyph."""
    return FONT.get(char.upper()) or FONT.get(char) or FONT.get("?")


def png_bytes(width: int, height: int, pixels: bytearray) -> bytes:
    """RGB pixel data (3 bytes per pixel, row major) to a PNG file's bytes."""
    if len(pixels) != width * height * 3:
        raise ValueError("pixel buffer size does not match the image size")
    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)  # PNG filter type 0: none. zlib does the work.
        rows += pixels[y * stride:(y + 1) * stride]
    return (PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _chunk(b"IEND", b""))


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_size(blob: bytes) -> tuple[int, int]:
    """(width, height) out of a PNG's IHDR - used by the tests to prove the file is real."""
    if blob[:8] != PNG_SIGNATURE or blob[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", blob[16:24])
    return width, height


class Canvas:
    """A tiny RGB raster with just enough drawing for a chart."""

    def __init__(self, width: int, height: int, background=BACKGROUND) -> None:
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytearray(bytes(background) * (self.width * self.height))

    # -- pixels ------------------------------------------------------------

    def set_pixel(self, x, y, color) -> None:
        x, y = int(x), int(y)
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 3
            self.pixels[index:index + 3] = bytes(color)

    def rect(self, x0, y0, x1, y1, color) -> None:
        left, right = sorted((int(x0), int(x1)))
        top, bottom = sorted((int(y0), int(y1)))
        for y in range(max(0, top), min(self.height, bottom + 1)):
            row = y * self.width
            for x in range(max(0, left), min(self.width, right + 1)):
                index = (row + x) * 3
                self.pixels[index:index + 3] = bytes(color)

    def hline(self, x0, x1, y, color, thickness: int = 1) -> None:
        for offset in range(max(1, int(thickness))):
            self.rect(x0, int(y) + offset, x1, int(y) + offset, color)

    def vline(self, x, y0, y1, color, thickness: int = 1) -> None:
        for offset in range(max(1, int(thickness))):
            self.rect(int(x) + offset, y0, int(x) + offset, y1, color)

    def line(self, x0, y0, x1, y1, color, thickness: int = 1) -> None:
        """Bresenham's line, with a square brush for thickness."""
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        radius = max(0, int(thickness) - 1)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            if radius:
                self.rect(x0 - radius, y0 - radius, x0 + radius, y0 + radius, color)
            else:
                self.set_pixel(x0, y0, color)
            if x0 == x1 and y0 == y1:
                return
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    # -- text --------------------------------------------------------------

    def text(self, x, y, message: str, color=INK, scale: int = 1) -> int:
        """Draw text and return the x it ended at. Unknown characters draw as '?'."""
        scale = max(1, int(scale))
        cursor = int(x)
        for char in str(message):
            rows = glyph(char)
            for row_index, row in enumerate(rows.split("/")):
                for column, cell in enumerate(row):
                    if cell != "#":
                        continue
                    left = cursor + column * scale
                    top = int(y) + row_index * scale
                    if scale == 1:
                        self.set_pixel(left, top, color)
                    else:
                        self.rect(left, top, left + scale - 1, top + scale - 1, color)
            cursor += 6 * scale  # 5 wide plus one column of spacing
        return cursor

    def text_width(self, message: str, scale: int = 1) -> int:
        return 6 * max(1, int(scale)) * len(str(message))

    # -- output ------------------------------------------------------------

    def to_png(self) -> bytes:
        return png_bytes(self.width, self.height, self.pixels)


def format_value(value: float, unit: str = "") -> str:
    """A number a reader can take in: 40112118151111.92 becomes 40.1T, 153.0 becomes 153."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    magnitude = abs(number)
    if magnitude >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:.1f}T{unit}"
    if magnitude >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B{unit}"
    if magnitude >= 1_000_000:
        return f"{number / 1_000_000:.1f}M{unit}"
    if magnitude >= 1_000:
        return f"{number:,.0f}{unit}"
    if number.is_integer():
        return f"{number:.0f}{unit}"  # a count, or an AQI: '3', not '3.0'
    if magnitude >= 0.1:
        return f"{number:.2f}{unit}"
    return f"{number:.3f}{unit}"


#: Fixed layout. Changing these moves every chart at once, on purpose.
WIDTH, HEIGHT = 640, 360
LEFT, RIGHT, TOP, BOTTOM = 78, 18, 58, 42
GRID_LINES = 4


def draw_figure(title: str, subtitle: str, x_labels: list[str], y_values: list[float],
                y_unit: str = "") -> tuple[Canvas, int, int, int, int]:
    """The frame every chart shares: title, axis labels, gridlines. Returns the plot box."""
    canvas = Canvas(WIDTH, HEIGHT)
    canvas.text(LEFT, 14, title[:66], INK, scale=2)
    canvas.text(LEFT, 36, subtitle[:110], MUTED, scale=1)

    left, right = LEFT, WIDTH - RIGHT
    top, bottom = TOP, HEIGHT - BOTTOM
    low, high = min(y_values), max(y_values)
    if high == low:  # a flat series still gets a readable axis
        pad = abs(high) * 0.1 or 1.0
        low, high = low - pad, high + pad
    span = high - low

    for step in range(GRID_LINES + 1):
        y = int(bottom - (bottom - top) * step / GRID_LINES)
        canvas.hline(left, right, y, GRID if step else AXIS)
        value = low + span * step / GRID_LINES
        canvas.text(6, y - 3, format_value(value, ""), MUTED)
    canvas.vline(left, top, bottom, AXIS)

    if x_labels:
        # Evenly spaced, never overlapping: at most six labels along the bottom.
        count = len(x_labels)
        shown = min(6, count)
        for index in range(shown):
            position = index if shown == 1 else round(index * (count - 1) / (shown - 1))
            label = str(x_labels[position])[:12]
            x = left + (right - left) * (position / max(1, count - 1))
            canvas.text(max(left, x - canvas.text_width(label) // 2), bottom + 8, label, MUTED)
    return canvas, left, right, top, bottom


def line_chart(title: str, subtitle: str, points: list[tuple[str, float]], y_unit: str = "") -> bytes:
    """A line chart with a soft filled area under it. points is [(x label, value)]."""
    if not points:
        raise ValueError("a chart needs at least one point")
    values = [float(value) for _label, value in points]
    canvas, left, right, top, bottom = draw_figure(title, subtitle, [label for label, _v in points],
                                                   values, y_unit)
    low, high = min(values), max(values)
    if high == low:
        pad = abs(high) * 0.1 or 1.0
        low, high = low - pad, high + pad
    span = high - low
    count = len(values)

    def place(index: int, value: float) -> tuple[int, int]:
        x = left + (right - left) * (index / max(1, count - 1))
        y = bottom - (bottom - top) * ((value - low) / span)
        return int(round(x)), int(round(y))

    coords = [place(index, value) for index, value in enumerate(values)]
    for x, y in coords:  # the area fill, drawn as thin columns
        canvas.vline(x, y, bottom, FILL)
    for index in range(1, len(coords)):
        canvas.line(coords[index - 1][0], coords[index - 1][1], coords[index][0], coords[index][1],
                    SERIES, thickness=max(2, 3 if count < 40 else 2))
    for x, y in coords:
        canvas.rect(x - 2, y - 2, x + 2, y + 2, SERIES_LIGHT)
    _annotate(canvas, left, right, top, coords[-1], format_value(values[-1], y_unit), ALERT)
    return canvas.to_png()


def bar_chart(title: str, subtitle: str, points: list[tuple[str, float]], y_unit: str = "") -> bytes:
    """A bar chart for counted things. points is [(x label, value)]. Values are not signed."""
    if not points:
        raise ValueError("a chart needs at least one point")
    values = [float(value) for _label, value in points]
    if any(value < 0 for value in values):
        raise ValueError("a bar chart cannot show negative values here")
    canvas, left, right, top, bottom = draw_figure(title, subtitle, [label for label, _v in points],
                                                   values + [0.0], y_unit)
    low = min(values + [0.0])
    high = max(values + [0.0])
    if high == low:
        high = low + 1
    span = high - low
    count = len(values)
    slot = (right - left) / max(1, count)
    width = max(2, int(slot * 0.62))

    for index, value in enumerate(values):
        centre = left + slot * (index + 0.5)
        y = bottom - (bottom - top) * ((value - low) / span)
        canvas.rect(centre - width / 2, y, centre + width / 2, bottom, SERIES)
        if value == high:
            canvas.text(max(left, centre - canvas.text_width(format_value(value, y_unit)) // 2),
                        y - 10, format_value(value, y_unit), ALERT)
    return canvas.to_png()


def _annotate(canvas: Canvas, left: int, right: int, top: int, point: tuple[int, int],
              label: str, color) -> None:
    """The last value of a line chart, labelled and pulled inside the frame if it overflows."""
    x, y = point
    width = canvas.text_width(label, scale=1)
    x = min(max(left + 2, x - width - 6), right - width - 2)
    y = min(max(top + 2, y - 12), canvas.height - 24)
    canvas.text(x, y, label, color)
