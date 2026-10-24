#!/usr/bin/env python3
"""Framekeeper: keep the sharpest shot of every burst of near-duplicates in a RAW+JPG shoot folder.

Pipeline: scan -> pair RAW+JPG -> score sharpness -> group near-duplicates ->
propose (best per group) -> review in the browser (default) -> apply.

Without a folder, the browser opens a start page to choose or resume a folder.
Nothing is deleted. Rejects are moved to <folder>/_rejects (undo with --undo)
or tagged with XMP colour labels. Working files live in <folder>/.framekeeper.

Examples:
    python framekeeper.py                          # start page
    python framekeeper.py ~/Pictures/shoot         # open this folder right away
    python framekeeper.py ~/Pictures/shoot --no-review --dry-run
    python framekeeper.py ~/Pictures/shoot --undo
    python framekeeper.py ~/Pictures/shoot --cleanup --dry-run   # RAWs only, before Lightroom
"""
import argparse
import os
import sys
from pathlib import Path

import apply as applier
import pipeline
import review


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", help="shoot folder containing RAW and/or JPG files (omit for the start page)")
    ap.add_argument("--no-review", action="store_true", help="skip the browser and apply the proposal directly")
    ap.add_argument("--apply", choices=review.APPLY_MODES, default="move",
                    help="move: rejects -> _rejects/ (default); xmp: colour labels in XMP sidecars; none: only write decisions.json")
    ap.add_argument("--dry-run", action="store_true", help="print what would be applied without touching files")
    ap.add_argument("--undo", action="store_true", help="move everything from a previous --apply move back")
    ap.add_argument("--cleanup", action="store_true",
                    help="after --apply move: move the JPGs of kept RAW shots, _rejects/ and the preview cache to the trash")
    ap.add_argument("--rescan", action="store_true", help="ignore existing .framekeeper/decisions.json (and your review edits) and start over")
    ap.add_argument("--time-gap", type=float, default=pipeline.DEFAULT_PARAMS["time_gap"],
                    help="max seconds between shots in one group (default 2.0)")
    ap.add_argument("--hash-dist", type=int, default=pipeline.DEFAULT_PARAMS["hash_dist"],
                    help="max perceptual-hash distance between neighbours in a group, 0-64 (default 12)")
    ap.add_argument("--margin", type=float, default=pipeline.DEFAULT_PARAMS["margin"],
                    help="flag a group as uncertain if #2 scores within this fraction of #1 (default 0.10)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    params = {k: getattr(args, k) for k in ("time_gap", "hash_dist", "margin")}

    folder = None
    if args.folder:
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            sys.exit(f"not a folder: {folder}")
    elif args.undo or args.no_review or args.cleanup:
        sys.exit("--undo, --cleanup and --no-review need a folder")

    if args.undo:
        applier.undo(folder, args.dry_run)
    elif args.cleanup:
        dec = pipeline.load(folder)
        if not dec:
            sys.exit("this folder has not been scored")
        try:
            if applier.cleanup(folder, dec, args.dry_run):
                pipeline.save(folder, dec)
        except (ValueError, RuntimeError) as e:
            sys.exit(f"Cleanup not done: {e}")
    elif args.no_review:
        run_without_review(folder, params, args)
    else:
        review.serve(folder, params, args.workers, args.apply, args.dry_run, args.port, args.rescan)


def run_without_review(folder, params, args):
    dec = None if args.rescan else pipeline.load(folder)
    if dec:
        print(f"Resuming from {pipeline.dec_path(folder)} (use --rescan to start over)")
    else:
        print(f"Scoring {folder} with {args.workers} workers …")
        dec = pipeline.build(folder, params, args.workers,
                             lambda done, total: print(f"  scored {done}/{total}", end="\r", flush=True))
        print()
    print("\n".join(pipeline.summary_lines(dec)))
    if review.blocked_reason(dec):
        sys.exit(f"Not applied: {review.blocked_reason(dec)}")
    if args.apply != "none":
        applier.apply(folder, dec, args.apply, args.dry_run)
        if not args.dry_run:
            dec["applied"] = args.apply
            pipeline.save(folder, dec)
    print(f"Decisions: {pipeline.dec_path(folder)}")


if __name__ == "__main__":
    main()
