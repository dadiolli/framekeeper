"""Scoring pipeline shared by the CLI and the web app.

scan -> pair RAW+JPG -> score sharpness -> group near-duplicates -> propose (best per group).
The result lives in <folder>/.framekeeper/decisions.json and is updated with every review click.
"""
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import imagehash

from imaging import JPG_EXT, RAW_EXT, WORK_DIR, analyze_shot, migrate_work_dir

DEFAULT_PARAMS = {"time_gap": 2.0, "hash_dist": 12, "margin": 0.10}


def dec_path(folder):
    return Path(folder) / WORK_DIR / "decisions.json"


def load(folder):
    migrate_work_dir(folder)
    p = dec_path(folder)
    return json.loads(p.read_text()) if p.exists() else None


def save(folder, dec):
    p = dec_path(folder)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dec, indent=1))
    tmp.replace(p)


def scan(folder):
    """Pair files by basename: DSC01234.ARW + DSC01234.JPG -> one shot."""
    shots = {}
    for p in sorted(Path(folder).iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        kind = "raw" if ext in RAW_EXT else "jpg" if ext in JPG_EXT else None
        if kind:
            shots.setdefault(p.stem, {"id": p.stem, "raw": None, "jpg": None})[kind] = p.name
    return list(shots.values())


def analyze(folder, shots, workers, progress=None):
    """Score all shots in parallel. progress(done, total) is called as results come in."""
    for sub in ("previews", "thumbs", "crops"):
        (Path(folder) / WORK_DIR / sub).mkdir(parents=True, exist_ok=True)
    results, failed = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(analyze_shot, str(folder), s) for s in shots]
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            (failed if "error" in r else results).append(r)
            if progress:
                progress(i, len(futures))
    return results, failed


def group(shots, time_gap, hash_dist):
    """Consecutive shots that are close in time AND look alike form one group.

    Each shot is compared to the previous one (not the first of the group) so
    slow pans/drifting bursts still chain together.
    """
    shots = sorted(shots, key=lambda s: (s["time"], s["id"]))
    groups, cur = [], []
    for s in shots:
        if cur:
            prev = cur[-1]
            close = s["time"] - prev["time"] <= time_gap
            similar = imagehash.hex_to_hash(s["phash"]) - imagehash.hex_to_hash(prev["phash"]) <= hash_dist
            if close and similar:
                cur.append(s)
                continue
            groups.append(cur)
        cur = [s]
    if cur:
        groups.append(cur)
    return groups


def propose(groups, margin):
    out = []
    for i, g in enumerate(groups):
        g = sorted(g, key=lambda s: -s["score"])
        best = g[0]
        for s in g:
            s["keep"] = s is best
            s["rel"] = s["score"] / best["score"] if best["score"] > 0 else 0.0
        m = (best["score"] - g[1]["score"]) / best["score"] if len(g) > 1 and best["score"] > 0 else 1.0
        out.append({"id": i, "shots": g, "margin": m, "uncertain": len(g) > 1 and m < margin})
    return out


def build(folder, params, workers, progress=None):
    """Score a folder from scratch and write decisions.json. Raises ValueError if there is nothing to score."""
    folder = Path(folder)
    migrate_work_dir(folder)
    shots = scan(folder)
    if not shots:
        raise ValueError("no RAW or JPG files found")
    scored, failed = analyze(folder, shots, workers, progress)
    if not scored:
        raise ValueError(f"none of the {len(shots)} shots could be read (first error: {failed[0]['error']})")
    dec = {
        "folder": str(folder),
        "created": datetime.now().isoformat(timespec="seconds"),
        "params": params,
        "reviewed": False,
        "applied": False,  # False, or the apply mode ("move"/"xmp") once applied
        "skipped": [{"file": r["jpg"] or r["raw"], "error": r["error"]} for r in failed],
        "groups": propose(group(scored, params["time_gap"], params["hash_dist"]), params["margin"]),
    }
    save(folder, dec)
    return dec


def regroup(dec, time_gap, hash_dist, apply=False):
    """Group the already-scored shots again with other thresholds (no rescoring needed).

    Stacks that come out exactly as before keep the user's choices and "seen" state;
    stacks that change get a fresh proposal. With apply=False nothing is modified and
    only the resulting counts are returned (live preview).
    """
    shots = [s for g in dec["groups"] for s in g["shots"]]
    old = {frozenset(s["id"] for s in g["shots"]): g for g in dec["groups"]}
    new = group(shots, time_gap, hash_dist)
    changed = sum(frozenset(s["id"] for s in g) not in old for g in new)
    result = {"stacks": len(new), "near_dup_groups": sum(len(g) > 1 for g in new), "changed": changed}
    if not apply:
        return result
    keep_before = {s["id"]: s["keep"] for s in shots}
    groups = propose(new, dec["params"]["margin"])
    for g in groups:
        prev = old.get(frozenset(s["id"] for s in g["shots"]))
        if prev:
            for s in g["shots"]:
                s["keep"] = keep_before[s["id"]]
            if prev.get("seen"):
                g["seen"] = True
    dec["groups"] = groups
    dec["params"] = {**dec["params"], "time_gap": time_gap, "hash_dist": hash_dist}
    return result


def summary(dec):
    groups = dec["groups"]
    shots = [s for g in groups for s in g["shots"]]
    keep = sum(s["keep"] for s in shots)
    return {
        "shots": len(shots),
        "stacks": len(groups),
        "near_dup_groups": sum(len(g["shots"]) > 1 for g in groups),
        "uncertain": sum(g["uncertain"] for g in groups),
        "seen": sum(bool(g.get("seen")) for g in groups),
        "keep": keep,
        "reject": len(shots) - keep,
        "mtime_only": sum(s.get("time_source") == "mtime" for s in shots),
        "skipped": len(dec.get("skipped", [])),
    }


def summary_lines(dec):
    s = summary(dec)
    lines = [f"  {s['shots']} shots, {s['stacks']} stacks ({s['near_dup_groups']} with near-duplicates), "
             f"{s['uncertain']} uncertain, {s['keep']} keep / {s['reject']} reject"]
    if s["mtime_only"]:
        lines.append(f"  note: {s['mtime_only']} shots had no EXIF capture time; file mtime was used for grouping")
    for k in dec.get("skipped", []):
        lines.append(f"  ! skipped {k['file']}: {k['error']}")
    return lines
