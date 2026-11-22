#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Captions and dates for scanned photos.

After a photo's two sides are saved, this reads the handwriting on the back
(handwriting.py), writes the caption and date into both files and adds the
photo to captions.csv (metadata.py). The scanning app does this in a
background thread (Enricher); the `fastfoto-enrich` command does it for a
folder of photos that are already scanned:

    fastfoto-enrich [FOLDER]              photos not read yet, and failures
    fastfoto-enrich --all [FOLDER]        read every back again
    fastfoto-enrich --apply-csv [FOLDER]  write an edited captions.csv back
                                          into the files, looking up places
                                          that are new or were changed
                                          (nothing goes to Gemini)
    fastfoto-enrich --sort [FOLDER]       move fronts and backs lying loose
                                          in the folder into fronts/, backs/
    fastfoto-enrich [FOLDER] --turn 180 78 109
                                          turn photos 78 and 109 upside down
                                          (also: --turn right ..., --turn left ...)
    fastfoto-enrich [FOLDER] --remove 402 405
                                          take photos 402 and 405 out (ones
                                          that were scanned again): their
                                          files go to FOLDER/removed

Photos (00001f.jpg, 00001b.jpg) are kept in two folders, FOLDER/fronts and
FOLDER/backs, so the fronts can be looked through, or uploaded, on their
own; captions.csv sits beside them in FOLDER. Photos lying loose in FOLDER,
as older versions left them, are found too.

A place named on a back is looked up (places.py) as soon as it has been
read, so the photo can be put on a map, unless the "map_places" setting is
off. What it was matched to is kept in the row's place_found column, as
    Charlotte -> Charlotte, Mecklenburg County, North Carolina, United States (35.22721, -80.84308)
which is also how a later change to the place is noticed.

Each photo's row in captions.csv has a status:
    ok            done
    needs_review  done, but something in "notes" deserves a look
    not_read      the back hasn't been sent for reading
    error         reading or writing failed; fastfoto-enrich tries again
