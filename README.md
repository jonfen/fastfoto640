# FastFoto Scan: Epson FastFoto FF-640 on Linux

Epson doesn't ship Linux drivers for the FastFoto FF-640 photo scanner. This
is a small app that drives it directly over USB. You drop a stack of
photos or banknotes in the feeder, it scans both sides, and it writes
cropped and straightened JPEGs into a folder, much like Epson's FastFoto
software does on Windows and macOS.

It was written to feed [Dollar Detective](#use-with-dollar-detective)
(fancy serial-number spotting on US $1 bills), but it works for anything the
feeder takes. For [photos](#scanning-photos) it can also turn each one the
right way up and store what is written on the back as the photo's caption
and date.

## Features

- **Starts by itself** when you put paper in the feeder (or click *Scan now*).
- **Both sides in one pass**, saved as `fronts/00001f.jpg` and
  `backs/00001b.jpg`, numbered across sessions, so the fronts can be looked
  through or uploaded on their own. (Or `FastFoto_00001.jpg` and
  `FastFoto_00001_b.jpg` in one folder; pick under *File names*.)
- **Photos turned upright.** Works out which way up each photo goes, from
  the scene (sky, ground, buildings, furniture) and from the faces in it,
  and turns it, and its back, to match.
- **Handwriting on the back becomes the caption.** The back is read by
  Google's Gemini, and the text, the date and the place are written into
  both image files, where photo programs look for them. Blank backs are
  spotted on your computer and not sent.
- **Complete edges.** It turns on the scanner's overscan, so every page has
  background on all four sides, even when an item feeds in crooked.
- **Auto-crop and straighten.** Items that feed rotated or slanted are
  squared up. An adjustable margin (Tight / Normal / Loose) keeps a little
  background around each one. The two sides of a sheet are checked against
  each other, so a photo whose sand or sky is the grey of the scanner's
  background isn't cut short.
- **300 or 600 dpi.** About 0.8 s per bill, both sides, at 300 dpi.
- Files are written under a temporary name and then renamed, so a program
  watching the folder never picks up a half-written image.

## Install

Tested on Ubuntu 24.04 with Python 3.12.

```sh
sudo apt install python3-tk python3-venv libimage-exiftool-perl
git clone https://github.com/jonfen/fastfoto640.git
cd fastfoto640
./install.sh
```

`install.sh` creates a virtualenv, downloads the two models for turning
photos upright (77 MB, into `models/`), installs a udev rule so you can use
the scanner without root (this step asks for `sudo`), and adds **FastFoto Scan** to your app menu. Plug the scanner
in, switch it on, and start the app. After updating, run `./install.sh`
again.

`libimage-exiftool-perl` (exiftool) is only needed for writing captions and
dates into photos.

## Scanning photos

Choose a folder under *Save to*, set *File names* to `00001f.jpg / 00001b.jpg`,
and load a stack face down. Each photo becomes two files with the same
number, in two folders inside the one you chose:

```
Save to folder/
    fronts/00001f.jpg, 00002f.jpg, ...    the pictures
    backs/00001b.jpg, 00002b.jpg, ...     their backs, for reference
    captions.csv                          what was read, one row per photo
```

`fronts` is the folder to browse, share or upload. What was written on a
back is in the front's caption (any photo program's *Info* or *Properties*
shows it), in `captions.csv`, and to see the writing itself, open the file
with the same number in `backs`.

Photos scanned by an earlier version lie loose in the folder. They still
work; `./fastfoto-enrich --sort` moves them into `fronts` and `backs`.

### Turn photos upright

With this ticked, the app works out which way up the front goes, and turns
it. The back is turned to match, so both sides keep the same top edge. This
runs on your computer; nothing is sent anywhere.

It goes by two things. One is the scene: a model trained on pictures turned
all four ways, which knows that sky is above ground, how houses, rooms and
furniture stand, and so on. It is what turns landscapes, buildings and
interiors. The other is the faces in the photo, found with the photo turned
each of the four ways. The scene decides when it is sure, and the faces
decide when it isn't.

Two kinds of photo are turned and also marked `needs_review` in
`captions.csv`, and named in the app's log, so you can look at them:

- the faces and the scene point different ways (the face finder sometimes
  sees a face in wallpaper or leaves; the photo is turned as the scene says);
- the scene isn't sure and there are no faces (a sunset, a close-up of
  flowers, a pattern); the photo is turned by the best guess.

On the 374 family photos from the 1970s and 80s this was tried on, each fed
all four ways, every one came out upright; six were marked for a look. Other
photos will not all go as well: photos taken looking straight up or down,
and pictures with no up or down in them, are the likely misses.

To turn a photo yourself:

