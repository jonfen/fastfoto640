# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read what is written on the back of a photo, with Google's Gemini.

The back image is sent to the Gemini API (so it leaves this computer), which
answers with the handwriting, any printed or stamped text, and the date if
one is written there.

The API key is taken from the GEMINI_API_KEY environment variable or, since
an app started from the menu doesn't see your shell's variables, from the
file ~/.config/fastfoto640/gemini_api_key. Either may hold the key itself or
a 1Password reference to it (op://vault/item/field); a reference is fetched
with the 1Password CLI when first needed and kept in memory only.

A back with nothing written on it doesn't need sending: looks_blank() tells
those apart on this computer.

Settings used (all optional):
  gemini_model     which model to ask                    (DEFAULT_MODEL)
  gemini_thinking  how long it may think: "minimal", "low", "medium", "high",
                   or "" to leave that to the model      ("low")
  date_order     how to read 3/4/65: "MDY" or "DMY"      ("MDY")
  century        the century of a two-digit year         (1900)
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

DEFAULT_MODEL = "gemini-3.8-flash"
CONFIG_DIR = Path.home() / ".config" / "fastfoto640"
KEY_FILE = CONFIG_DIR / "gemini_api_key"
MAX_SIDE = 1600                 # the image is shrunk to this before sending
# Left to itself the model can think for a minute and more about a back with
# faint printing on it, and read it no better for that (and thinking is paid
# for). Handwriting is read just as well with little thinking.
DEFAULT_THINKING = "low"
TIMEOUT_SECONDS = 60
RETRY_DELAYS = (2, 8, 30)       # seconds, when Gemini is busy or unreachable
RETRY_CODES = {408, 429, 500, 502, 503, 504}

LEGIBILITY = ("clear", "partial", "illegible", "none")
DATE_SOURCES = ("handwritten", "printed", "none")

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "handwritten_text": {"type": "STRING"},
        "printed_text": {"type": "STRING"},
        "date_as_written": {"type": "STRING"},
        "year": {"type": "INTEGER", "nullable": True},
        "month": {"type": "INTEGER", "nullable": True},
        "day": {"type": "INTEGER", "nullable": True},
        "date_source": {"type": "STRING", "enum": list(DATE_SOURCES)},
        "place": {"type": "STRING"},
        "legibility": {"type": "STRING", "enum": list(LEGIBILITY)},
    },
    "required": ["handwritten_text", "printed_text", "date_as_written",
                 "year", "month", "day", "date_source", "place", "legibility"],
}

PROMPT = """\
This is a scan of the back of a photographic print. It may be blank.

The print went through the scanner any way up, so writing may be sideways or
upside down. Find which way up each piece of writing reads before copying
it. A short note read the wrong way up gives nonsense: a year written '85 or
'86 looks like 58. or 28. upside down, and a photo lab's 11/84 like 48/11. The
apostrophe of such a year sits before the figures, at the top; a mark after
them, at the bottom, means the note is upside down. The apostrophe can also
look like a 1: 184 with nothing else is '84.

Fill in these fields:

handwritten_text: Everything written by hand (pen, pencil, marker), copied
exactly as written: same spelling, same abbreviations, one line of writing
per line. Do not correct, translate or explain it. Where a word can't be read
put [?]; never guess a name. Empty if there is no handwriting.

printed_text: Text that was printed or stamped by a machine: photo lab date
stamps, processing or frame numbers, studio stamps. Give a repeating
paper-brand watermark (such as "Kodak" or "Fujicolor Crystal Archive Paper")
once only. Empty if there is none.

date_as_written: The date of the photo exactly as it appears on the back,
for example "July 4th '74". Prefer a handwritten date; if there is none, use
a date printed by the photo lab, which is usually month/year in figures
("3/84"). Empty if there is no date. An age ("age 4") or a phone number is
not a date, and neither is a lab's row of processing codes.

year, month, day: That same date as numbers. Use null for any part that
isn't given: "Summer 1962" is year 1962 with month and day null. Only use
what is written; do not estimate a date from the look of the print.
{date_rules}

date_source: "handwritten", "printed", or "none".

place: Where the photo was taken, if the back says: a town, a park, a
country, a landmark, exactly as written, for example "Craters of the Moon".
Empty if no place is named. A person's name is not a place, nor is an
occasion ("Easter") or somebody's home ("Grandma's house"). Do not work a
place out from anything else.

legibility: How well the handwriting could be read: "clear", "partial" (some
words uncertain), "illegible", or "none" if there is no handwriting.
"""


class HandwritingError(Exception):
    """This photo's back couldn't be read (worth trying again later)."""


class SetupError(HandwritingError):
    """Nothing can be read until something is fixed: key, package or model."""


def _configured_key():
    """The API key as configured: the key itself, an op:// reference to it,
    or None. Doesn't contact 1Password."""
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


_fetch_lock = threading.Lock()
_fetched = {}            # 1Password reference -> key, for as long as we run
_fetch_failed = {}       # 1Password reference -> (when, why)
FETCH_RETRY_SECONDS = 60