"""

import argparse
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import cv2

import handwriting
import metadata
import orient
import places

PREFIX = "FastFoto_"
# 00001f.jpg / 00001b.jpg (photos) or FastFoto_00001.jpg / FastFoto_00001_b.jpg
NAME_RE = re.compile(rf"^(?:{PREFIX}(\d+)(_b)?|(\d+)([fb]))\.jpg$", re.IGNORECASE)
# Where the two sides of photos are kept, inside the output folder.
SIDE_FOLDERS = {"f": "fronts", "b": "backs"}
# What --turn can be told, and the degrees clockwise that it means.
TURNS = {"right": 90, "90": 90, "180": 180, "left": 270, "270": 270}
# Where photos taken out with --remove go. Nothing here is looked at again.
REMOVED_FOLDER = "removed"


def photo_names(naming: str, n: int):
    """File names of photo number n: (front, back)."""
    if naming == "photo":
        return f"{n:05d}f.jpg", f"{n:05d}b.jpg"
    return f"{PREFIX}{n:05d}.jpg", f"{PREFIX}{n:05d}_b.jpg"


def photo_paths(folder, naming: str, n: int):
    """Where photo number n is saved: (front, back). Photos go into a folder
    for each side; the FastFoto_ naming, which Dollar Detective reads, stays
    as one flat folder."""
    front, back = photo_names(naming, n)
    folder = Path(folder)
    if naming == "photo":
        return folder / SIDE_FOLDERS["f"] / front, folder / SIDE_FOLDERS["b"] / back
    return folder / front, folder / back


def parse_name(name: str):
    """(number, 'f' or 'b') for one of our file names, else None."""
    m = NAME_RE.match(name)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)), "b" if m.group(2) else "f"
    return int(m.group(3)), m.group(4).lower()


def find_photos(folder) -> dict:
    """{number: {'f': Path, 'b': Path}} for the scans in a folder: those in
    its fronts/ and backs/ folders, and any lying loose in the folder itself
    (where a file is in both places, the one in fronts/ or backs/ counts)."""
    folder = Path(folder)
    photos = {}
    for where in (folder, *(folder / name for name in SIDE_FOLDERS.values())):
        try:
            entries = sorted(os.listdir(where))
        except OSError:
            continue
        for entry in entries:
            parsed = parse_name(entry)
            if parsed:
                photos.setdefault(parsed[0], {})[parsed[1]] = where / entry
    return photos


def sort_photos(folder) -> tuple:
    """Move photos lying loose in `folder` (00001f.jpg, 00001b.jpg) into its
    fronts/ and backs/ folders. Files in the FastFoto_ naming stay where
    they are. Returns (moved, left alone because one is already there)."""
    folder = Path(folder)
    moved = skipped = 0
    for entry in sorted(os.listdir(folder)):
        m = NAME_RE.match(entry)
        if not m or m.group(1) or not (folder / entry).is_file():
            continue
        target = folder / SIDE_FOLDERS[m.group(4).lower()] / entry
        if target.exists():
            skipped += 1
            continue
        target.parent.mkdir(exist_ok=True)
        os.replace(folder / entry, target)
        moved += 1
    return moved, skipped


def photo_numbers(words) -> list:
    """The photo numbers in words like "78", "00109" and "103-125"."""
    numbers = []
    for word in words:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", word)
        if not m or int(m.group(2) or m.group(1)) < int(m.group(1)):
            raise ValueError(f"'{word}' is not a photo number like 78, or a range like 103-125")
        numbers += range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1)
    return list(dict.fromkeys(numbers))     # each once: a photo named twice isn't turned twice


def remove_photo(folder, number) -> int:
    """Take photo `number` out: its files are moved to the folder's removed/
    folder (not deleted, in case it was the wrong number) and its row is
    taken out of captions.csv. Its number is not used again. Returns how
    many files were moved; raises ValueError if there is no such photo."""
    folder = Path(folder)
    files = find_photos(folder).get(number, {})
    had_row = metadata.delete_row(folder, number)
    if not files and not had_row:
        raise ValueError("there is no photo with this number")
    for path in files.values():
        target = folder / REMOVED_FOLDER / path.name
        target.parent.mkdir(exist_ok=True)
        if target.exists():             # removed before, and the number is back
            target = target.with_name(f"{target.stem}-{int(time.time())}{target.suffix}")
        os.replace(path, target)
    return len(files)


def forget_thumbnails(path) -> int:
    """Delete the desktop's cached thumbnails of the image at `path`, so the
    file manager makes new ones. It goes by a file's modified time to tell
    whether its thumbnail is still good, and our files keep the photo's date
    as their modified time whatever is done to them. Returns how many were
    deleted."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "thumbnails"
    name = hashlib.md5(Path(path).resolve().as_uri().encode()).hexdigest() + ".png"
    deleted = 0
    try:
        for size in cache.iterdir():
            try:
                (size / name).unlink()
                deleted += 1
            except OSError:
                pass
    except OSError:
        pass
    return deleted


HOLD_SECONDS = 2.0     # how long show_changes holds the other time


def show_changes(paths) -> None:
    """Get a file manager that has the folder open to show pictures that
    were changed. It goes by a file's modified time to tell whether the
    picture it is showing is still right, and our files keep the photo's
    date as their modified time whatever is done to them: it would go on
    showing the old picture, reloading or not. So the time is moved on by a
    second, held there long enough to be noticed, and put back. Each move
    makes the file manager look at the picture again."""
    times = {}
    for path in paths:
        try:
            was = os.stat(path)
            os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns + 1_000_000_000))
            times[path] = (was.st_atime_ns, was.st_mtime_ns)
        except OSError:
            pass
    try:
        if times:
            time.sleep(HOLD_SECONDS)
    finally:
        for path, ns in times.items():
            try:
                os.utime(path, ns=ns)
            except OSError:
                pass


