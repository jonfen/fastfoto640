# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a scanned photo the right way up.

A photo can go through the feeder in any of four directions. Two things on
the front say which way is up, and both are looked at on this computer:

The scene. A neural network trained on pictures turned all four ways tells
how much further a picture has to be turned: it goes by sky and ground,
buildings, furniture, people, whatever is there. We show it the photo at
each of the four quarter turns and put its four answers together, which is
steadier than one look.

The faces. A face detector (YuNet, through OpenCV) is run at each of the
four quarter turns and the turn where the faces score best is kept. The
detector also finds upside-down faces, just less confidently (roughly
0.6-0.9 instead of 0.9+), so plain "which turn has the most faces" is a coin
toss on close-up portraits. What separates the turns is how far each face is
above a confidence floor, added up over two image sizes: the small size
rarely finds a wrong-way-up face at all, the larger one finds the small faces
in group shots. People standing well back have faces too small for those two
sizes, so a photo where nobody was found gets a second look at two larger
sizes.

The scene decides when it is sure. The faces decide when it isn't, and they
are a check on it when it is: the detector sees faces in wallpaper and
foliage now and then, so a photo where the two point different ways is
turned as the scene says and reported. A photo with neither a sure scene nor
faces is turned by the scene's best guess and reported too.

Either model can be missing (install.sh downloads both); the other is then
used alone. With faces alone, a photo without any is left as scanned.
"""

import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

MODELS_DIR = Path(__file__).resolve().parent / "models"

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_PATH = MODELS_DIR / MODEL_NAME
# Pinned to one commit of opencv_zoo, so the checksum below keeps matching.
MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/"
             "48b10fa505aeb51fbd127fc60a5c660040e823b7/"
             "models/face_detection_yunet/" + MODEL_NAME)
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

# Duarte Barbosa's image orientation model (EfficientNetV2-S, MIT licence),
# https://github.com/duartebarbosadev/deep-image-orientation-detection
SCENE_NAME = "orientation_model_v2_0.9882.onnx"
SCENE_PATH = MODELS_DIR / SCENE_NAME
SCENE_URL = ("https://github.com/duartebarbosadev/deep-image-orientation-detection/"
             "releases/download/v2/" + SCENE_NAME)
SCENE_SHA256 = "cffe911c1dff47fbfbbd90110aaab9c07134645c460d35b3ae8832079bea91ba"

# (what it is, where it goes, where it comes from, checksum, rough size)
MODELS = (("face", MODEL_PATH, MODEL_URL, MODEL_SHA256, "0.2 MB"),
          ("scene", SCENE_PATH, SCENE_URL, SCENE_SHA256, "77 MB"))

SIZES = (320, 640)     # long side, in pixels, of the copies searched for faces
SMALL_FACE_SIZES = (960, 1280)   # ...and of the second look, if nobody was found
FLOOR = 0.75           # faces below this confidence count for nothing
MIN_SCORE = 0.1        # the best turn needs at least this much to be believed
CLEAR_RATIO = 0.6      # ...and the runner-up must score under this share of it

SCENE_SIZE = 384       # the picture the scene model was trained on, in pixels
SCENE_BORDER = 16      # ...cut from a copy this much larger all round
# The share of the scene model's vote the winning turn needs. On 374 family
# photos from the 1970s and 80s, each tried all four ways, every photo but
# two got 0.91 or more (and the right turn); those two got 0.71 and 0.76.
SCENE_SURE = 0.85
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)      # RGB, as trained
_STD = np.array([0.229, 0.224, 0.225], np.float32)

_QUARTER_TURNS = (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180,
                  cv2.ROTATE_90_COUNTERCLOCKWISE)


class OrientError(Exception):
    pass


class Result(NamedTuple):
    rotation: int      # degrees clockwise the front was turned: 0, 90, 180, 270
    faces: int         # faces found the right way up in that direction
    # "upright" or "rotated": settled.
    # "disagree": turned, but the faces and the scene point different ways.
    # "guess": turned by the scene's best guess; it wasn't sure, and no faces.
    # "no_faces" or "unclear": left as scanned (only without the scene model).
    status: str


def turn(img, quarter_turns: int):
    """Rotate by a number of clockwise quarter turns (negative = the other way)."""
    k = quarter_turns % 4
    return img if k == 0 else cv2.rotate(img, _QUARTER_TURNS[k - 1])


def missing_models() -> list:
    """Which of "face" and "scene" haven't been downloaded."""
    return [what for what, path, *_ in MODELS if not path.exists()]


_detector = None
_scene = None


def _get_detector():
    """The face detector, or None if its model isn't there."""
    global _detector
    if _detector is None and MODEL_PATH.exists():
        try:
            _detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (320, 320),
                                                  FLOOR, 0.3, 5000)
        except cv2.error as e:
            raise OrientError(f"couldn't load the face model: {e}")
    return _detector


def _get_scene():
    """The scene model, or None if it isn't there."""
    global _scene
    if _scene is None and SCENE_PATH.exists():
        try:
            _scene = cv2.dnn.readNet(str(SCENE_PATH))
        except cv2.error as e:
            raise OrientError(f"couldn't load the scene model: {e}")
    return _scene


