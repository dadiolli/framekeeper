"""Apply review decisions without deleting anything.

move: every file of a rejected shot (RAW, JPG, existing sidecars sharing the
      basename) goes to <folder>/_rejects/. Logged to .framekeeper/moves.json for --undo.
xmp:  writes <basename>.xmp sidecars next to RAW files with a colour label
      (keep = Green, reject = Red). Lightroom keeps pick/reject flags in its
      catalog only, so labels are the portable option; filter on them after
      "Metadata > Read Metadata from Files". JPG-only shots are skipped
      (Lightroom ignores sidecars for JPGs) - use move for those.

cleanup (after move, before importing into Lightroom): moves the JPGs of kept
      RAW+JPG shots, the _rejects folder and the preview caches to the macOS
      Trash, leaving only the kept RAW files (plus sidecars and JPG-only keepers).
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from imaging import WORK_DIR, migrate_work_dir

REJECT_DIR = "_rejects"
CACHE_DIRS = ("previews", "thumbs", "crops", "full")  # inside .framekeeper; decisions.json is kept
KEEP_LABEL, REJECT_LABEL = "Green", "Red"
OWN_XMP_TOOLKITS = ("framekeeper", "photo_cull")  # sidecars we wrote (also under the old name) may be rewritten

XMP = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="framekeeper">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Label="{label}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def shot_files(folder, shot):
    """All top-level files belonging to a shot: DSC1.ARW, DSC1.JPG, DSC1.xmp, DSC1.ARW.xmp, ..."""
    prefix = shot["id"] + "."
    return sorted(p for p in folder.iterdir() if p.is_file() and p.name.startswith(prefix))


def apply(folder, dec, mode, dry_run):
    shots = [s for g in dec["groups"] for s in g["shots"]]
    if mode == "move":
        move_rejects(folder, [s for s in shots if not s["keep"]], dry_run)
    elif mode == "xmp":
        write_labels(folder, shots, dry_run)


def move_rejects(folder, rejects, dry_run):
    dest = folder / REJECT_DIR
    log_path = folder / WORK_DIR / "moves.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else []
    moved = skipped = 0
    if not dry_run:
        dest.mkdir(exist_ok=True)
    for s in rejects:
        for src in shot_files(folder, s):
            dst = dest / src.name
            if dst.exists():
                print(f"  ! {dst} already exists, leaving {src.name} in place")
                skipped += 1
                continue
            if dry_run:
                print(f"  would move {src.name} -> {REJECT_DIR}/")
            else:
                shutil.move(src, dst)
                log.append({"src": str(src), "dst": str(dst), "at": datetime.now().isoformat(timespec="seconds")})
            moved += 1
    if not dry_run:
        log_path.write_text(json.dumps(log, indent=1))
    verb = "would move" if dry_run else "moved"
    print(f"Apply (move): {verb} {moved} files of {len(rejects)} rejected shots to {dest}"
          + (f", {skipped} skipped" if skipped else ""))


def write_labels(folder, shots, dry_run):
    written = existing = jpg_only = 0
    for s in shots:
        if not s["raw"]:
            jpg_only += 1
            continue
        xmp = folder / (s["id"] + ".xmp")
        if xmp.exists() and not any(f'x:xmptk="{m}"' in xmp.read_text(errors="ignore") for m in OWN_XMP_TOOLKITS):
            # don't clobber develop settings from Lightroom/Capture One - prototype limitation
            # (our own sidecars are rewritten, so re-applying after another review updates the labels)
            print(f"  ! {xmp.name} exists, not touching it")
            existing += 1
            continue
        label = KEEP_LABEL if s["keep"] else REJECT_LABEL
        if dry_run:
            print(f"  would write {xmp.name}: {label}")
        else:
            xmp.write_text(XMP.format(label=label), encoding="utf-8")
        written += 1
    verb = "would write" if dry_run else "wrote"
    print(f"Apply (xmp): {verb} {written} sidecars ({KEEP_LABEL}=keep, {REJECT_LABEL}=reject)"
          + (f", {existing} existing sidecars left alone" if existing else "")
          + (f", {jpg_only} JPG-only shots skipped" if jpg_only else ""))


def undo(folder, dry_run):
    migrate_work_dir(folder)
    dec_path = folder / WORK_DIR / "decisions.json"
    log_path = folder / WORK_DIR / "moves.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else []
    if not log:
        print("Nothing to undo.")
        return
    left = []
    for m in reversed(log):
        src, dst = m["src"], m["dst"]
        if dry_run:
            print(f"  would restore {dst} -> {src}")
            continue
        try:
            if not os.path.exists(src):
                shutil.move(dst, src)
                continue
            print(f"  ! {src} exists again, leaving {dst}")
        except OSError as e:
            print(f"  ! {dst}: {e}")
        left.append(m)
    if dry_run:
        return
    log_path.write_text(json.dumps(list(reversed(left)), indent=1))
    if dec_path.exists():
        dec = json.loads(dec_path.read_text())
        dec["applied"] = False
        dec_path.write_text(json.dumps(dec, indent=1))
    print(f"Restored {len(log) - len(left)} files" + (f", {len(left)} could not be restored" if left else ""))


def move_to_trash(paths):
    """Move files/folders to the system trash (restorable), never delete them.

    macOS 15+: the built-in `trash` command. Windows (Recycle Bin), Linux (freedesktop.org
    trash, as used by GNOME/KDE) and older macOS: the Send2Trash package.
    """
    paths = [str(p) for p in paths]
    if not paths:
        return
    if sys.platform == "darwin" and Path("/usr/bin/trash").exists():
        for i in range(0, len(paths), 200):  # keep the command line short
            r = subprocess.run(["/usr/bin/trash", *paths[i:i + 200]], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"could not move files to the Trash: {(r.stderr or r.stdout).strip()}")
        return
    try:
        from send2trash import send2trash
    except ImportError:
        raise RuntimeError("moving files to the trash needs the Send2Trash package (pip install Send2Trash)") from None
    send2trash(paths)


def cleanup_plan(folder, dec):
    """What cleanup would move to the Trash and what stays."""
    folder = Path(folder)
    if dec.get("applied") not in (True, "move"):
        raise ValueError("cleanup is only for folders whose rejects were moved to _rejects/ (apply mode 'move')")
    kept = [s for g in dec["groups"] for s in g["shots"] if s["keep"]]
    rejects = folder / REJECT_DIR
    work = folder / WORK_DIR
    return {
        # only the JPG twin of a kept RAW goes; a keeper that exists only as JPG stays
        "jpgs": [folder / s["jpg"] for s in kept if s["jpg"] and s["raw"] and (folder / s["jpg"]).exists()],
        "rejects": rejects if rejects.is_dir() else None,
        "rejects_files": sum(1 for p in rejects.rglob("*") if p.is_file()) if rejects.is_dir() else 0,
        "caches": [work / d for d in CACHE_DIRS if (work / d).exists()],
        "raws_kept": sum(1 for s in kept if s["raw"] and (folder / s["raw"]).exists()),
        "jpg_only_kept": sorted(s["jpg"] for s in kept if s["jpg"] and not s["raw"]),
    }


def plan_summary(plan):
    return {k: (len(v) if isinstance(v, list) else v) for k, v in plan.items() if k != "rejects"} | {
        "has_rejects": plan["rejects"] is not None, "jpg_only_kept": plan["jpg_only_kept"]}


def cleanup(folder, dec, dry_run):
    """Trash the JPG twins, _rejects and caches. Returns True if something was moved."""
    folder = Path(folder)
    plan = cleanup_plan(folder, dec)
    items = plan["jpgs"] + plan["caches"] + ([plan["rejects"]] if plan["rejects"] else [])
    verb = "would move" if dry_run else "moved"
    print(f"Cleanup: {verb} to the Trash: {len(plan['jpgs'])} JPGs of kept RAW shots"
          + (f", the {REJECT_DIR} folder ({plan['rejects_files']} files)" if plan["rejects"] else "")
          + (", the preview cache" if plan["caches"] else ""))
    print(f"  kept: {plan['raws_kept']} RAW files" + (f" + {len(plan['jpg_only_kept'])} JPG-only keepers" if plan["jpg_only_kept"] else ""))
    if dry_run or not items:
        return False
    move_to_trash(items)
    moves = folder / WORK_DIR / "moves.json"
    if moves.exists():
        moves.write_text("[]")  # _rejects is in the Trash now, so there is nothing to undo
    dec["cleaned"] = datetime.now().isoformat(timespec="seconds")
    print("  Everything can be restored from the Trash (Finder: Put Back).")
    return True
