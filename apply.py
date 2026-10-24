"""Apply review decisions without deleting anything.

move: every file of a rejected shot (RAW, JPG, existing sidecars sharing the
      basename) goes to <folder>/_rejects/. Logged to .framekeeper/moves.json for --undo.
xmp:  writes <basename>.xmp sidecars next to RAW files with a colour label
      (keep = Green, reject = Red). Lightroom keeps pick/reject flags in its
      catalog only, so labels are the portable option; filter on them after
      "Metadata > Read Metadata from Files". JPG-only shots are skipped
      (Lightroom ignores sidecars for JPGs) - use move for those.
"""
import json
import os
import shutil
from datetime import datetime

from imaging import WORK_DIR, migrate_work_dir

REJECT_DIR = "_rejects"
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