```sh
./fastfoto-enrich --turn 180 78 109      # photos 78 and 109 are upside down
./fastfoto-enrich --turn right 80-85     # 80 to 85 need a quarter turn clockwise
./fastfoto-enrich --turn left 91         # 91 needs a quarter turn anticlockwise
```

`right` and `left` say which way the picture is turned: if the top of the
picture is at the left of the screen, it needs `--turn right`. (`90` and
`270`, degrees clockwise, mean the same.)

The front is turned as you look at it and the back is turned to match. The
pictures are saved again with their captions, dates and places as they were.
A file manager that has the folder open shows the turned pictures a few
seconds after the command ends (the command takes two seconds longer to
make that happen: it moves each file's time on by a second and back, which
is what a file manager goes by; reloading the folder does nothing).

### When a picture is the colour of the scanner

The scanner's background is grey, and so is a lot of sand, sky and water. An
edge of a photo that can't be told from the background is found from the
back of the same sheet, which is plain paper and always easy to see. When
that moves an edge by more than a little, the app says so in its log and in
the `notes` column of `captions.csv` (`crop: ...`), so you can look at that
photo. Scan both sides to get this check.

### Read handwriting on backs

With this ticked, every back with something written on it is sent to
Google's Gemini API, which returns the handwriting, any printed or stamped
text, the date if there is one, and the place if one is named. **Those back
images leave your computer**, and Google may charge a small amount per
image. Fronts are never sent.

