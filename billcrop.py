# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Barros
"""Find a bill (or photo) on the FF-640's grey background, straighten it and crop it.

The scanner returns a fixed-width strip: grey background around the item, and
when the item is shorter than the scan window the backend pads the rest with
pure white. We trim the padding and separate the item from the grey by colour.

A bill that feeds crooked doesn't come out as a neatly rotated rectangle: it
also drifts sideways while it feeds, so the scan is slightly slanted (a
parallelogram). So instead of fitting one rotated rectangle, we fit a straight
line to each of the four edges (using only the middle of each edge, so torn or
dog-eared corners don't throw it off), intersect them to get the four corners,
and map those corners onto an upright rectangle.

Photos add a problem bills don't have: sand, sky or water at the edge of a
picture can be almost the colour of the scanner's background, so parts of an
edge can't be told from it. Two things deal with that. Each edge is taken to
be the straight line that most of the edge agrees on while next to nothing
lies outside it (a picture that fades into the background only ever makes the
item look smaller, never bigger). And when both sides of a sheet were
scanned, they are compared: they are the same piece of paper, so if one side
came out smaller, the edge of it that fell short is moved out to match the
other side.
"""

import cv2
import numpy as np


def _trim_white_padding(img):
    """Drop the all-white rows the backend appends after the paper ends."""
    not_white = (img < 250).any(axis=2).any(axis=1)
    rows = np.flatnonzero(not_white)
    if rows.size == 0:
        return img
    return img[: rows[-1] + 1]


def _edge_band(w):
    """Columns at the far left/right to ignore: with overscan the scanner
    returns a few dark columns outside its sensor area."""
    return max(2, int(w * 0.025))


def _background_colour(img):
    """Median colour of strips just inside the left/right edges (always
    background: the feed is wider than a bill and items are centre-fed)."""
    w = img.shape[1]
    a = _edge_band(w)
    b = a + max(4, w // 100)
    edges = np.concatenate([img[:, a:b].reshape(-1, 3), img[:, w - b:w - a].reshape(-1, 3)])
    return np.median(edges, axis=0)


def _item_mask(img, min_area_frac=0.02):
    """Filled mask of the largest non-background blob, plus its contour."""
    h, w = img.shape[:2]
    bg = _background_colour(img).astype(np.float32)
    diff = np.abs(img.astype(np.float32) - bg).max(axis=2)
    mask = (diff > 22).astype(np.uint8) * 255
    band = _edge_band(w)
    mask[:, :band] = 0
    mask[:, w - band:] = 0

    k = max(3, w // 300)
    # Pad with background first: OpenCV's erosion treats outside-the-image as
    # foreground, which smears a bill whose corner touches the top edge (the
    # scan starts right where the paper starts) along that edge.
    p = k * 5
    mask = cv2.copyMakeBorder(mask, p, p, p, p, cv2.BORDER_CONSTANT, value=0)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k * 5, k * 5), np.uint8))
    mask = mask[p:-p, p:-p]

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < min_area_frac * h * w:
        return None, None
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [c], -1, 255, thickness=cv2.FILLED)
    return filled > 0, c


def find_item(img):
    """Return the item's rotated rect ((cx, cy), (w, h), angle) or None."""
    _, c = _item_mask(img)
    return None if c is None else cv2.minAreaRect(c)


