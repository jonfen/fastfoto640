# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find where a named place is, so a photo can be put on the map.

The name ("Craters of the Moon, Idaho") is looked up with Nominatim, the
search service of OpenStreetMap. The name is sent to OpenStreetMap; nothing
else is. Their rules for free use are kept: one request a second at most, a
User-Agent that says who is asking, and no asking twice for the same name.

Names on the backs of photos are short, and many places share a name: on its
own, "Craters of the Moon" finds a park in New Zealand before the one in
Idaho. So the name is looked up twice, everywhere and in the home country,
and the home country's place wins unless the other is clearly the better
known ("Paris" is still the one in France).

A few words can also happen to match a street or a building somewhere
("Teton N.P." finds a Teton Drive in New Hampshire). Those are not what
people write on photos, so such a match counts as no match.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "fastfoto640 (https://github.com/jonfen/fastfoto640)"
SECONDS_BETWEEN = 1.1
# How much better known (Nominatim's "importance", 0 to 1) a place elsewhere
# has to be to win over one of the same name in the home country.
CLEARLY_BETTER_KNOWN = 0.1
# Matches that are surely not what was meant: streets, single buildings, and
# anything OpenStreetMap itself rates as obscure.
NOT_PLACES = {"highway", "building"}
LEAST_IMPORTANCE = 0.15


class PlaceError(Exception):
    """The lookup itself failed (no network, service busy): try again later."""


_lock = threading.Lock()
_found = {}              # (name as asked, country) -> (lat, lon, full name), or None
_last_asked = 0.0


def home_country() -> str:
    """The country this computer is set up for, as a two-letter code ("us"),
    or "" if it doesn't say."""
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        m = re.match(r"[a-z]{2,3}_([A-Z]{2})\b", os.environ.get(variable, ""))
        if m:
            return m.group(1).lower()
    return ""


def _ask(name: str, country: str = ""):
    """The best match for `name` (within `country`, if given):
    (latitude, longitude, full name, importance), or None."""
    global _last_asked
    wait = _last_asked + SECONDS_BETWEEN - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    query = {"q": name, "format": "jsonv2", "limit": 1}
    if country:
        query["countrycodes"] = country
    request = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(query)}",
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            matches = json.load(answer)
    except urllib.error.HTTPError as e:
        raise PlaceError(f"OpenStreetMap wouldn't look up places ({e.code})")
    except (OSError, ValueError) as e:
        raise PlaceError(f"couldn't reach OpenStreetMap ({type(e).__name__})")
    finally:
        _last_asked = time.monotonic()
    try:
        best = matches[0]
        importance = float(best.get("importance") or 0)
        if best.get("category") in NOT_PLACES or importance < LEAST_IMPORTANCE:
            return None
        return (float(best["lat"]), float(best["lon"]),
                str(best.get("display_name") or name), importance)
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def find(name: str, country: str = ""):
    """(latitude, longitude, the place's full name) of the best match for
    `name`, or None if OpenStreetMap knows no such place. `country` is the
    two-letter code of the country to prefer. Raises PlaceError if it
    couldn't be asked."""
    country = (country or "").strip().lower()
    key = (" ".join(name.split()).lower(), country)
    if not key[0]:
        return None
    with _lock:
        if key not in _found:
            best = _ask(name)
            home = _ask(name, country) if country else None
            if home and (best is None
                         or best[3] <= home[3] + CLEARLY_BETTER_KNOWN):
                best = home
            _found[key] = best[:3] if best else None
        return _found[key]


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        try:
            print(arg, "->", find(arg, home_country()))
        except PlaceError as err:
            print(arg, "->", err)
