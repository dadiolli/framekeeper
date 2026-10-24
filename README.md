<p align="center">
  <img src="assets/framekeeper_camera.svg" alt="" height="110">
  <br>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo.svg">
    <img src="assets/logo-on-light.svg" alt="Framekeeper" height="44">
  </picture>
</p>

Keeps the sharpest shot of every burst of near-duplicates in a RAW+JPG shoot folder.
It proposes picks, lets you review them in the browser (on by default), then applies them without deleting anything.

## Setup

Use Python 3.10–3.13. rawpy has no wheels for 3.14 yet.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python framekeeper.py
```

This opens the **start page** at `http://127.0.0.1:8765`:

- **Choose folder…** opens the macOS folder picker. You can also paste a path into the field next to it.
- **Drag & drop:** drag a folder, or a few photos from it, from Finder onto the page. Browsers only give a page the names of dropped items, not their location on disk. So the app sends the folder name plus some of its file names, and finds the folder that matches both, using Spotlight. If Spotlight doesn't index that location, it searches near your recent folders, in Pictures, Desktop, Downloads, Documents, your home folder, and on external drives. If several folders match, you pick one; if none is found, use Choose folder….
- **Recent folders** shows each folder's state, for example "reviewed 340 / 2100 stacks", "finished" or "rejects moved". From there you can Resume, Rescan (discards your choices), Undo apply, or remove the folder from the list (its files aren't touched).
- New folders are scored first, with a progress bar on the page. The review opens automatically when scoring is done.

You can also start with a folder: `framekeeper.py ~/Pictures/shoot` opens it right away. If the app is already running, it hands the folder over to the running app instead of starting a second one.

**Stopping halfway through a big folder:** every click is saved to `<folder>/.framekeeper/decisions.json` right away. Use **Save & close** in the review, close the tab, click **Quit**, or press Ctrl+C; nothing is lost. When you reopen the folder later, Resume and the gallery (`G`) start at the first stack you haven't reviewed yet. A stack counts as reviewed once it was shown in the gallery or you changed a choice in it.

The recent-folders list is stored in `~/.framekeeper/recent.json`.

**What the script does with each folder:**