A back with nothing on it but the paper maker's watermark is recognised on
your computer and not sent. Its row in `captions.csv` says so (`back: looks
blank, was not sent for reading`). The check leans towards sending: faint
pencil counts as writing. To send every back regardless, set
`skip_blank_backs` to `false` in the [settings](#settings).

It needs an API key from [Google AI Studio](https://aistudio.google.com/apikey).
Save it in a file:

```sh
mkdir -p ~/.config/fastfoto640
nano ~/.config/fastfoto640/gemini_api_key     # paste the key, save
chmod 600 ~/.config/fastfoto640/gemini_api_key
```

(A `GEMINI_API_KEY` environment variable works too, but an app started from
the menu doesn't see variables set in your shell.)

If you keep the key in 1Password, put a reference to it in that file instead
of the key, so the key itself is never stored on disk:

```sh
echo "op://Private/Google AI Studio - FastFoto/credential" > ~/.config/fastfoto640/gemini_api_key
```

The reference is `op://` followed by the vault, the item and the field. This
needs the [1Password CLI](https://www.1password.dev/cli/) (`op`) and, in the
1Password app, *Settings > Developer > Integrate with 1Password CLI* turned
on. The app asks 1Password for the key when it starts with handwriting
reading ticked, 1Password asks you to approve, and the key is kept in memory
until the app closes.

Reading happens in the background while scanning carries on. If the
connection is down, scanning still works; run `fastfoto-enrich` afterwards.

### Where the caption, date and place are stored

Inside both image files of the photo, written by exiftool without
recompressing the picture:

| What | Field |
|------|-------|
| Handwritten text | EXIF `ImageDescription`, XMP `dc:description`, IPTC `Caption-Abstract` |
| Date of the photo | EXIF `DateTimeOriginal` and XMP `photoshop:DateCreated` |
| Place | EXIF GPS latitude and longitude, and the name in XMP `iptcCore:Location` |
| When it was scanned | EXIF `CreateDate` (DateTimeDigitized) |

The file's modified time is set to the photo's date as well. Linux has no
way to set a file's creation time.

If only the year, or the year and month, is written on the back, the XMP
date holds just that (`1974` or `1974-07`), while the EXIF date, which must
be complete, uses the 1st (`1974:07:01`). If there is no handwritten date, a
date stamped by the photo lab is used. If there is no date at all, the photo
gets no date rather than the day it was scanned.

A print never says what time it was taken, but `DateTimeOriginal` has to
hold a time. Each photo gets noon plus one second per photo number
(`12:00:07` for photo 7): noon, so that a program applying a time zone
doesn't push the photo into the day before, and the seconds, so that photos
with the same date stay in the order they were scanned.

All-number dates like 3/4/65 are read as month/day/year, and two-digit years
as 19xx. Both can be changed in the [settings](#settings).

Often the only thing on a back is a year in two figures. With an apostrophe
(`'84`) it is always taken for the year. Without one it is taken for the
year from 40 up (`84`), and for a frame or album number, which gives no
date, below that (`12`).

**Dates printed by the photo lab are often misread.** Many labs printed the
date in faint dots, and what comes back for those is wrong more often than
right, sometimes by years. So a date found on a back with no handwriting is
applied, but the photo is marked `needs_review` (`date: read from printing
on the back, check it`). Prints from one roll carry the same date, so the
quick way to put a run of them right is to read one yourself (the dots show
up when you turn the contrast of the back's scan right up), fill the
`date_applied` column down in a spreadsheet and run
`fastfoto-enrich --apply-csv`.

### Places

If a back names a place ("Craters of the Moon"), it is put in the `place`
column of `captions.csv`. With *Put places on the map* ticked, the place is
looked up with OpenStreetMap as soon as the back has been read, and its
position is written into the photo's files, so photo programs can show the
photo on a map. Only the place's name is sent, once per name, never the
photo or its caption.

Three columns of `captions.csv` hold this:

| Column | What it holds |
|--------|---------------|
| `place` | The place as written on the back, or as you typed it. |
| `gps` | Its position, as `43.14144, -113.49251` (or `none`, to keep the photo off the map). |
| `place_found` | What the lookup matched, so you can check it: `Craters of the Moon -> Craters of the Moon National Preserve, Idaho, United States (43.14144, -113.49251)` |

A few words on the back of a photo often fit more than one place, so
**check the `place_found` column**. Of two places with the same name, the
one in your own country is taken, unless the other is much better known
("Paris" is the one in France). To put one right, edit `captions.csv` and
run `fastfoto-enrich --apply-csv`:

- **Wrong match:** make `place` more exact (`Charlotte, Vermont`). It is
  looked up again because the place has changed.
- **Not a place at all** (Charlotte was a person): empty the `place` cell.
- **You know the exact spot:** put its position in `gps`, as a map gives it
  when you copy a spot's coordinates. A position you typed is never replaced.
- **A place too large or too vague to pin down** (a canal 184 miles long,
  "the lake"): put `none` in `gps`. The photo keeps the name of the place
  and stays off the map.
- **A photo with no place on its back:** type one into `place`.

A place that couldn't be found is noted in the row (`place: ... was not
found on the map`), and the photo is marked `needs_review`. Abbreviations
are the usual reason ("Teton N.P."): write the name out in `place`
(`Grand Teton National Park`). A match that is only a street or a building
is not accepted.

### captions.csv

The output folder also gets a `captions.csv` with one row per photo: the
handwritten text, the printed text, the date as written, the date applied,
the place, its position and what it was matched to, how far the photo was
rotated, how many faces were found, and a status:

| Status | Meaning |
|--------|---------|
| `ok` | Done. |
| `needs_review` | Done, but the `notes` column says what to check: an edge that was hard to see, handwriting that was hard to read, a blank back, no date found, a date read from printing, a place that wasn't found, or a turn it wasn't sure of. |
| `not_read` | The back hasn't been sent for reading. |
| `error` | Reading or writing failed; `fastfoto-enrich` tries again. |

To correct a caption, date or place, edit `handwritten_text`, `date_applied`
(as `1974`, `1974-07` or `1974-07-04`) or `place` in a spreadsheet, save as
CSV, and run `fastfoto-enrich --apply-csv`. That is also how to give a date
to a photo that has none written on it.

### fastfoto-enrich

```sh
./fastfoto-enrich [FOLDER]               # read backs not read yet, retry failures
./fastfoto-enrich --all [FOLDER]         # read every back again
./fastfoto-enrich --apply-csv [FOLDER]   # write an edited captions.csv into the files
./fastfoto-enrich --sort [FOLDER]        # move loose photos into fronts/ and backs/
./fastfoto-enrich [FOLDER] --turn 180 78 109   # turn photos saved the wrong way up
./fastfoto-enrich [FOLDER] --remove 402 405    # take out photos that were scanned again
```

`--remove` moves a photo's two files into a `removed` folder inside the
output folder, rather than deleting them, and takes its row out of
`captions.csv`. The number is not used again. Delete the `removed` folder
yourself when you are sure. Use `--turn` and `--remove` with the app closed
or idle: the app is still writing to photos that are waiting for their
caption.

Without `FOLDER` it uses the app's *Save to* folder. It also works on photos
scanned before these options existed. `--all` replaces captions and dates
you corrected by hand (a place you typed in is kept); `--apply-csv` sends
nothing to Google, only new or changed place names to OpenStreetMap (and
not those, if *Put places on the map* is unticked). `--sort` can be given
together with `--apply-csv`.

### Putting the photos in Google Photos

Upload the `fronts` folder only. Google Photos sorts by the date inside
each file, puts photos that have a position on its map, and shows the
caption in a photo's info panel (under *Other*). Before uploading, give the
photos with no date one in `captions.csv` (even just a year) and run
`fastfoto-enrich --apply-csv`, or they are filed under the day they were
scanned. Photos tend to be scanned in the order they were kept, so an
undated one between two of the same year is often from that year too; look
at it first, because loose prints get mixed. The same goes the other way: a
date that doesn't fit the photos either side of it is worth a look at the
back. Changes made in Google Photos afterwards stay in Google Photos; they
are not written back to your files.

## Use with Dollar Detective

Point *Save to* at Dollar Detective's Monitor folder and set *File names* to
`FastFoto_00001.jpg / _b.jpg`, which is the `NNNN.jpg` + `NNNN_b.jpg` naming
Dollar Detective already pairs. If Dollar Detective is installed when the app
first starts, the folder and the naming are set that way by default. Leave the
photo options unticked. With this naming all files stay together in the
one folder.

## How it works

| File | What it does |
|------|--------------|
| `esci2.py` | Minimal driver for Epson's ESC/I-2 scanner protocol over USB (pyusb): status, settings, feeding, and image transfer. Pages come over as JPEG made by the scanner itself. |
| `billcrop.py` | Finds the item on the grey background, fits a line to each of its four edges, checks the two sides of a sheet against each other, and maps the corners onto an upright rectangle (OpenCV). |
| `fastfoto_scan.py` | The Tk app: watches the feeder, runs a scan, crops in a background thread, pairs fronts with backs, and writes the files. |
| `orient.py` | Works out which way up a photo goes. Asks an image orientation model ([Duarte Barbosa's](https://github.com/duartebarbosadev/deep-image-orientation-detection), EfficientNetV2, MIT licence) about the photo at each of the four quarter turns and adds up its answers; finds faces with OpenCV's YuNet detector at the same four turns; and weighs the two. Both run through OpenCV. |
| `handwriting.py` | Tells blank backs from written ones, sends a written one to Gemini and gets the handwriting, printed text, date and place back as structured data. |
| `metadata.py` | Writes caption, dates and place into the files with exiftool, sets the file time, and keeps `captions.csv`. |
| `places.py` | Looks up where a named place is, with OpenStreetMap's Nominatim. |
| `fastfoto_enrich.py` | Ties the last three together: the app's background caption thread, and the `fastfoto-enrich` command. |

Why not SANE? SANE's `epsonds` backend can talk to the FF-640 if you add its
USB ID (`usb 0x04b8 0x015f` in `/etc/sane.d/epsonds.conf`), but it never asks
for overscan. Without overscan the scanner ends each page a few lines before
the paper does, so a corner of a crooked item is lost. The protocol details
here were worked out from the sane-backends `epsonds` source and by talking to
the scanner. No SANE code is included.

## Settings

Settings live in `~/.config/fastfoto640/settings.json` and the log in
`~/.cache/fastfoto640/fastfoto.log`. Most are set from the app. These can
only be set by editing the file (with the app closed):

| Setting | Default | What it does |
|---------|---------|--------------|
| `gemini_model` | `gemini-3.8-flash` | The Gemini model that reads the backs. |
| `gemini_thinking` | `low` | How long the model may think about a back: `minimal`, `low`, `medium` or `high`, or `""` to leave it to the model. More thinking is slower and costs more, and did not read faint printing any better. |
| `date_order` | `MDY` | How an all-number date is read: `MDY` or `DMY`. |
| `century` | `1900` | The century of a two-digit year. |
| `skip_blank_backs` | `true` | Don't send a back to Gemini when nothing is written on it. |
| `home_country` | from your system's language setting | Two-letter code (`us`, `gb`, `de`) of the country preferred when a place name fits several places. `""` for no preference. |
| `scan_length_mm` | none | Longest page to scan. By default photos get the scanner's full 14 inches and bills 120 mm. |
| `exiftool` | none | Path of exiftool, if it isn't on your `PATH`. |
| `keep_raw_dir` | none | A folder where every uncropped page is saved as well, to debug cropping. |

## Limitations

- Only tested with the FF-640 (USB ID `04b8:015f`). Other Epson sheet-fed
  scanners that speak ESC/I-2 might work with a changed product ID; that's
  untested.
- Colour only, 300/600 dpi. Epson's photo enhancement, red-eye removal and
  restoration are not implemented.
- Bills are assumed to feed long-edge-first. Items must be under 14 inches
  long.
- Turning photos upright is right most of the time, not always, and a wrong
  turn it was sure of is not marked. Look through the fronts after a stack
  and put the odd one right with `fastfoto-enrich --turn`.
- The check for blank backs looks for handwriting and dark print. Faint lab
  printing alone (a frame number, a dotted date) can pass as blank.
- An edge that blends into the scanner's grey background can only be put
  right when both sides are scanned. Scanned one side at a time, such a
  photo can come out short.
- Handwriting recognition makes mistakes, especially with names. Check the
  rows marked `needs_review`, and anything that matters to you.
- A year written on its own can be read upside down (`'85` as `'58`) or
  with a figure wrong. Both are years, so nothing marks the photo.
- A file's modified time can't go back before 1980 on FAT or exFAT drives.
  The dates inside the file are not affected.

## License

[GNU AGPL v3 or later](LICENSE). Not affiliated with or endorsed by Epson.
"FastFoto" and "Epson" are trademarks of Seiko Epson Corporation.