def _fit(img, long_side):
    h, w = img.shape[:2]
    scale = long_side / max(h, w)
    if scale >= 1:
        return img
    return cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))),
                      interpolation=cv2.INTER_AREA)


def _upright_faces(det, img):
    """(score, count) of faces that are the right way up in this image."""
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    score, count = 0.0, 0
    if faces is None:
        return score, count
    for f in faces:
        # Each row: box (4), right eye, left eye, nose, mouth corners (2 each),
        # confidence. Upright means the mouth is below the eyes.
        eyes = (f[4:6] + f[6:8]) / 2
        mouth = (f[10:12] + f[12:14]) / 2
        dx, dy = mouth - eyes
        if dy > abs(dx):
            score += float(f[14]) - FLOOR
            count += 1
    return score, count


def _by_faces(det, img):
    """What the faces say: (quarter turns, faces at each turn, ""), or
    (None, faces at each turn, "no_faces" or "unclear") if they don't."""
    counts = [0] * 4
    for sizes in (SIZES, SMALL_FACE_SIZES):
        scores = [0.0] * 4
        counts = [0] * 4
        for size in sizes:
            small = _fit(img, size)
            for k in range(4):
                s, n = _upright_faces(det, turn(small, k))
                scores[k] += s
                counts[k] = max(counts[k], n)
        order = sorted(range(4), key=lambda k: scores[k], reverse=True)
        best, second = order[0], order[1]
        if scores[best] < MIN_SCORE:
            continue
        if scores[second] > CLEAR_RATIO * scores[best]:
            return None, counts, "unclear"
        return best, counts, ""
    return None, counts, "no_faces"


def _by_scene(net, img):
    """What the scene says: (quarter turns, its share of the vote, 0 to 1)."""
    # The model wants a square, whatever the photo's shape: squash it, as in
    # its training, and cut the border off. A square can be turned afterwards.
    edge = SCENE_SIZE + 2 * SCENE_BORDER
    small = cv2.resize(img, (edge, edge), interpolation=cv2.INTER_AREA)
    small = small[SCENE_BORDER:-SCENE_BORDER, SCENE_BORDER:-SCENE_BORDER]
    small = (cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255 - _MEAN) / _STD
    try:
        net.setInput(np.stack([np.ascontiguousarray(turn(small, k)).transpose(2, 0, 1)
                               for k in range(4)]))
        out = net.forward().astype(np.float64)
    except cv2.error as e:
        raise OrientError(f"the scene model couldn't be run: {e}")
    shares = np.exp(out - out.max(axis=1, keepdims=True))
    shares /= shares.sum(axis=1, keepdims=True)
    # Row k is about the photo after k turns, and its column c says "c more
    # turns to go": a vote for the photo itself needing k + c.
    votes = [float(np.mean([shares[k][(need - k) % 4] for k in range(4)]))
             for need in range(4)]
    best = max(range(4), key=votes.__getitem__)
    return best, votes[best]


def find_rotation(img) -> Result:
    """Work out how the front of a photo should be turned. Doesn't change it."""
    det, net = _get_detector(), _get_scene()
    if det is None and net is None:
        raise OrientError(f"its models are not in {MODELS_DIR}; run ./install.sh again")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    faces, counts, trouble = _by_faces(det, img) if det else (None, [0] * 4, "no_faces")
    if net is None:
        if faces is None:
            return Result(0, counts[0], trouble)
        return Result(faces * 90, counts[faces], "rotated" if faces else "upright")
    scene, share = _by_scene(net, img)
    sure = share >= SCENE_SURE
    k = scene if sure or faces is None else faces
    if faces is not None and faces != scene:
        status = "disagree"
    elif not sure and faces is None:
        status = "guess"
    else:
        status = "rotated" if k else "upright"
    return Result(k * 90, counts[k], status)


def upright_pair(front, back=None):
    """Return (front, back, Result) with both sides turned so the front is
    the right way up.

    The back is turned the opposite way, so it keeps the same top edge of the
    paper as the front: the back is seen from the other side, where a
    clockwise turn of the front is an anticlockwise one."""
    result = find_rotation(front)
    k = result.rotation // 90
    return turn(front, k), (None if back is None else turn(back, -k)), result


# --------------------------------------------------------------------------- #
# Model download (used by install.sh)
# --------------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(what, path, url, sha256, size) -> bool:
    if path.exists() and _sha256(path) == sha256:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    print(f"Downloading the {what} model ({size})...")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        print(f"Couldn't download the {what} model: {e}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False
    if _sha256(tmp) != sha256:
        print(f"The downloaded {what} model doesn't match its checksum; not using it.",
              file=sys.stderr)
        tmp.unlink()
        return False
    tmp.replace(path)
    return True


def fetch_models() -> bool:
    """Download the models that aren't there yet. True if both now are."""
    return all([_fetch(*model) for model in MODELS])


if __name__ == "__main__":
    if sys.argv[1:] in (["--fetch-models"], ["--fetch-model"]):
        sys.exit(0 if fetch_models() else 1)
    for path in sys.argv[1:]:
        im = cv2.imread(path)
        if im is None:
            print(path, "can't be read")
            continue
        r = find_rotation(im)
        print(f"{path}: {r.status}, turn {r.rotation} degrees clockwise, {r.faces} face(s)")