1. **Scan.** `DSC01234.ARW` and `DSC01234.JPG` are treated as one shot. RAW-only shots are scored on their embedded preview.
2. **Score.** Each image is resized to 3000 px on the long edge. On an 8×8 tile grid, the score is the Laplacian variance of the sharpest 10 % of tiles, which measures the in-focus subject rather than the bokeh.
3. **Group.** Neighbouring shots that are no more than 2 s apart (EXIF time) *and* within perceptual-hash distance 12 form a group.
4. **Propose.** The best shot in each group is kept. If #2 scores within 10 % of #1, the group is marked *uncertain*.
5. **Review.** Uncertain groups are listed first.
   - **Grid** (default):
     - Click a shot to make it the only keep in its group. ⌘/Ctrl/Shift-click to keep several.
     - Ungrouped photos are hidden until you tick "show ungrouped photos". Clicking one toggles keep/reject.
     - "Reject all" rejects a whole group; "Restore" brings back the proposal.
     - **Grouping** opens a panel to regroup the folder with looser or stricter rules, without rescanning. A **Similarity** slider goes from "very similar" (strict) to "loosely similar", and **Max. time between neighbouring shots** goes from 1 s to "no limit". To find the same subject shot seconds or minutes apart, you usually need a longer time gap as well as looser similarity. A live preview shows the resulting number of stacks before you click Apply. Stacks that stay exactly the same keep your choices and "reviewed" state; stacks that change get a fresh automatic proposal. The settings are saved per folder.
     - To preview a shot without changing the selection: hover and press Space, Alt/⌥-click it, or use the ⤢ button.
     - "Compare" opens the full-resolution compare view for that group (see below).
   - **Gallery** (button or `G`; `G` over a group opens the gallery at that group): one stack at a time, almost full window, in shooting order, ungrouped photos included. It opens on the stack's current keep, with a filmstrip of the stack below.

     | key | action |
     |---|---|
     | `←` `→` | previous / next stack |
     | `↑` `↓` | previous / next photo within the stack |
     | `P` | keep only this photo (`⇧P`: also keep it) |
     | `X` | reject / un-reject this photo |
     | `⇧X` | reject the whole stack (again: restore) |
     | `U` | restore the proposal for this stack |
     | `⌫` | undo the last decision (while its confirmation is shown) |
     | `Z` / `Space` | toggle fit ↔ 100% (starts on the sharpest area) |
     | `0` `1` `2` `5` | fit / 100% / 200% / 50% |
     | `C` | compare the stack at full resolution |
     | `Esc` / `G` | back to the grid |

     On the last stack, a **Finish & apply** button appears in the gallery (and in compare), so you can finish right there.

     With "auto-advance" on (default), it jumps to the next stack after `P`, `⇧X`, or rejecting an ungrouped photo. Every decision shows a short confirmation at the bottom ("✕ Rejected DSC0005 · next stack") with an Undo button.

     **Zoom and move around:**
     - Click a spot on the photo to see it at 100%, or use the keys above or the Fit/50%/100%/200% buttons.
     - To move around: drag, two-finger scroll, or click/drag in the **navigator** (bottom right). The navigator is an overview of the photo with the visible area outlined.
     - `↑` `↓` keep the zoom and position, so you can flip through a stack looking at the same spot.
   - **Compare** (`C` in the gallery, or "Compare" on a group in the grid): every photo of the stack side by side at full resolution (the camera JPG, or the RAW's embedded preview). It opens at 100 % on the sharpest area of the proposed pick.
     - All panels pan and zoom together, so you always see the same spot in each photo. Drag, scroll, or click/drag in the navigator (bottom right) to move around; pinch or `0` / `1` / `2` / `5` (fit / 100 % / 200 % / 50 %) to zoom; `Z` or `Space` toggles fit and 100 %.
     - In fit view, clicking a spot zooms every photo to 100 % there. When zoomed, clicking a photo selects it.
     - `P`, `X`, `⇧X`, `U`, `↑` `↓` (select photo) and `←` `→` (next stack, stays in compare) work as in the gallery. `Esc` or `C` goes back to the gallery.
     - 100 % means one image pixel per physical screen pixel, so on a Retina display it looks half the size you might expect. Use 200 % for a closer look.

6. **Apply.** Pick the mode next to **Finish & apply** in the review (the default comes from `--apply`). The result is shown on the page, with a link back to the folder list.
   - **move** (default): rejected RAW+JPG pairs (and their sidecars) go to `_rejects/`. Undo it from the start page or with `--undo`. A folder whose rejects were moved can only be reopened after an undo (or a rescan).
   - **xmp**: writes `.xmp` sidecars for RAW files with label Green (keep) or Red (reject). In Lightroom, run *Metadata → Read Metadata from Files* and filter by label. Applying again after another review rewrites the sidecars Framekeeper created.
   - **none**: only saves `decisions.json`.
7. **Clean up for Lightroom** (optional, after **move**): on the start page, folders whose rejects were moved get a **Clean up for Lightroom** button (or run `framekeeper.py <folder> --cleanup`, add `--dry-run` to preview). It moves these to the system trash, where they can be restored:
   - the JPGs of kept shots that also have a RAW
   - the `_rejects` folder
   - the preview cache

   What's left is the kept RAW files with their sidecars, ready to import. Kept photos that exist only as JPG stay. A confirmation lists everything first. Afterwards "Undo apply" is no longer possible. The trash is the macOS Trash (built-in `trash` command on macOS 15+), the Windows Recycle Bin, or the freedesktop.org trash on Linux (via Send2Trash).

## Useful flags

| flag | default | |
|---|---|---|
| `--no-review` | off | apply the proposal without the browser (needs a folder) |
| `--apply` | move | default apply mode: `move`, `xmp` or `none` |
| `--port` | 8765 | port of the local web app |
| `--dry-run` | off | print what would be moved or written |
| `--time-gap` | 2.0 | seconds between neighbouring shots in a group |
| `--hash-dist` | 12 | 0–64. Raise it if bursts with subject movement get split; lower it if different scenes get merged |
| `--margin` | 0.10 | threshold for "uncertain" |
| `--rescan` | off | ignore saved decisions and score again |
| `--undo` | | restore files moved by `--apply move` |
| `--cleanup` | | after `--apply move`: trash JPG twins of kept RAWs, `_rejects/` and the cache |

## Limitations (prototype)

- Sharpness only. It doesn't detect closed eyes or check exposure.
- In XMP mode, existing `.xmp` sidecars from other programs are left untouched rather than merged.
- One folder is open at a time.
- Working files (previews and crops) live in `<folder>/.framekeeper`. Delete that folder when you are done.

## License

Framekeeper's own code is MIT licensed, see [LICENSE](LICENSE).

Bundled third-party components are not covered by that license. Icons come from [Font Awesome Free](https://fontawesome.com) 7.3.1 by Fonticons, Inc., bundled in `vendor/fontawesome/` so the app works offline: icons CC BY 4.0, fonts SIL OFL 1.1, code MIT. Details are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