def turn_photo(folder, number, degrees, settings):
    """Turn photo `number` by `degrees` clockwise (90, 180 or 270), as the
    front is looked at; its back is turned the other way, so both keep the
    same top edge. The pictures are saved again (at the "jpeg_quality"
    setting) with their captions and dates as they were. Raises ValueError
    or metadata.MetadataError."""
    files = find_photos(folder).get(number)
    if not files:
        raise ValueError("its files aren't in this folder")
    exiftool = metadata.find_exiftool(settings)
    quality = int(settings.get("jpeg_quality", 95))
    for side, path in files.items():
        img = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            raise ValueError(f"can't open {path.name}")
        img = orient.turn(img, degrees // 90 * (1 if side == "f" else -1))
        tmp = path.with_name(f".{path.name}.part")
        try:
            ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                raise ValueError(f"couldn't save {path.name}")
            tmp.write_bytes(data.tobytes())
            metadata.copy_metadata(path, tmp, exiftool)
            times = path.stat()
            os.utime(tmp, (times.st_atime, times.st_mtime))
            os.replace(tmp, path)
            forget_thumbnails(path)
        finally:
            tmp.unlink(missing_ok=True)
    row = metadata.read_rows(folder).get(number)
    if row:
        notes = [n for n in _notes(row) if not n.startswith("rotation:")]
        turned = (int(row.get("rotation") or 0) + degrees) % 360
        row = {**row, "rotation": str(turned), "notes": "; ".join(notes)}
        if row.get("status") in ("ok", "needs_review"):
            row["status"] = "needs_review" if notes else "ok"
        metadata.save_row(folder, row)


def _notes(row) -> list:
    return [n.strip() for n in (row.get("notes") or "").split(";") if n.strip()]


# place_found: the place as it was looked up -> what it matched (its position)
_FOUND_RE = re.compile(r"(.*?) -> (.*) \((-?\d+\.\d+, -?\d+\.\d+)\)")
OFF_THE_MAP = "none"   # in the gps column: this place is not to be looked up


def place_position(row, settings, notes) -> tuple:
    """Where the photo in `row` was taken: (latitude, longitude) or None.

    A position typed into the gps column is used as it is. Otherwise the
    place is looked up, when there is one and the "map_places" setting
    allows; that fills in the row's gps and place_found. Of two places with
    the same name, the one in the "home_country" setting is preferred (by
    default, the country this computer is set up for). A position that was
    looked up for a place that has since been changed, or removed, is
    dropped (and looked up again). "none" in the gps column keeps the photo
    off the map, for a place too large or too vague to pin down: it is not
    looked up. Trouble is added to `notes`. Raises ValueError if the gps
    column holds something that isn't a position."""
    place = (row.get("place") or "").strip()
    if (row.get("gps") or "").strip().lower() == OFF_THE_MAP:
        row["gps"], row["place_found"] = OFF_THE_MAP, ""
        return None
    gps = metadata.parse_gps(row.get("gps", ""))
    record = _FOUND_RE.fullmatch((row.get("place_found") or "").strip())
    if gps:
        looked_up = record and metadata.format_gps(gps) == record.group(3)
        if not looked_up:
            row["place_found"] = ""             # typed by hand: leave it be
            return gps
        if record.group(1) == place:
            return gps                          # still the place it was found for
    row["gps"] = row["place_found"] = ""
    if not place or not settings.get("map_places", True):
        return None
    country = settings.get("home_country")
    try:
        hit = places.find(place, places.home_country() if country is None else country)
    except places.PlaceError as e:
        notes.append(f"place: not looked up, {e}")
        return None
    if hit is None:
        notes.append(f'place: "{place.replace(";", ",")}" was not found on the map')
        return None
    row["gps"] = metadata.format_gps(hit[:2])
    row["place_found"] = f"{place} -> {hit[2]} ({row['gps']})"
    return metadata.parse_gps(row["gps"])


def needs_reading(row) -> bool:
    """Whether a photo's back still has to be sent for reading."""
    if not row:
        return True
    if row.get("status") == "not_read":
        return True
    return _read_failed(row)


def enrich_photo(folder, number, front, back, settings, *, read=True,
                 turned=None, crop_notes=None, scanned_at=None, retry=True) -> dict:
    """Read one photo's back (if `read`), write caption and date into its
    files, record it in captions.csv, and return its row.

    turned: the orient.Result from scanning, if the photo was just scanned.
    crop_notes: what the crop had to say about it, likewise.
    Never raises for a problem with the photo: that goes in the row."""
    old = metadata.read_rows(folder).get(number, {})
    row = {**dict.fromkeys(metadata.FIELDS, ""), **old}
    row.update(number=f"{number:05d}", front=Path(front).name,
               back=Path(back).name if back else "")
    notes, failed = [], False

    # How it was cut out
    if crop_notes is not None:
        notes += [f"crop: {n.replace(';', ',')}" for n in crop_notes]
    else:
        notes += [n for n in _notes(old) if n.startswith("crop:")]

    # Which way up
    if turned is not None:
        row["rotation"] = str(turned.rotation)
        row["faces_found"] = str(turned.faces)
        if turned.status == "disagree":
            notes.append("rotation: the faces and the scene point different ways, check it")
        elif turned.status == "guess":
            notes.append("rotation: not sure which way is up, check it")
        elif turned.status == "no_faces":
            notes.append("rotation: no faces found, left as scanned")
        elif turned.status == "unclear":
            notes.append("rotation: unclear, left as scanned")
    else:
        notes += [n for n in _notes(old) if n.startswith("rotation:")]

    # The back. A blank one is spotted here and not sent.
    was_read = not back or not needs_reading(old)
    if read and back:
        try:
            if settings.get("skip_blank_backs", True) and handwriting.looks_blank(back):
                found = dict(handwriting.NOTHING_WRITTEN)
                notes.append("back: looks blank, was not sent for reading")
            else:
                found = handwriting.read_back(back, settings, retry=retry)
        except handwriting.HandwritingError as e:
            notes.append(f"handwriting: {e}")
            failed = True
            was_read = False
        else:
            was_read = True
            date = metadata.clean_date(found["year"], found["month"], found["day"])
            row.update(handwritten_text=found["handwritten_text"],
                       printed_text=found["printed_text"],
                       date_as_written=found["date_as_written"],
                       date_applied=metadata.format_date(date))
            if not row["place"].strip():        # a place already there was checked by hand
                row["place"] = found["place"]
            if found["legibility"] in ("partial", "illegible"):
                notes.append("handwriting: hard to read, check the caption")
            if date is None and found["year"] is not None:
                notes.append("date: the date read from the back isn't a possible one")
            elif date is None:
                notes.append("date: none found")
            elif found["date_source"] == "printed" or not found["handwritten_text"].strip():
                # Photo labs' faint dot printing is misread more often than
                # not. With no handwriting the date can only be from print,
                # whatever was said about where it came from.
                notes.append("date: read from printing on the back, check it")
    elif was_read:
        notes += [n for n in _notes(old) if n.startswith(("handwriting:", "date:", "back:"))]

    # Into the files
    paths = [p for p in (front, back) if p]
    try:
        date = metadata.parse_date(row["date_applied"])
    except ValueError as e:
        date = None
        notes.append(f"date: {e}")
        failed = True
    try:
        gps = place_position(row, settings, notes)
    except ValueError as e:
        gps = None
        notes.append(f"place: {e}")
        failed = True
    try:
        metadata.write_metadata(paths, row["handwritten_text"], date,
                                scanned_at=scanned_at,
                                exiftool=metadata.find_exiftool(settings),
                                number=number, place=row["place"], gps=gps)
        if date and not metadata.set_file_dates(paths, date, number):
            notes.append("metadata: this drive can't hold a file date that old")
    except metadata.MetadataError as e:
        notes.append(f"metadata: {e}")
        failed = True

    row["status"] = ("error" if failed else "not_read" if not was_read
                     else "needs_review" if notes else "ok")
    row["notes"] = "; ".join(notes)
    metadata.save_row(folder, row)
    return row


def _read_failed(row) -> bool:
    return row.get("status") == "error" and any(
        n.startswith("handwriting:") for n in _notes(row))


def problems(row) -> list:
    """What went wrong with a row whose status is "error"."""
    if row.get("status") != "error":
        return []
    return [n.split(":", 1)[1].strip() for n in _notes(row)
            if n.startswith(("handwriting:", "metadata:")) and "hard to read" not in n]


# --------------------------------------------------------------------------- #
# Background thread for the scanning app
# --------------------------------------------------------------------------- #

class Enricher(threading.Thread):
    """Works through scanned photos one at a time, so a slow or missing
    network connection never holds up the feeder. Reports each finished
    photo with emit("caption", row) and each new kind of trouble with
    emit("warn", text)."""

    def __init__(self, settings: dict, emit):
        super().__init__(daemon=True)
        self.settings = settings
        self.emit = emit
        self.jobs = queue.Queue()
        self.done = 0
        self.failed = 0
        self._warned = set()
        self._struggling = False     # the last read failed: don't wait around

    def submit(self, folder, number, front, back, turned=None, crop_notes=None):
        self.jobs.put((Path(folder), number, front, back, turned, crop_notes,
                       time.time(), bool(self.settings.get("read_handwriting"))))

    def waiting(self) -> int:
        return self.jobs.unfinished_tasks

    def run(self):
        while True:
            (folder, number, front, back, turned, crop_notes,
             scanned_at, read) = self.jobs.get()
            row, crash = None, ""
            try:
                row = enrich_photo(folder, number, front, back, self.settings,
                                   read=read, turned=turned, crop_notes=crop_notes,
                                   scanned_at=scanned_at,
                                   retry=not self._struggling)
            except Exception:
                crash = traceback.format_exc().strip().splitlines()[-1]
            self.jobs.task_done()
            if row is None:
                self.failed += 1
                self.emit("warn", f"Photo {number:05d}: caption step crashed ({crash})")
                continue
            if read:
                self._struggling = _read_failed(row)
            if row["status"] == "error":
                self.failed += 1
                for text in problems(row):
                    if text not in self._warned:
                        self._warned.add(text)
                        self.emit("warn", f"Photo {row['number']}: {text}. "
                                          "Run fastfoto-enrich afterwards to try again.")
            else:
                self.done += 1
            self.emit("caption", row)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _app_settings() -> dict:
    try:
        return json.loads((handwriting.CONFIG_DIR / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def _summary(row) -> str:
    text = " / ".join(row["handwritten_text"].split("\n"))
    if len(text) > 60:
        text = text[:57] + "..."
    parts = [row["number"], f"{row['status']:12s}"]
    if row["date_applied"]:
        parts.append(row["date_applied"])
    if text:
        parts.append(f'"{text}"')
    if row["status"] in ("error", "needs_review") and row["notes"]:
        parts.append(f"[{row['notes']}]")
    return "  ".join(parts)


def apply_csv(folder, settings) -> int:
    """Write every row of captions.csv back into its photo's files. A place
    that is new, or was changed since it was looked up, is looked up (the
    name goes to OpenStreetMap), and the row is updated with what was found."""
    rows = metadata.read_rows(folder)
    if not rows:
        print(f"No {metadata.CSV_NAME} in {folder}, or it lists no photos.")
        return 1
    photos = find_photos(folder)
    exiftool = metadata.find_exiftool(settings)
    written = skipped = 0
    for number in sorted(rows):
        old, files = rows[number], photos.get(number, {})
        row = {**dict.fromkeys(metadata.FIELDS, ""), **old}
        paths = [files[side] for side in ("f", "b") if side in files]
        try:
            if not paths:
                raise ValueError("its files aren't in this folder")
            date = metadata.parse_date(row["date_applied"])
            # Notes about the date and the place are about how things stood
            # before the edit: make them afresh.
            notes = [n for n in _notes(row)
                     if not n.startswith("place:") and not (date and n == "date: none found")]
            gps = place_position(row, settings, notes)
            metadata.write_metadata(paths, row["handwritten_text"], date,
                                    exiftool=exiftool, number=number,
                                    place=row["place"], gps=gps)
            if date and not metadata.set_file_dates(paths, date, number):
                print(f"{number:05d}  note: this drive can't hold a file date that old")
            for note in notes:
                if note.startswith("place:"):
                    print(f"{number:05d}  {note}")
            if row["place_found"] and row["place_found"] != old.get("place_found"):
                print(f"{number:05d}  place: {row['place_found']}")
            row["notes"] = "; ".join(notes)
            if row["status"] in ("ok", "needs_review"):
                row["status"] = "needs_review" if notes else "ok"
            if row != old:
                metadata.save_row(folder, row)
            written += 1
        except (ValueError, metadata.MetadataError) as e:
            print(f"{number:05d}  skipped: {e}")
            skipped += 1
    print(f"{written} photo(s) updated from {metadata.CSV_NAME}"
          + (f", {skipped} skipped" if skipped else ""))
    return 1 if skipped else 0


def main(argv=None) -> int:
    settings = _app_settings()
    ap = argparse.ArgumentParser(
        prog="fastfoto-enrich",
        description="Read the handwriting on the backs of scanned photos and "
                    "write captions and dates into the files.")
    ap.add_argument("folder", nargs="?", default=settings.get("output_dir"),
                    help="folder of scans (default: the app's Save to folder)")
    ap.add_argument("--all", action="store_true",
                    help="read every back again, not just new and failed ones "
                         "(this replaces captions you corrected by hand)")
    ap.add_argument("--apply-csv", action="store_true",
                    help="write captions.csv, as edited, back into the files, "
                         "looking up places that are new or were changed; "
                         "nothing is sent to Gemini")
    ap.add_argument("--sort", action="store_true",
                    help="move photos lying loose in the folder into its "
                         "fronts/ and backs/ folders, and do nothing else "
                         "(unless --apply-csv is given too)")
    ap.add_argument("--turn", nargs="+", metavar=("WHICH_WAY", "PHOTO"),
                    help="turn photos that were saved the wrong way up: right "
                         "(a quarter turn clockwise), left, or 180, then the "
                         "photos' numbers, e.g. --turn 180 78 109 or --turn "
                         "left 80-85. Nothing else is done")
    ap.add_argument("--remove", nargs="+", metavar="PHOTO",
                    help="take photos out, e.g. ones that were scanned again: "
                         "--remove 402 405. Their files are moved to the "
                         "folder's removed/ folder and their rows taken out "
                         "of captions.csv. Nothing else is done")
    ap.add_argument("--model", help="Gemini model to use for this run")
    args = ap.parse_args(argv)

    if not args.folder:
        ap.error("no folder given")
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(f"{folder} is not a folder.")
        return 1
    if args.model:
        settings["gemini_model"] = args.model
    added = metadata.add_missing_columns(folder)
    if added:
        print(f"Added to {metadata.CSV_NAME}: the column(s) {', '.join(added)}.")
    if args.sort:
        moved, skipped = sort_photos(folder)
        print(f"{moved} file(s) moved into {folder / SIDE_FOLDERS['f']} and "
              f"{folder / SIDE_FOLDERS['b']}"
              + (f"; {skipped} left where they are, because a file of the same "
                 "name is already there" if skipped else ""))
        if not args.apply_csv:
            return 1 if skipped else 0
    if not metadata.find_exiftool(settings):
        print("exiftool isn't installed: sudo apt install libimage-exiftool-perl")
        return 1
    if args.remove:
        try:
            numbers = photo_numbers(args.remove)
        except ValueError as e:
            print(e)
            return 1
        failed = 0
        for number in numbers:
            try:
                moved = remove_photo(folder, number)
                print(f"{number:05d}  taken out, {moved} file(s) moved to "
                      f"{folder / REMOVED_FOLDER}")
            except (ValueError, OSError) as e:
                print(f"{number:05d}  not taken out: {e}")
                failed += 1
        return 1 if failed else 0
    if args.turn:
        try:
            if args.turn[0].lower() not in TURNS or len(args.turn) < 2:
                raise ValueError("--turn takes right, left or 180 and then the "
                                 "photos' numbers: --turn left 78 109")
            degrees = TURNS[args.turn[0].lower()]
            numbers = photo_numbers(args.turn[1:])
        except ValueError as e:
            print(e)
            return 1
        failed, turned = 0, []
        for number in numbers:
            try:
                turn_photo(folder, number, degrees, settings)
                print(f"{number:05d}  turned {args.turn[0].lower()}")
                turned += find_photos(folder).get(number, {}).values()
            except (ValueError, metadata.MetadataError) as e:
                print(f"{number:05d}  not turned: {e}")
                failed += 1
        show_changes(turned)
        return 1 if failed else 0
    if args.apply_csv:
        return apply_csv(folder, settings)

    rows = metadata.read_rows(folder)
    jobs = []           # (number, files, read the back?)
    for number, files in sorted(find_photos(folder).items()):
        if "f" not in files:
            continue
        row = rows.get(number)
        if "b" in files and (args.all or needs_reading(row)):
            jobs.append((number, files, True))
        elif row is None or row.get("status") == "error":
            jobs.append((number, files, False))
    if not jobs:
        print(f"Nothing to do in {folder}: every photo has been read.")
        return 0
    if any(read for _, _, read in jobs):
        problem = handwriting.setup_problem()
        if problem:
            print("Can't read handwriting: " + problem)
            return 1

    counts = {}
    struggling = False
    for number, files, read in jobs:
        # A photo scanned before captions existed has no scan time recorded;
        # its file time still is the scan time.
        scanned_at = None if number in rows else files["f"].stat().st_mtime
        row = enrich_photo(folder, number, files["f"], files.get("b"), settings,
                           read=read, scanned_at=scanned_at, retry=not struggling)
        if read:
            struggling = _read_failed(row)
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        print(_summary(row), flush=True)
    print(", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
          + f". Details in {folder / metadata.CSV_NAME}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
