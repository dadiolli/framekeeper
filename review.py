"""Local web app: start page (choose / resume a folder), review page, apply.

Stdlib only (http.server), bound to 127.0.0.1. One folder is open at a time.
Every click is saved to that folder's .framekeeper/decisions.json immediately, so
closing the tab, "Save & close" or Ctrl+C never loses choices - reopening the
folder resumes where you left off.
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import apply as applier
import pipeline
from imaging import WORK_DIR, render_crop, render_full

HERE = Path(__file__).parent
PAGES = {"/": HERE / "home.html", "/review": HERE / "review.html"}
# static folders served as-is: /vendor/ = third-party assets (Font Awesome), /assets/ = Framekeeper's own logos
STATIC_DIRS = {"vendor": HERE / "vendor", "assets": HERE / "assets"}
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
                ".svg": "image/svg+xml"}
RECENT = Path(os.environ.get("FRAMEKEEPER_HOME") or Path.home() / ".framekeeper") / "recent.json"
MAX_RECENT = 20
APPLY_MODES = ("move", "xmp", "none")


# ---------- recent folders (~/.framekeeper/recent.json) ----------

def recent_paths():
    try:
        return [r["path"] for r in json.loads(RECENT.read_text())]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _write_recent(paths):
    RECENT.parent.mkdir(exist_ok=True)
    RECENT.write_text(json.dumps([{"path": p} for p in paths[:MAX_RECENT]], indent=1))


def recent_add(folder):
    _write_recent([str(folder)] + [p for p in recent_paths() if p != str(folder)])


def recent_forget(folder):
    _write_recent([p for p in recent_paths() if p != str(folder)])


def folder_status(path):
    p = Path(path)
    st = {"path": str(p), "name": p.name, "exists": p.is_dir()}
    if not st["exists"]:
        return st
    try:
        dec = pipeline.load(p)
    except (OSError, ValueError) as e:
        return {**st, "error": f"unreadable decisions.json: {e}"}
    if dec:
        moves = p / WORK_DIR / "moves.json"
        st.update(pipeline.summary(dec), scored=True, created=dec.get("created"),
                  applied=dec.get("applied"), reviewed=dec.get("reviewed"), cleaned=dec.get("cleaned"),
                  can_undo=moves.exists() and moves.read_text().strip() not in ("", "[]"),
                  can_cleanup=dec.get("applied") in (True, "move") and not dec.get("cleaned"))
    return st


def blocked_reason(dec):
    """Folders whose rejects were moved can't be reviewed again until they are undone."""
    if dec and dec.get("applied") in (True, "move"):
        return "rejects were already moved to _rejects/ - undo the apply first (or rescan)"
    return None


def choose_folder():
    """Native macOS folder picker. A web page can't learn a folder's path, so the server asks."""
    if sys.platform != "darwin":
        raise ValueError("the folder picker only works on macOS - paste the path instead")
    # run it for the frontmost app (the browser) so the dialog appears in front of it
    script = ('tell application (path to frontmost application as text) to '
              'POSIX path of (choose folder with prompt "Choose a photo folder to cull")')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() or None  # None = cancelled