def _from_1password(reference: str) -> str:
    """Fetch a key with the 1Password CLI. The 1Password app may ask the user
    to approve, so this can take as long as they do."""
    exe = shutil.which("op")
    if not exe:
        raise SetupError("the API key is in 1Password, but the 1Password "
                         "command-line tool (op) isn't installed")
    try:
        done = subprocess.run([exe, "read", "--no-newline", reference],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("1Password didn't answer when asked for the API key")
    key = done.stdout.strip()
    if done.returncode != 0 or not key:
        if "No accounts configured" in done.stderr:
            why = ("turn on Settings > Developer > Integrate with 1Password CLI "
                   "in the 1Password app")
        else:
            lines = [ln.strip() for ln in done.stderr.splitlines() if ln.strip()]
            # op's errors look like "[ERROR] 2026/11/22 14:45:13 the message"
            why = (re.sub(r"^\[\w+\] [\d/]+ [\d:]+ ", "", lines[-1]) if lines
                   else "no key came back")
        raise SetupError(f"couldn't get the API key from 1Password: {why}")
    return key


def api_key() -> str:
    """The Gemini API key. Raises SetupError if there is none, or if it is in
    1Password and can't be fetched (which is tried again after a minute)."""
    value = _configured_key()
    if not value:
        raise SetupError(f"no Gemini API key: put one in {KEY_FILE} (see the README)")
    if not value.startswith("op://"):
        return value
    with _fetch_lock:
        if value in _fetched:
            return _fetched[value]
        when, why = _fetch_failed.get(value, (0, ""))
        if time.monotonic() - when < FETCH_RETRY_SECONDS and why:
            raise SetupError(why)
        try:
            _fetched[value] = _from_1password(value)
        except SetupError as e:
            _fetch_failed[value] = (time.monotonic(), str(e))
            raise
        return _fetched[value]


def setup_problem(fetch_key=True):
    """What's missing before handwriting can be read, or None if all is ready.

    With fetch_key=False a key kept in 1Password is taken on trust rather
    than fetched, so this returns at once and never makes 1Password prompt."""
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return "the google-genai package isn't installed; run ./install.sh again"
    try:
        if fetch_key:
            api_key()
        elif not _configured_key():
            raise SetupError(f"no Gemini API key: put one in {KEY_FILE} (see the README)")
    except SetupError as e:
        return str(e)
    return None


# A number of two figures with nothing else on the back: people date prints
# that way ("84"), and they number them too (the frame, the page of an
# album). From this number up it is taken for the year; below it, only when
# it has an apostrophe ('34). Films had up to 36 frames.
LOWEST_BARE_YEAR = 40


def _century(settings) -> int:
    return int(settings.get("century", 1900)) // 100


def _date_rules(settings) -> str:
    order = str(settings.get("date_order", "MDY")).upper()
    first = "day/month/year" if order == "DMY" else "month/day/year"
    century = _century(settings)
    return (f"Read an all-number date such as 3/4/65 as {first}. "
            f"A two-digit year is in the {century}00s: '65 is {century}65. "
            f"A year on its own is a date, and so is a two-digit year with an "
            f"apostrophe and nothing else ('84). A number of two figures with "
            f"no apostrophe and nothing else is the year if it is "
            f"{LOWEST_BARE_YEAR} or more (84 is {century}84); a smaller one "
            f"(12) is a frame or album number, not a date.")


_BARE_NUMBER_RE = re.compile(r"(['’‘`´]?)\s*(\d\d)\.?")


def _settle_bare_number(found: dict, settings) -> dict:
    """Apply the rule for a two-figure number that is all the handwriting
    on a back (see LOWEST_BARE_YEAR), whatever the model made of it: it has
    been seen to read "84" on one back as the year and on the next as
    nothing."""
    m = _BARE_NUMBER_RE.fullmatch(found["handwritten_text"].strip())
    if not m:
        return found
    number = int(m.group(2))
    year = _century(settings) * 100 + number
    if m.group(1) or number >= LOWEST_BARE_YEAR:
        if found["year"] is None:
            found = {**found, "year": year, "month": None, "day": None,
                     "date_as_written": found["handwritten_text"].strip(),
                     "date_source": "handwritten"}
    elif found["date_source"] == "handwritten" and found["year"] == year:
        found = {**found, "year": None, "month": None, "day": None,
                 "date_as_written": "", "date_source": "none"}
    return found


def _jpeg_to_send(path) -> bytes:
    img = cv2.imread(str(path))
    if img is None:
        raise HandwritingError(f"can't open {Path(path).name}")
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (round(w * scale), round(h * scale)),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise HandwritingError(f"can't prepare {Path(path).name} for sending")
    return buf.tobytes()


_clients = {}


def _client(key):
    if key not in _clients:
        from google import genai
        from google.genai import types
        _clients[key] = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=TIMEOUT_SECONDS * 1000))
    return _clients[key]


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse(text) -> dict:
    """Turn the model's JSON answer into our result, tolerating sloppiness."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise HandwritingError("Gemini's answer wasn't understood")
    if not isinstance(data, dict):
        raise HandwritingError("Gemini's answer wasn't understood")

    def words(key):
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    handwritten = words("handwritten_text")
    legibility = words("legibility")
    if legibility not in LEGIBILITY:
        legibility = "partial" if handwritten else "none"
    source = words("date_source")
    year = _int_or_none(data.get("year"))
    if source not in DATE_SOURCES:
        source = "none" if year is None else "handwritten"
    return {
        "handwritten_text": handwritten,
        "printed_text": words("printed_text"),
        "date_as_written": words("date_as_written"),
        "year": year,
        "month": _int_or_none(data.get("month")),
        "day": _int_or_none(data.get("day")),
        "date_source": source,
        "place": words("place"),
        "legibility": legibility,
    }


# What read_back would say about a back with nothing on it.
NOTHING_WRITTEN = {"handwritten_text": "", "printed_text": "", "date_as_written": "",
                   "year": None, "month": None, "day": None, "date_source": "none",
                   "place": "", "legibility": "none"}

INK_DARKER_BY = 20       # grey levels below the paper around it that count as ink
BLANK_BELOW = 0.0002     # share of the back that may be that dark and still be blank
EDGE_STRIP = 0.02        # of the longer side: the strip along each edge that isn't counted


def looks_blank(path) -> bool:
    """Whether the back image at `path` has nothing written on it, judged
    here, without sending it anywhere.

    Writing is darker than the paper around it; a paper maker's watermark
    hardly is. So are the scanner's background and the shadow of the paper's
    edge, but those run along the border of the image: whatever is joined to
    the border is left out, and so is a narrow strip inside it. (A wider
    strip, with nothing else done, missed captions written along the bottom
    edge.) The line is drawn low, so a doubtful back, one with a stain or a
    lab's faint print, counts as written and is sent for reading. An image
    that can't be opened is not called blank."""
    img = cv2.imread(str(path))
    if img is None:
        return False
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (round(w * scale), round(h * scale)),
                         interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    rim = int(EDGE_STRIP * max(h, w))
    if h - 2 * rim < 50 or w - 2 * rim < 50:
        return False
    dark = img.min(axis=2)              # darkest channel: black, blue and red ink alike
    # The paper's own shade around each spot: a wide median of a small copy.
    small = cv2.resize(dark, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    paper = cv2.resize(cv2.medianBlur(small, 31), (dark.shape[1], dark.shape[0]),
                       interpolation=cv2.INTER_LINEAR)
    ink = (paper.astype("int16") - dark > INK_DARKER_BY).astype("uint8")
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
    _, joined = cv2.connectedComponents(ink, connectivity=8)
    at_border = np.unique(np.concatenate([joined[0], joined[-1], joined[:, 0], joined[:, -1]]))
    ink[np.isin(joined, at_border[at_border > 0])] = 0
    return float(ink[rim:h - rim, rim:w - rim].sum()) / ink.size < BLANK_BELOW


def read_back(path, settings=None, retry=True, client=None) -> dict:
    """Ask Gemini what is written on the back image at `path`.

    Returns a dict with handwritten_text, printed_text, date_as_written,
    year, month, day (None where unknown), date_source, place and legibility.
    Raises SetupError or HandwritingError. `retry=False` gives up at the
    first busy or unreachable answer instead of waiting and trying again."""
    settings = settings or {}
    if client is None:
        problem = setup_problem(fetch_key=False)
        if problem:
            raise SetupError(problem)
        client = _client(api_key())
    from google.genai import errors, types

    model = settings.get("gemini_model") or DEFAULT_MODEL
    image = _jpeg_to_send(path)
    contents = [types.Part.from_bytes(data=image, mime_type="image/jpeg"),
                PROMPT.format(date_rules=_date_rules(settings))]
    thinking = settings.get("gemini_thinking", DEFAULT_THINKING)
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=SCHEMA,
        temperature=0,
        thinking_config=(types.ThinkingConfig(thinking_level=str(thinking).upper())
                         if thinking else None))

    delays = list(RETRY_DELAYS) if retry else []
    while True:
        try:
            answer = client.models.generate_content(
                model=model, contents=contents, config=config)
            break
        except errors.APIError as e:
            message = (getattr(e, "message", None) or str(e)).strip()
            if e.code in (401, 403) or "api key" in message.lower():
                raise SetupError(f"Gemini refused the API key: {message}")
            if e.code == 404:
                raise SetupError(f"Gemini has no model called '{model}'; set "
                                 f"\"gemini_model\" in the settings file")
            if e.code not in RETRY_CODES:
                raise HandwritingError(f"Gemini: {message}")
            problem = ("Gemini is over its rate limit" if e.code == 429
                       else f"Gemini is unavailable ({e.code})")
        except Exception as e:                    # no network, timeout, ...
            problem = f"couldn't reach Gemini ({type(e).__name__})"
        if not delays:
            raise HandwritingError(problem)
        time.sleep(delays.pop(0))

    try:
        text = answer.text
    except Exception:
        text = None
    if not text:
        raise HandwritingError("Gemini gave no answer for this image")
    return _settle_bare_number(_parse(text), settings)


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(arg)
        try:
            print(json.dumps(read_back(arg), indent=2, ensure_ascii=False))
        except HandwritingError as err:
            print("  ", err)
