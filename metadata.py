# SPDX-License-Identifier: AGPL-3.0-or-later
"""Put captions and dates into scanned photos, and keep captions.csv.

The caption and the date go inside the image files, in the fields photo
programs read, so they travel with the photo:

  caption   EXIF ImageDescription, XMP dc:description, IPTC Caption-Abstract
  date      EXIF DateTimeOriginal (what photo programs sort by) and
            XMP photoshop:DateCreated (which can hold just a year, or a year
            and month, when that's all that is known)
  place     EXIF GPS latitude and longitude (what photo programs put on a
            map), and the name in XMP iptcCore:Location
  scanned   EXIF CreateDate, i.e. "DateTimeDigitized"

A print has a date at best, never a time. DateTimeOriginal needs one, so
every photo gets noon (the time least likely to slip to another day when a
program applies a time zone), plus one second per photo number, so that
photos with the same date stay in the order they were scanned.

The writing is done by exiftool, which changes the metadata without
recompressing the picture. The file's modified time is set to the photo's
date too. (Linux has no way to set a file's creation time.)

captions.csv, in the same folder, has one row per photo with the same
information. It can be corrected in a spreadsheet and written back into the
files with `fastfoto-enrich --apply-csv`.
"""

import csv
import datetime
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CSV_NAME = "captions.csv"
FIELDS = ["number", "front", "back", "handwritten_text", "printed_text",
          "date_as_written", "date_applied", "place", "gps", "place_found",
          "rotation", "faces_found", "status", "notes"]
FIRST_PHOTO_YEAR = 1826


class MetadataError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Dates: (year, month, day), where month and day may be None if unknown
# --------------------------------------------------------------------------- #

def clean_date(year, month=None, day=None):
    """Return (year, month, day) keeping only the parts that can be right,
    or None if even the year can't be."""
    today = datetime.date.today()
    if not isinstance(year, int) or not FIRST_PHOTO_YEAR <= year <= today.year:
        return None
    if not isinstance(month, int) or not 1 <= month <= 12:
        return (year, None, None)
    if isinstance(day, int):
        try:
            datetime.date(year, month, day)
        except ValueError:
            day = None
    else:
        day = None
    if datetime.date(year, month, day or 1) > today:      # later this year
        return (year, None, None)
    return (year, month, day)


def format_date(date) -> str:
    """'1974-07-04', '1974-07', '1974', or '' for no date."""
    if not date:
        return ""
    y, m, d = date
    return f"{y:04d}" + (f"-{m:02d}" if m else "") + (f"-{d:02d}" if m and d else "")


def parse_date(text: str):
    """The reverse of format_date. Raises ValueError if it isn't a date."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{4})(?:[-/:.](\d{1,2})(?:[-/:.](\d{1,2}))?)?", text)
    if not m:
        raise ValueError(f"'{text}' is not a date like 1974, 1974-07 or 1974-07-04")
    y, mo, d = (int(g) if g else None for g in m.groups())
    date = clean_date(y, mo, d)
    if date != (y, mo, d):
        raise ValueError(f"'{text}' is not a possible date")
    return date


def photo_time(number) -> tuple:
    """(hour, minute, second) given to photo `number`: noon, plus a second
    per photo number (starting over after ten hours' worth of photos)."""
    seconds = 12 * 3600 + int(number) % 36000
    return seconds // 3600, seconds % 3600 // 60, seconds % 60


# --------------------------------------------------------------------------- #
# Places: (latitude, longitude) in degrees, north and east positive
# --------------------------------------------------------------------------- #

def format_gps(gps) -> str:
    """'43.41660, -113.51660' (about a metre's precision), or '' for none."""
    return f"{gps[0]:.5f}, {gps[1]:.5f}" if gps else ""


def parse_gps(text: str):
    """The reverse of format_gps: (latitude, longitude), or None for empty.
    Takes what a map gives when you copy a spot's coordinates. Raises
    ValueError if it isn't a pair of coordinates."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"\(?\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*\)?", text)
    if not m:
        raise ValueError(f"'{text}' is not a position like 43.4166, -113.5166")
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"'{text}' is not a position on Earth")
    return lat, lon


# --------------------------------------------------------------------------- #
# Writing into the image files
# --------------------------------------------------------------------------- #

def find_exiftool(settings=None):
    """Path of exiftool, or None. Settings may name one with "exiftool"."""
    return (settings or {}).get("exiftool") or shutil.which("exiftool")


def write_metadata(paths, caption, date, scanned_at=None, exiftool=None,
                   number=0, place="", gps=None):
    """Write the caption, date and place into every file in `paths`.

    An empty caption or place, or a date or gps of None, removes those
    fields. `number` is the photo's number, which sets its time of day (see
    photo_time). `scanned_at` (seconds since the epoch) is recorded as the
    scan time, but only in files that don't have one yet."""
    exe = exiftool or shutil.which("exiftool")
    if not exe:
        raise MetadataError("exiftool isn't installed "
                            "(sudo apt install libimage-exiftool-perl)")
    caption = (caption or "").strip()
    args = [exe]
    if scanned_at is not None:
        stamp = time.strftime("%Y:%m:%d %H:%M:%S", time.localtime(scanned_at))
        # -wm cg: only create the tag, never change one that is already there.
        args += ["-wm", "cg", f"-EXIF:CreateDate={stamp}", "-execute"]
    args += [f"-EXIF:ImageDescription={caption}",
             f"-XMP-dc:Description={caption}",
             f"-IPTC:Caption-Abstract={caption}"]
    if caption:
        args.append("-IPTC:CodedCharacterSet=UTF8")
    if date:
        y, m, d = date
        hh, mm, ss = photo_time(number)
        args += [f"-EXIF:DateTimeOriginal={y:04d}:{m or 1:02d}:{d or 1:02d} "
                 f"{hh:02d}:{mm:02d}:{ss:02d}",
                 f"-XMP-photoshop:DateCreated={format_date(date)}"]
    else:
        args += ["-EXIF:DateTimeOriginal=", "-XMP-photoshop:DateCreated="]
    args.append(f"-XMP-iptcCore:Location={(place or '').strip()}")
    if gps:
        lat, lon = gps
        # exiftool takes the hemisphere (the "Ref") from the sign of a number.
        args += [f"-EXIF:GPSLatitude={abs(lat):.6f}", f"-EXIF:GPSLatitudeRef={lat:.6f}",
                 f"-EXIF:GPSLongitude={abs(lon):.6f}", f"-EXIF:GPSLongitudeRef={lon:.6f}"]
    else:
        args += ["-EXIF:GPSLatitude=", "-EXIF:GPSLatitudeRef=",
                 "-EXIF:GPSLongitude=", "-EXIF:GPSLongitudeRef="]
    args += ["-common_args", "-overwrite_original"] + [str(p) for p in paths]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MetadataError(f"couldn't run exiftool: {e}")
    if done.returncode != 0:
        errors = [ln for ln in done.stderr.splitlines() if ln.startswith("Error")]
        raise MetadataError("exiftool: " + (errors[0] if errors
                                            else done.stderr.strip() or "failed"))


