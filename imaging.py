"""Image loading, capture time and sharpness scoring (runs inside worker processes)."""
import io
from datetime import datetime
from pathlib import Path

import cv2
import exifread
import imagehash
import numpy as np
from PIL import Image, ImageOps

RAW_EXT = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".orf", ".raf", ".rw2",
           ".dng", ".pef", ".srw", ".3fr", ".iiq", ".x3f"}
JPG_EXT = {".jpg", ".jpeg"}

WORK_DIR = ".framekeeper"
LEGACY_WORK_DIRS = (".cull",)  # names used before the app was called Framekeeper
SCORE_EDGE = 3000    # long edge every image is resized to before scoring, so scores are comparable
PREVIEW_EDGE = 2400  # review page large preview / gallery
THUMB_EDGE = 480     # review page grid thumbnail
GRID = 8             # sharpness is measured on a GRID x GRID tile grid
TOP_FRACTION = 0.1   # score = mean of the sharpest 10% of tiles (the in-focus subject)
CROP_SIZE = 640      # 100% crop shown in the review page


def migrate_work_dir(folder):
    """Rename a folder's working dir from an old app name, keeping its decisions and caches."""
    new = Path(folder) / WORK_DIR
    if new.exists():
        return
    for name in LEGACY_WORK_DIRS:
        old = Path(folder) / name
        if (old / "decisions.json").exists():
            old.rename(new)
            return


def load_image(path_jpg, path_raw):
    """Full-resolution RGB image: the JPG if there is one, else the RAW's embedded preview."""
    if path_jpg:
        img = ImageOps.exif_transpose(Image.open(path_jpg))
        return img.convert("RGB")

    try:
        import rawpy
    except ImportError:
        raise RuntimeError("RAW without a matching JPG needs rawpy (pip install rawpy)") from None
    with rawpy.imread(str(path_raw)) as raw:
        flip = raw.sizes.flip
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            # postprocess() already applies the orientation
            return Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))
    if thumb.format == rawpy.ThumbFormat.JPEG:
        img = Image.open(io.BytesIO(thumb.data)).convert("RGB")
    else:
        img = Image.fromarray(thumb.data)
    # embedded previews are stored unrotated; LibRaw's flip tells us how to turn them
    if flip == 3:
        img = img.rotate(180)
    elif flip == 5:
        img = img.rotate(90, expand=True)
    elif flip == 6:
        img = img.rotate(-90, expand=True)
    return img


def exif_time(path):
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return None
    dt = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
    if not dt:
        return None
    try:
        t = datetime.strptime(str(dt).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return None
    sub = "".join(c for c in str(tags.get("EXIF SubSecTimeOriginal", "")) if c.isdigit())
    if sub:
        t += float("0." + sub)
    return t


def capture_time(paths):
    """EXIF DateTimeOriginal (+ subseconds) of the first file that has one, else mtime."""
    for p in paths:
        t = exif_time(p)
        if t:
            return t, "exif"
    return paths[0].stat().st_mtime, "mtime"


def sharpness(gray):
    """Tile-based Laplacian variance.

    Returns (score, focus) where focus is the centre of the sharpest tile as
    fractions of width/height - used to cut the 100% comparison crop.
    """
    gray = cv2.GaussianBlur(gray, (3, 3), 0)  # damp sensor noise, which also has high Laplacian variance
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    h, w = gray.shape
    th, tw = h // GRID, w // GRID
    tiles = []
    for r in range(GRID):
        for c in range(GRID):
            tiles.append((float(lap[r * th:(r + 1) * th, c * tw:(c + 1) * tw].var()), r, c))
    tiles.sort(reverse=True)
    k = max(1, int(len(tiles) * TOP_FRACTION))
    score = float(np.mean([v for v, _, _ in tiles[:k]]))
    _, r, c = tiles[0]
    return score, [(c + 0.5) / GRID, (r + 0.5) / GRID]


def resize_long_edge(img, edge):
    w, h = img.size
    s = edge / max(w, h)
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)


def analyze_shot(folder, shot):
    """Worker: score one shot and write its review previews. Returns the enriched shot dict."""
    cv2.setNumThreads(1)
    folder = Path(folder)
    jpg = folder / shot["jpg"] if shot["jpg"] else None
    raw = folder / shot["raw"] if shot["raw"] else None
    try:
        img = load_image(jpg, raw)
    except Exception as e:
        return {**shot, "error": f"{type(e).__name__}: {e}"}

    work = folder / WORK_DIR
    resize_long_edge(img, PREVIEW_EDGE).save(work / "previews" / f"{shot['id']}.jpg", quality=85)
    small = resize_long_edge(img, THUMB_EDGE)
    small.save(work / "thumbs" / f"{shot['id']}.jpg", quality=80)

    gray = cv2.cvtColor(np.asarray(resize_long_edge(img, SCORE_EDGE)), cv2.COLOR_RGB2GRAY)
    score, focus = sharpness(gray)
    t, t_src = capture_time([p for p in (jpg, raw) if p])
    return {
        **shot,
        "time": t,
        "time_source": t_src,
        "phash": str(imagehash.phash(small)),
        "score": score,
        "focus": focus,
        "size": list(img.size),
    }


def render_full(folder, shot, out_path):
    """Full-resolution JPG for shots without a camera JPG (RAW embedded preview)."""
    img = load_image(None, Path(folder) / shot["raw"])
    img.save(out_path, quality=95)


def render_crop(folder, shot, focus, out_path):
    """Cut a CROP_SIZE x CROP_SIZE 100% crop of `shot` centred on `focus` (fractions of w/h).

    The review page passes the group winner's focus point for every shot in the
    group, so the crops show the same region side by side.
    """
    folder = Path(folder)
    img = load_image(folder / shot["jpg"] if shot["jpg"] else None,
                     folder / shot["raw"] if shot["raw"] else None)
    w, h = img.size
    cx, cy = focus[0] * w, focus[1] * h
    half = CROP_SIZE // 2
    left = int(min(max(cx - half, 0), max(w - CROP_SIZE, 0)))
    top = int(min(max(cy - half, 0), max(h - CROP_SIZE, 0)))
    img.crop((left, top, left + CROP_SIZE, top + CROP_SIZE)).save(out_path, quality=92)