def _spotlight(name, folders_only):
    if sys.platform != "darwin":
        return []
    q = name.replace("\\", "\\\\").replace('"', '\\"')
    query = f'kMDItemFSName == "{q}"' + (' && kMDItemContentType == "public.folder"' if folders_only else "")
    try:
        r = subprocess.run(["mdfind", query], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [Path(line) for line in r.stdout.splitlines() if line]


def _walk_for(name, max_depth=3, max_dirs=20000):
    """Fallback for places Spotlight doesn't index (excluded drives, temp folders):
    look for a directory called `name` under the usual photo places and next to recent folders."""
    home = Path.home()
    roots = [Path(p).parent for p in recent_paths()] + [home / "Pictures", home / "Desktop", home / "Downloads"]
    roots += sorted(Path("/Volumes").glob("*")) + [home / "Documents", home]
    found, seen, budget = [], set(), max_dirs
    for root in roots:
        queue = [(root, 0)]
        while queue and budget > 0:
            d, depth = queue.pop(0)
            if d in seen:
                continue
            seen.add(d)
            budget -= 1
            try:
                entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
            except OSError:
                continue
            for e in entries:
                if e.name == name:
                    found.append(Path(e.path))
                if depth < max_depth and e.name != "Library":
                    queue.append((Path(e.path), depth + 1))
    return found


def locate_dropped(folder=None, children=(), files=()):
    """Find the real path of a folder dragged onto the start page.

    Browsers only reveal names, so we search by name and keep the candidates that
    contain the same files: `folder` + some of its file names, or (for dropped photos)
    just file names, whose common parent folder is what we want.
    """
    if folder:
        sample = [c for c in children if c and "/" not in c][:20]
        ok = lambda d: d.is_dir() and all((d / c).exists() for c in sample)
        matches = [d for d in dict.fromkeys(_spotlight(folder, True)) if ok(d)]
        if not matches:
            matches = [d for d in dict.fromkeys(_walk_for(folder)) if ok(d)]
    elif files:
        names = [f for f in files if f and "/" not in f][:20]
        parents = [p.parent for p in _spotlight(names[0], False)]
        matches = [d for d in dict.fromkeys(parents) if all((d / n).exists() for n in names)]
    else:
        raise ValueError("nothing was dropped")
    return [str(m) for m in matches]


# ---------- app state ----------

class App:
    def __init__(self, params, workers, apply_mode, dry_run):
        self.params, self.workers, self.apply_mode, self.dry_run = params, workers, apply_mode, dry_run
        self.lock = threading.RLock()
        self.folder = self.dec = None
        self.shots = {}
        self.token = ""
        self.job = {"state": "idle"}  # idle | scoring | ready | error
        self.server = None

    def open(self, folder, rescan=False):
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError(f"not a folder: {folder}")
        dec = None if rescan else pipeline.load(folder)
        if blocked_reason(dec):
            raise ValueError(blocked_reason(dec))
        with self.lock:
            if self.job["state"] == "scoring":
                raise ValueError(f"still scoring {self.job['folder']} - wait until it is done")
            self.folder, self.dec, self.shots = folder, None, {}
            self.job = {"state": "scoring", "folder": str(folder), "done": 0, "total": 0,
                        "message": "Loading…" if dec else "Scanning…"}
        recent_add(folder)
        threading.Thread(target=self._load, args=(folder, dec), daemon=True).start()

    def _load(self, folder, dec):
        try:
            if dec is None:
                print(f"Scoring {folder} with {self.workers} workers …")
                dec = pipeline.build(folder, self.params, self.workers, self._progress)
                print()
            for line in pipeline.summary_lines(dec):
                print(line)
            with self.lock:
                if self.folder != folder:
                    return
                self.dec = dec
                self.shots = {s["id"]: s for g in dec["groups"] for s in g["shots"]}
                # images are cached by URL; the token keeps DSC0001 of one folder (or scan) from showing up in another
                self.token = hashlib.sha1(f"{folder}|{dec.get('created')}".encode()).hexdigest()[:10]
                self.job = {"state": "ready", "folder": str(folder)}
        except Exception as e:
            print(f"\n! {folder}: {e}")
            with self.lock:
                self.folder = None
                self.job = {"state": "error", "folder": str(folder), "message": f"{e}"}

    def _progress(self, done, total):
        with self.lock:
            self.job.update(done=done, total=total, message=f"Scoring {done} / {total}")
        print(f"  scored {done}/{total}", end="\r", flush=True)

    def close(self):
        with self.lock:
            if self.job["state"] != "scoring":
                self.folder, self.dec, self.shots = None, None, {}
                self.job = {"state": "idle"}

    def ready(self):
        return self.job["state"] == "ready" and self.dec is not None

    def save(self):
        pipeline.save(self.folder, self.dec)

    def finish(self, mode):
        with self.lock:
            folder, dec = self.folder, self.dec
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                print(f"{folder}")
                for line in pipeline.summary_lines(dec):
                    print(line)
                if mode == "none":
                    print("Choices saved; no files were changed.")
                else:
                    applier.apply(folder, dec, mode, self.dry_run)
            dec["reviewed"] = True
            if mode != "none" and not self.dry_run:
                dec["applied"] = mode
            self.save()
            self.close()
        print(out.getvalue(), end="")
        return out.getvalue()


def captured(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args)
    print(out.getvalue(), end="")
    return out.getvalue()


# ---------- HTTP ----------

def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            cacheable = ctype == "image/jpeg" or self.path.startswith(("/vendor/", "/assets/"))
            self.send_header("Cache-Control", "max-age=3600" if cacheable else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, code=200):
            self.send(code, json.dumps(obj).encode(), "application/json")

        def send_file(self, path):
            if path.exists():
                self.send(200, path.read_bytes(), "image/jpeg")
            else:
                self.send(404, b"not found", "text/plain")

        def do_GET(self):
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/", 1)
            if parts[0] in STATIC_DIRS and len(parts) == 2:
                root = STATIC_DIRS[parts[0]].resolve()
                f = (root / unquote(parts[1])).resolve()
                if root in f.parents and f.is_file() and f.suffix in STATIC_TYPES:
                    self.send(200, f.read_bytes(), STATIC_TYPES[f.suffix])
                else:
                    self.send(404, b"not found", "text/plain")
            elif url.path in PAGES:
                self.send(200, PAGES[url.path].read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/home":
                with app.lock:
                    job, active = dict(app.job), str(app.folder) if app.folder else None
                self.send_json({"app": "framekeeper", "job": job, "active": active, "dry_run": app.dry_run,
                                "recent": [folder_status(p) for p in recent_paths()]})
            elif url.path == "/api/decisions":
                with app.lock:
                    if not app.ready():
                        return self.send_json({"error": "no folder open"}, 409)
                    body = json.dumps({**app.dec, "apply_mode": app.apply_mode, "token": app.token,
                                       "default_params": pipeline.DEFAULT_PARAMS,
                                       "dry_run": app.dry_run}).encode()
                self.send(200, body, "application/json")
            elif len(parts) == 2 and parts[0] in ("thumb", "preview", "crop", "full"):
                self.send_image(parts[0], unquote(parts[1]), parse_qs(url.query))
            else:
                self.send(404, b"not found", "text/plain")

        def send_image(self, kind, sid, query):
            with app.lock:
                folder, shot, shots = app.folder, app.shots.get(sid), app.shots
            if not shot:
                return self.send(404, b"unknown shot", "text/plain")
            work = folder / WORK_DIR
            try:
                if kind == "thumb":
                    return self.send_file(work / "thumbs" / f"{sid}.jpg")
                if kind == "preview":
                    return self.send_file(work / "previews" / f"{sid}.jpg")
                if kind == "full":
                    # the camera JPG as is (browsers apply its EXIF rotation), or the RAW's embedded preview
                    if shot["jpg"]:
                        return self.send_file(folder / shot["jpg"])
                    out = work / "full" / f"{sid}.jpg"
                    if not out.exists():
                        out.parent.mkdir(exist_ok=True)
                        render_full(folder, shot, out)
                    return self.send_file(out)
                # /crop/<id>?at=<id of shot whose focus point to use>
                at = shots.get(query.get("at", [sid])[0], shot)
                out = work / "crops" / f"{sid}@{at['id']}.jpg"
                if not out.exists():
                    render_crop(folder, shot, at["focus"], out)
                self.send_file(out)
            except Exception as e:
                self.send(500, str(e).encode(), "text/plain")

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self.send_json({"error": "bad json"}, 400)
            try:
                self.route_post(self.path, body)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)

        def route_post(self, path, body):
            if path == "/api/choose":
                self.send_json({"path": choose_folder()})
            elif path == "/api/locate":
                self.send_json({"matches": locate_dropped(body.get("folder"), body.get("children", []),
                                                          body.get("files", []))})
            elif path == "/api/open":
                app.open(body["path"], bool(body.get("rescan")))
                self.send_json({})
            elif path == "/api/close":
                app.close()
                self.send_json({})
            elif path == "/api/forget":
                recent_forget(body["path"])
                self.send_json({})
            elif path == "/api/cleanup":  # {"path": folder, "dry_run": bool}
                folder = Path(body["path"])
                dec = pipeline.load(folder)
                if not dec:
                    raise ValueError("this folder has not been scored")
                if body.get("dry_run"):
                    return self.send_json({"plan": applier.plan_summary(applier.cleanup_plan(folder, dec))})
                try:
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        if applier.cleanup(folder, dec, False):
                            pipeline.save(folder, dec)
                except RuntimeError as e:
                    raise ValueError(str(e))
                print(out.getvalue(), end="")
                self.send_json({"output": out.getvalue()})
            elif path == "/api/undo":
                folder = Path(body["path"])
                self.send_json({"output": captured(applier.undo, folder, False)})
            elif path == "/api/quit":
                self.send_json({})
                threading.Thread(target=app.server.shutdown, daemon=True).start()
            elif path in ("/api/keep", "/api/seen", "/api/apply_mode", "/api/finish", "/api/regroup"):
                with app.lock:
                    if not app.ready():
                        return self.send_json({"error": "no folder open"}, 409)
                    if path == "/api/keep":  # {shot_id: bool}
                        for sid, k in body.items():
                            if sid in app.shots:
                                app.shots[sid]["keep"] = bool(k)
                        app.save()
                    elif path == "/api/seen":  # {"groups": [group ids]}
                        ids = set(body.get("groups", []))
                        for g in app.dec["groups"]:
                            if g["id"] in ids:
                                g["seen"] = True
                        app.save()
                    elif path == "/api/regroup":  # {"time_gap": s, "hash_dist": n, "apply": bool}
                        time_gap, hash_dist = float(body["time_gap"]), int(body["hash_dist"])
                        if not (0 <= hash_dist <= 64 and time_gap >= 0):
                            raise ValueError("bad grouping values")
                        result = pipeline.regroup(app.dec, time_gap, hash_dist, bool(body.get("apply")))
                        if body.get("apply"):
                            app.shots = {s["id"]: s for g in app.dec["groups"] for s in g["shots"]}
                            app.save()
                        return self.send_json(result)
                    elif path == "/api/apply_mode":
                        if body.get("mode") not in APPLY_MODES:
                            raise ValueError("unknown apply mode")
                        app.apply_mode = body["mode"]
                    else:
                        return self.send_json({"output": app.finish(body.get("mode", app.apply_mode))})
                self.send_json({})
            else:
                self.send(404, b"not found", "text/plain")

    return Handler


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/home", timeout=2) as r:
            return json.load(r).get("app") == "framekeeper"
    except (OSError, ValueError):
        return False


def serve(folder, params, workers, apply_mode, dry_run, port, rescan=False):
    """Run the app until Ctrl+C or "Quit". With a folder, open it right away."""
    url = f"http://127.0.0.1:{port}/" + ("?go=1" if folder else "")
    app = App(params, workers, apply_mode, dry_run)
    try:
        app.server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        if not _already_running(port):
            sys.exit(f"port {port} is in use by another program - try --port")
        # hand the folder to the instance that is already running instead of starting a second one
        if folder:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/open", method="POST",
                                         data=json.dumps({"path": str(folder), "rescan": rescan}).encode())
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                sys.exit(f"could not open {folder}: {json.load(e).get('error')}")
        print(f"Framekeeper is already running - opened {url}")
        webbrowser.open(url)
        return

    if folder:
        try:
            app.open(folder, rescan)
        except ValueError as e:
            print(f"! {e}")
            url = url.split("?")[0]
    print(f"Framekeeper at {url}   (Ctrl+C to quit - choices are saved as you go)")
    webbrowser.open(url)
    try:
        app.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server.server_close()
        print(f"\nStopped {datetime.now():%H:%M}. Reopen a folder any time to continue where you left off.")