def copy_metadata(source, target, exiftool=None):
    """Give `target` all the metadata `source` has (for a picture that has
    been saved again, which loses it)."""
    exe = exiftool or shutil.which("exiftool")
    if not exe:
        raise MetadataError("exiftool isn't installed "
                            "(sudo apt install libimage-exiftool-perl)")
    try:
        done = subprocess.run([exe, "-TagsFromFile", str(source), "-all:all",
                               "-overwrite_original", str(target)],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MetadataError(f"couldn't run exiftool: {e}")
    if done.returncode != 0:
        raise MetadataError("exiftool: " + (done.stderr.strip() or "failed"))


def set_file_dates(paths, date, number=0) -> bool:
    """Set each file's modified time to the photo's date (and the time of
    day photo `number` gets). False if the filesystem can't hold that date
    (FAT and exFAT start in 1980)."""
    y, m, d = date
    try:
        stamp = datetime.datetime(y, m or 1, d or 1, *photo_time(number)).timestamp()
        for p in paths:
            os.utime(p, (stamp, stamp))
            if abs(os.stat(p).st_mtime - stamp) > 2:
                return False
    except (OSError, OverflowError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- #
# captions.csv
# --------------------------------------------------------------------------- #

def _load(path: Path):
    """(column names, {number: row}) of a captions.csv; empty if there is none."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            names = list(reader.fieldnames or [])
            rows = {}
            for row in reader:
                try:
                    rows[int(row.get("number") or "")] = {
                        k: (v or "") for k, v in row.items() if k is not None}
                except ValueError:
                    continue          # a row without a photo number
    except OSError:
        return [], {}
    return names, rows


def read_rows(folder) -> dict:
    """{photo number: row} for every photo listed in the folder's captions.csv."""
    return _load(Path(folder) / CSV_NAME)[1]


def _rewrite(path: Path, names, rows):
    """Write the whole file again, with our columns first and then any
    columns somebody added by hand."""
    names = FIELDS + [n for n in names if n not in FIELDS]
    tmp = path.with_name(f".{CSV_NAME}.part")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, names, restval="", extrasaction="ignore")
        writer.writeheader()
        for n in sorted(rows):
            writer.writerow(rows[n])
    os.replace(tmp, path)


def save_row(folder, row: dict):
    """Add a photo's row to captions.csv, or replace the one it already has."""
    path = Path(folder) / CSV_NAME
    names, rows = _load(path)
    number = int(row["number"])
    if names == FIELDS and number not in rows:
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, FIELDS, extrasaction="ignore").writerow(row)
        return
    rows[number] = {**rows.get(number, {}), **row}
    _rewrite(path, names, rows)


def delete_row(folder, number) -> bool:
    """Take a photo's row out of captions.csv. False if it had none."""
    path = Path(folder) / CSV_NAME
    names, rows = _load(path)
    if rows.pop(int(number), None) is None:
        return False
    _rewrite(path, names, rows)
    return True


def add_missing_columns(folder) -> list:
    """Give a captions.csv made by an older version the columns it lacks
    (empty), so they are there to be filled in. Returns their names."""
    path = Path(folder) / CSV_NAME
    names, rows = _load(path)
    missing = [n for n in FIELDS if n not in names] if names else []
    if missing:
        _rewrite(path, names, rows)
    return missing