def _robust_line(t, v):
    """Fit v = a*t + b, dropping outliers (wavy or nicked edge spots).
    Returns (a, b) or None."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    if t.size < 10:
        return None
    keep = np.ones(t.size, bool)
    for _ in range(3):
        a, b = np.polyfit(t[keep], v[keep], 1)
        res = np.abs(v - (a * t + b))
        mad = np.median(res[keep]) + 1e-6
        new_keep = res <= max(2.0, 3.0 * mad)
        if new_keep.sum() < 10 or (new_keep == keep).all():
            break
        keep = new_keep
    return a, b


MAX_SLOPE = 0.2        # steepest edge we fit (about 11 degrees); beyond that, the fallback
NEAR = 0.012           # samples within this share of the edge's length of a line belong to it
SOLID = 0.6            # an edge seen along this share of its length is trusted
SIZE_TOLERANCE = 0.01  # front and back may differ this much before we look closer
SIZE_WARNING = 0.02    # ...and this much before it's worth telling the user
NOT_BACKGROUND = 6.0   # typical distance from the background colour that says "not background"

EDGES = ("top", "bottom", "left", "right")
_PAIRS = (("left", "right"), ("top", "bottom"))    # the edges that set the width, the height
_ENDS = {"top": (0, 1), "right": (1, 2), "bottom": (3, 2), "left": (0, 3)}  # corners of each edge


def _edge_line(t, v, outward, slope=None):
    """Fit v = a*t + b to one edge of the item.

    v[i] is where the item starts at position t[i], coming from outside;
    `outward` is -1 if outside is towards smaller v (top, left), +1 otherwise.
    With `slope` given, only the position of the line is fitted.

    Where the picture is the colour of the background, the item seems to
    start further in, at no particular line, and a fit through all the
    samples gets dragged inwards and tilted. But a picture that blends into
    the background can only make the item look smaller, never bigger. So the
    edge is the straight band that holds the most samples while having next
    to none outside it, and the line is fitted through the samples of that
    band only. (The band is wide enough to hold the soft shadow that runs
    along a real edge.)

    Returns (a, b, support): support is the share of the samples that lie
    along the line, a measure of how much of the edge was really seen."""
    flip = -outward
    t = np.asarray(t, np.float64)
    v = np.asarray(v, np.float64) * flip        # from here on, outside = smaller
    n = t.size
    if n < 10:
        return None
    mid = t.mean()
    tc = t - mid
    half = max(float(np.abs(tc).max()), 1.0)
    near = max(4.0, NEAR * 2 * half)
    # Try every slope, a few pixels apart at the ends of the edge, and count
    # the samples in each band along it.
    if slope is None:
        slopes = np.linspace(-MAX_SLOPE, MAX_SLOPE,
                             2 * int(MAX_SLOPE * half / (near / 4)) + 1)
    else:
        slopes = np.array([slope * flip], np.float64)
    r = v[None, :] - slopes[:, None] * tc[None, :]
    lo = float(r.min())
    cell = near / 2
    cells = ((r - lo) / cell).astype(np.int64)
    width = int(cells.max()) + 2
    flat = cells + np.arange(slopes.size)[:, None] * width
    counts = np.bincount(flat.ravel(), minlength=slopes.size * width)
    counts = counts.reshape(slopes.size, width)
    inside = counts[:, :-1] + counts[:, 1:]             # bands two cells wide
    outside = np.cumsum(counts, axis=1)[:, :-1] - counts[:, :-1]
    inside = np.where(outside <= max(2, 0.05 * n), inside, 0)
    si, ci = np.unravel_index(np.argmax(inside), inside.shape)
    if inside[si, ci] < 10:
        return None
    a = float(slopes[si])
    b = lo + (ci + 1) * cell
    in_band = np.abs(v - (a * tc + b)) <= near
    if slope is None:
        line = _robust_line(tc[in_band], v[in_band])
        if line is None:
            return None
        a, b = line
    else:
        b = float(np.median(v[in_band] - a * tc[in_band]))
    support = float((np.abs(v - (a * tc + b)) <= near).mean())
    return float(a) * flip, float(b - a * mid) * flip, support


class Outline:
    """The four edges of an item as straight lines.

    top/bottom are y = a*x + b, left/right are x = a*y + b. `support` says,
    per edge, what share of the edge was actually seen on that line (0 for an
    edge that was cut off by the scan window and had to be assumed)."""

    def __init__(self, lines, support):
        self.lines = dict(lines)
        self.support = dict(support)

    def corners(self):
        """TL, TR, BR, BL as float32 (4, 2)."""
        def meet(hl, vl):
            ah, bh = hl
            av, bv = vl
            y = (ah * bv + bh) / (1 - ah * av)
            return av * y + bv, y
        ln = self.lines
        return np.array([meet(ln["top"], ln["left"]), meet(ln["top"], ln["right"]),
                         meet(ln["bottom"], ln["right"]), meet(ln["bottom"], ln["left"])],
                        np.float32)

    def size(self):
        """(width, height) in pixels."""
        tl, tr, br, bl = self.corners()
        return (float(np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2,
                float(np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2)

    def weakest(self, axis):
        """The less certain of the two edges that set the width (axis 0) or
        the height (axis 1), and its support."""
        name = min(_PAIRS[axis], key=lambda e: self.support[e])
        return name, self.support[name]

    def moved(self, edge, target):
        """A copy with `edge` moved, parallel to the edge opposite it, so
        that the width (for left, right) or the height (top, bottom) becomes
        `target`."""
        axis = 0 if edge in _PAIRS[0] else 1
        first, second = _PAIRS[axis]
        other = second if edge == first else first
        sign = -1.0 if edge == first else 1.0
        a, b = self.lines[other]
        gap = target
        for _ in range(2):      # size is proportional to the gap between the lines
            lines = dict(self.lines)
            lines[edge] = (a, b + sign * gap)
            out = Outline(lines, self.support)
            gap *= target / max(out.size()[axis], 1.0)
        return out


def find_outline(img, mask=None):
    """Fit the item's four edges. Returns an Outline, or None if the item or
    its left and right edges can't be found. (`mask` is the item's mask, if
    the caller already has it.)"""
    if mask is None:
        mask, _ = _item_mask(img)
    if mask is None:
        return None
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    bw, bh = x1 - x0, y1 - y0

    # Top/bottom edges: sample columns across the middle 80% of the width.
    cols = np.arange(int(x0 + 0.1 * bw), int(x1 - 0.1 * bw), 3)
    colmask = mask[:, cols]
    has = colmask.any(axis=0)
    top = np.argmax(colmask, axis=0)
    bot = h - 1 - np.argmax(colmask[::-1], axis=0)
    # Drop samples sitting on the image border: there the bill was cut off by
    # the scan window, so the real edge is somewhere beyond it.
    t_ok = has & (top > 0)
    b_ok = has & (bot < h - 1)

    # Left/right edges: sample rows across the middle 70% of the height.
    rows = np.arange(int(y0 + 0.15 * bh), int(y1 - 0.15 * bh), 2)
    rowmask = mask[rows, :]
    rhas = rowmask.any(axis=1)
    left = np.argmax(rowmask, axis=1)
    right = w - 1 - np.argmax(rowmask[:, ::-1], axis=1)
    l_ok = rhas & (left > 0)
    r_ok = rhas & (right < w - 1)

    samples = {"top": (cols[t_ok], top[t_ok], -1),         # y = a*x + b
               "bottom": (cols[b_ok], bot[b_ok], +1),
               "left": (rows[l_ok], left[l_ok], -1),       # x = a*y + b
               "right": (rows[r_ok], right[r_ok], +1)}
    fits = {e: _edge_line(*samples[e]) for e in EDGES}
    # Opposite edges of a sheet are parallel. Where one of a pair was seen
    # much less well than the other, its direction is the shakier of the two,
    # so it takes the other's and only its position is fitted.
    for one, two in (("top", "bottom"), ("left", "right")):
        if fits[one] is None or fits[two] is None:
            continue
        weak, strong = (one, two) if fits[one][2] < fits[two][2] else (two, one)
        if fits[strong][2] - fits[weak][2] > 0.1:
            fits[weak] = _edge_line(*samples[weak], slope=fits[strong][0]) or fits[weak]
    if fits["left"] is None or fits["right"] is None:
        return None
    # A long edge that's entirely cut off: assume it's parallel to the other
    # one and lies on the image border (the best we can do; what's past the
    # border was never scanned).
    if fits["top"] is None and fits["bottom"] is None:
        return None
    if fits["top"] is None:
        a = fits["bottom"][0]
        fits["top"] = (a, 0.0 - a * (x0 + x1) / 2, 0.0)
    if fits["bottom"] is None:
        a = fits["top"][0]
        fits["bottom"] = (a, (h - 1) - a * (x0 + x1) / 2, 0.0)
    return Outline({e: fits[e][:2] for e in EDGES}, {e: fits[e][2] for e in EDGES})


def find_corners(img):
    """Return the item's four corners (TL, TR, BR, BL) as float32 (4, 2),
    or None if the edges can't be fitted."""
    outline = find_outline(img)
    return None if outline is None else outline.corners()


def _rotated_rect_crop(img, margin_px, bg):
    """Fallback: crop the blob's minimum-area rotated rectangle."""
    rect = find_item(img)
    if rect is None:
        return None
    (cx, cy), (rw, rh), angle = rect
    # Straighten by the small angle only, so the item stays the way round it
    # went through the scanner; each quarter turn we take off the angle swaps
    # which side of the rectangle is its width.
    while angle > 45:
        angle -= 90
        rw, rh = rh, rw
    while angle < -45:
        angle += 90
        rw, rh = rh, rw
    out_w = int(round(rw)) + 2 * margin_px
    out_h = int(round(rh)) + 2 * margin_px
    if out_w <= 0 or out_h <= 0:
        return None
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    m[0, 2] += out_w / 2 - cx
    m[1, 2] += out_h / 2 - cy
    return cv2.warpAffine(img, m, (out_w, out_h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=bg)


def _corners_plausible(q, img, rect=None):
    """Reject fits that came out twisted or far off the blob. (`rect` is
    find_item's answer, if the caller already has it.)"""
    tl, tr, br, bl = q
    wt, wb = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    hl, hr = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    if min(wt, wb, hl, hr) < 20:
        return False
    if abs(wt - wb) > 0.05 * max(wt, wb) or abs(hl - hr) > 0.08 * max(hl, hr):
        return False
    if rect is None:
        rect = find_item(img)
    if rect is None:
        return False
    area_rect = rect[1][0] * rect[1][1]
    area_quad = cv2.contourArea(q.reshape(-1, 1, 2))
    return 0.85 * area_rect <= area_quad <= 1.15 * area_rect


def _usable_outline(img):
    """The item's outline, or None if it can't be fitted or doesn't hold up."""
    mask, contour = _item_mask(img)     # the slow part: done once, used twice
    if mask is None:
        return None
    outline = find_outline(img, mask)
    if outline is not None and _corners_plausible(outline.corners(), img,
                                                  cv2.minAreaRect(contour)):
        return outline
    return None


def _crop(img, outline, margin_px):
    """Cut the item out of a padding-trimmed scan: along its outline if there
    is one, otherwise by the fallback. Returns (image, found)."""
    bg = tuple(float(v) for v in _background_colour(img))
    if outline is not None:
        out_w, out_h = (int(round(v)) for v in outline.size())
        m = margin_px
        dst = np.array([[m, m], [m + out_w - 1, m], [m + out_w - 1, m + out_h - 1],
                        [m, m + out_h - 1]], np.float32)
        H = cv2.getPerspectiveTransform(outline.corners(), dst)
        # Anything outside the scan is filled with the background grey (not
        # smeared edge pixels), so a cut-off edge looks like what it is.
        out = cv2.warpPerspective(img, H, (out_w + 2 * m, out_h + 2 * m),
                                  flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=bg)
        return out, True
    out = _rotated_rect_crop(img, margin_px, bg)
    if out is None:
        return img, False
    return out, True


def crop_item(img, margin_px=0):
    """Return (cropped_image, found). If nothing is found, returns the
    padding-trimmed scan unchanged and found=False.

    margin_px > 0 keeps that much background around the item, so a slightly
    misjudged edge is kept rather than trimmed; margin_px < 0 trims inward."""
    img = _trim_white_padding(img)
    return _crop(img, _usable_outline(img), margin_px)


def _strip_difference(img, bg, before, after, edge):
    """How unlike the background the scan is between an edge's old place and
    its new one: the typical (median) distance of the strip's pixels from the
    background colour. 0 if the strip lies outside the scan."""
    i, j = _ENDS[edge]
    old, new = before.corners(), after.corners()
    quad = np.array([old[i], old[j], new[j], new[i]])
    h, w = img.shape[:2]
    x0, y0 = (int(max(0, np.floor(v))) for v in quad.min(axis=0))
    x1 = int(min(w, np.ceil(quad[:, 0].max()) + 1))
    y1 = int(min(h, np.ceil(quad[:, 1].max()) + 1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    inside = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillConvexPoly(inside, np.round(quad - (x0, y0)).astype(np.int32), 1)
    px = img[y0:y1, x0:x1][inside > 0]
    if px.shape[0] < 100:
        return 0.0
    px = px[::max(1, px.shape[0] // 200000)].astype(np.float32)
    return float(np.median(np.abs(px - bg).max(axis=1)))


def _edge_to_move(img, outline, axis, target):
    """Which edge of an outline that came out too small should move out to
    make it `target` wide (axis 0) or long (axis 1). None if there's nothing
    to go on.

    First choice: the end beyond which the scan shows something that isn't
    quite background (the part of the picture that was taken for it).
    Second: an edge that was barely seen in the first place."""
    bg = _background_colour(img).astype(np.float32)
    looks = {e: _strip_difference(img, bg, outline, outline.moved(e, target), e)
             for e in _PAIRS[axis]}
    most, least = sorted(looks, key=looks.get, reverse=True)
    if looks[most] >= NOT_BACKGROUND and looks[most] >= 1.5 * looks[least]:
        return most
    edge, support = outline.weakest(axis)
    return edge if support < SOLID else None


def _match_sizes(outlines, images):
    """Compare the outlines of the two sides of one sheet ({"front": ...,
    "back": ...}, and their scans likewise). Where one side came out smaller
    than the other, find the edge that fell short and move it out to the
    other side's size. Returns (outlines, notes)."""
    notes = []
    sides = dict(outlines)
    for axis, what in ((0, "width"), (1, "length")):
        sizes = {k: o.size()[axis] for k, o in sides.items()}
        small = min(sizes, key=sizes.get)
        big = "back" if small == "front" else "front"
        if sizes[small] >= sizes[big] * (1 - SIZE_TOLERANCE):
            continue
        edge = _edge_to_move(images[small], sides[small], axis, sizes[big])
        if edge is None:
            # Nothing says which side is right, so neither is changed. (The
            # feed can stretch one side a little, mostly on the first sheet.)
            if sizes[small] < sizes[big] * (1 - SIZE_WARNING):
                notes.append(f"the {small} came out {1 - sizes[small] / sizes[big]:.0%} "
                             f"smaller in {what} than the {big}, check both")
            continue
        sides[small] = sides[small].moved(edge, sizes[big])
        if sizes[small] < sizes[big] * (1 - SIZE_WARNING):    # small moves aren't news
            notes.append(f"the {edge} edge of the {small} was hard to see, so its {what} "
                         f"was taken from the {big}")
    return sides, notes


def crop_pair(front, back, margin_px=0):
    """Crop the two sides of one sheet, using each to check the other.

    Returns (front, back, notes). notes lists, in plain words, anything about
    the crop that deserves a look; it is empty when all went well. A side
    whose item can't be found comes back as its padding-trimmed scan."""
    front = _trim_white_padding(front)
    back = _trim_white_padding(back)
    images = {"front": front, "back": back}
    outlines = {name: _usable_outline(img) for name, img in images.items()}
    notes = []
    if outlines["front"] is not None and outlines["back"] is not None:
        outlines, notes = _match_sizes(outlines, images)
    out = {}
    for name, img in images.items():
        out[name], found = _crop(img, outlines[name], margin_px)
        if not found:
            notes.append(f"couldn't find the edges of the {name}, kept the full scan")
        elif outlines[name] is None:
            notes.append(f"the edges of the {name} were hard to find, check its crop")
    return out["front"], out["back"], notes


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        im = cv2.imread(path)
        out, ok = crop_item(im)
        dst = path.rsplit(".", 1)[0] + "_crop.jpg"
        cv2.imwrite(dst, out, [cv2.IMWRITE_JPEG_QUALITY, 95])
        print(path, "->", dst, out.shape[1], "x", out.shape[0], "found" if ok else "NOT FOUND")
