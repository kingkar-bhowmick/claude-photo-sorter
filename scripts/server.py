"""The dashboard app.  python scripts/server.py   ->  http://127.0.0.1:8765

Serves dashboard/ plus a small local API so the page can do everything itself:
pick folders, peek at a folder, run the whole sort (scan, duplicates, labels,
build folders) with live progress, edit folders, browse photos at full size, move a
photo to another folder, and open folders in Finder.
Listens on 127.0.0.1 only: nothing is reachable from other computers.
"""
import csv, glob, hashlib, io, json, mimetypes, os, random, re, shutil, signal, subprocess, sys, threading, time, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from apply import apply_lock
from common import (CODEX_LABELS, DATA, IMG, JEV_OUT, LABELS, PROFILE_ENV, PROFILES, ROOT, VID, WORK,
                    categories, category_path, codex_cache, is_within, load, overrides_path, read_codex_labels,
                    save, sort_destination, valid_labels)

PORT = int(os.environ.get("PORT", 8765))
DASH = os.path.join(ROOT, "dashboard")
PEEK = os.path.join(DASH, "peek")
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable
LABELERS = {"codex": "label_codex.py", "claude": "label_claude.py", "jev": "label_jev.py"}
job_lock = threading.Lock()
active_process = stop_finished = None
job = {"step": None, "labeler": None, "request": {}, "running": False, "stopping": False,
       "cancelled": False, "fresh_pending": False, "log": [],
       "code": None, "phase": None, "phases": [], "done": 0, "total": 0,
       "started": None, "phase_started": None, "timings": {}}


def has_jev_key():
    if os.environ.get("JEV_KEY") or os.environ.get("TYPESAFE_API_KEY"):
        return True
    env = os.path.join(ROOT, ".env")
    return os.path.exists(env) and bool(re.search(r"^(JEV_KEY|TYPESAFE_API_KEY)=\S", open(env).read(), re.M))


def has_sorted_copies(dest):
    if not dest:
        return False
    manifest = os.path.join(dest, "sorted.csv")
    if os.path.islink(manifest):
        return False
    try:
        with open(manifest, newline="") as report:
            for row in csv.DictReader(report):
                if row.get("file"):
                    path = os.path.join(dest, row["file"])
                    if os.path.isfile(path) and is_within(dest, path):
                        return True
    except (OSError, ValueError, csv.Error):
        pass
    return False


def state():
    fields = ("step", "labeler", "request", "running", "stopping", "cancelled", "fresh_pending", "code",
              "phase", "phases", "done", "total", "started", "phase_started", "timings")
    with job_lock:
        status = {k: job[k] for k in fields} | {"log": job["log"][-40:], "now": job.get("finished") or time.time()}
    inv = load(os.path.join(DATA, "inventory.json"), [])
    src = load(os.path.join(DATA, "source.json"), {})
    profile = src.get("profile", "standard")
    output = src.get("sorted")
    applied = bool(output) and os.path.exists(os.path.join(DATA, "labels.json"))
    missing = applied and not status["running"] and not has_sorted_copies(output)
    claude_files = glob.glob(os.path.join(LABELS, "*.json"))
    profiles = {key: {"label": title, "categories": categories(key)} for key, title in PROFILES.items()}
    return {
        "categories": profiles["standard"]["categories"], "profiles": profiles,
        "result_categories": load(os.path.join(DATA, "categories.json")) if applied else None,
        "source": src.get("source"), "limit": src.get("limit"), "sorted": output, "profile": profile,
        "output_missing": missing,
        "files": len(inv), "bytes": sum(r["bytes"] for r in inv),
        "steps": {
            "scan": bool(inv),
            "dupes": os.path.exists(os.path.join(DATA, "dupes.json")),
            "label": bool(claude_files) or os.path.isdir(CODEX_LABELS) or os.path.exists(JEV_OUT),
            "jev_labels": os.path.exists(JEV_OUT),
            "apply": applied and not missing,
        },
        "jev_key": has_jev_key(),
        "codex_cli": bool(shutil.which("codex")),
        "claude_cli": bool(shutil.which("claude")),
        "overrides": len(load(overrides_path(profile), {})),
        "job": status,
        "platform": sys.platform,
    }


def live_labels():
    """Labels written so far, for the live animation."""
    if job["labeler"] == "codex":
        recs = load(os.path.join(DATA, "inventory.json"), [])
        profile = job["request"].get("profile", "standard")
        return read_codex_labels(recs, categories(profile), profile)
    out = {}
    for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
        out.update({str(k): v for k, v in load(f, {}).items()})
    for k, v in load(JEV_OUT, {}).items():
        if "key" in v and (v.get("confidence", 0) >= 0.7 or k not in out):
            out[k] = v["key"]
    return out


def clear_library():
    """Invalidate the inventory before replacing previews so a stopped scan is retried."""
    files = [os.path.join(DATA, name) for name in ("inventory.json", "labels.json", "dupes.json")]
    files += [JEV_OUT] + [overrides_path(profile) for profile in PROFILES]
    for f in files:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    for d in (LABELS, os.path.join(WORK, "sheets")):
        shutil.rmtree(d, ignore_errors=True)


def stop_process(process, finished):
    try:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            if result.returncode and process.poll() is None:
                process.terminate()
            return
        os.killpg(process.pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.1)
            os.killpg(process.pid, 0)
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        finished.set()


def stop_job(started):
    global stop_finished
    with job_lock:
        if not job["running"] or (started is not None and job["started"] != started) or job["stopping"]:
            return
        job["stopping"] = True
        if active_process is not None:
            stop_finished = threading.Event()
            threading.Thread(target=stop_process, args=(active_process, stop_finished), daemon=True).start()


def stream(cmd, profile):
    """Run one script, keep its output for the log, and read PROGRESS lines."""
    global active_process, stop_finished
    with job_lock:
        if job["stopping"]:
            return 130
        active_process = p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=ROOT,
            env={**os.environ, PROFILE_ENV: profile}, start_new_session=os.name != "nt",
        )
    try:
        with p.stdout:
            for line in p.stdout:
                line = line.rstrip()
                m = re.match(r"PROGRESS (\d+) (\d+)", line)
                with job_lock:
                    if m:
                        job["done"], job["total"] = int(m.group(1)), int(m.group(2))
                    else:
                        job["log"].append(line)
        return p.wait()
    finally:
        with job_lock:
            finished = stop_finished
            if finished is None:
                active_process = None
        if finished is not None:
            finished.wait()
            with job_lock:
                active_process = stop_finished = None


def start_job(step, phases, body, labeler=None, request=None):
    def run():
        try:
            code = body()
        except Exception as error:
            job["log"].append(str(error))
            code = 1
        with job_lock:
            cancelled = job["stopping"] and code != 0
            job.update(running=False, stopping=False, cancelled=cancelled, code=130 if cancelled else code,
                       phase="done" if code == 0 else job["phase"], finished=time.time())

    with job_lock:
        if job["running"]:
            raise RuntimeError("Something is already running. Wait for it to finish.")
        now = time.time()
        job.update(step=step, labeler=labeler, request=request or {}, running=True, log=[f"$ {step}"],
                   stopping=False, cancelled=False, fresh_pending=bool((request or {}).get("fresh")),
                   code=None, phase=None, phases=phases, done=0, total=0, started=now, finished=None,
                   phase_started=now, timings={})
        threading.Thread(target=run, daemon=True).start()


def run_phase(name, cmd, profile="standard"):
    with job_lock:
        if job["stopping"]:
            return 130
        job.update(phase=name, done=0, total=0, phase_started=time.time())
    code = stream([PY, "-u"] + cmd, profile)
    with job_lock:
        job["timings"][name] = round(time.time() - job["phase_started"], 1)
        if job["stopping"] and name != "apply":
            return 130
    return code


def sort_all(args):
    source = (args.get("source") or "").strip()
    dest = (args.get("dest") or "").strip()
    limit = int(args.get("limit") or 0)
    labeler = args.get("labeler") or "codex"
    profile = args.get("profile") or "standard"
    fresh = args.get("fresh", False)
    if profile not in PROFILES or type(fresh) is not bool:
        raise ValueError("Choose a sorting preset and whether to start from scratch.")
    if labeler not in LABELERS:
        raise ValueError("Choose a labeler under Advanced.")
    if not source or not os.path.isdir(os.path.expanduser(source)):
        raise ValueError("Choose the folder with your photos first.")
    source = os.path.abspath(os.path.expanduser(source))
    dest = sort_destination(source, dest)
    if labeler == "claude" and not shutil.which("claude"):
        raise ValueError("Claude Code is not installed. Install it or choose Codex under Advanced.")
    if labeler == "jev" and not has_jev_key():
        raise ValueError("Add JEV_KEY=your-key to a .env file in this folder to use Jev.")
    src = load(os.path.join(DATA, "source.json"), {})
    have = os.path.exists(os.path.join(DATA, "inventory.json"))
    need_scan = fresh or not have or src.get("source") != source or int(src.get("limit") or 0) != limit
    phases = (["scan"] if need_scan else []) + ["dupes", "label", "apply"]

    def phase(name, script, *extra):
        return run_phase(name, [os.path.join(SCRIPTS, script), *extra], profile)

    def body():
        # Hold the copy lock while changing the inventory and labels as well.
        # apply.py takes the lock itself when the copy phase starts.
        with apply_lock():
            if job["stopping"]:
                return 130
            if need_scan:
                clear_library()
                if phase("scan", "scan.py", source, *(["--limit", str(limit)] if limit else [])):
                    return 1
            if fresh:
                recs = load(os.path.join(DATA, "inventory.json"), [])
                cache = codex_cache(recs, categories(profile), profile)
                if labeler == "codex" and os.path.isdir(cache):
                    shutil.rmtree(cache)
                job["fresh_pending"] = False
            if phase("dupes", "dupes.py"):
                return 1
            if labeler != "codex":
                marker = os.path.join(LABELS, ".categories")
                if profile == "standard":
                    with open(category_path(profile), "rb") as f:
                        inputs = f.read()
                else:
                    inputs = json.dumps([profile, categories(profile)]).encode()
                fingerprint = hashlib.sha1(inputs).hexdigest()[:12]
                if load(marker) != fingerprint:  # folders changed since the last labels
                    shutil.rmtree(LABELS, ignore_errors=True)
                    if os.path.exists(JEV_OUT):
                        os.remove(JEV_OUT)
                save(marker, fingerprint)
            if phase("label", LABELERS[labeler]):
                if labeler == "codex" or job["stopping"]:
                    return 1
                total = len(load(os.path.join(DATA, "inventory.json"), []))
                if not total or len(live_labels()) < 0.95 * total:
                    return 1
                job["log"].append("A few files could not be labelled; they go to the catch-all folders.")
        return phase("apply", "apply.py", dest, *(["--labeler", "codex"] if labeler == "codex" else []))
    start_job("sort", phases, body, labeler, {"source": source, "dest": dest, "limit": limit,
                                           "profile": profile, "fresh": fresh})


def run_single(step, args):
    profile = args.get("profile") or "standard"
    if profile not in PROFILES:
        raise ValueError("Choose a sorting preset.")
    cmds = {
        "scan": ["scan.py", args.get("source", "")] + (["--limit", str(args["limit"])] if args.get("limit") else []),
        "dupes": ["dupes.py"], "estimate": ["label_jev.py", "--estimate"], "jev": ["label_jev.py"],
        "codex": ["label_codex.py"], "claude": ["label_claude.py"], "apply": ["apply.py", args.get("dest", "")],
    }
    if step == "apply" and args.get("labeler") == "codex":
        cmds[step] += ["--labeler", "codex"]
    if step not in cmds:
        raise ValueError("unknown step")

    def body():
        cmd = [os.path.join(SCRIPTS, cmds[step][0])] + cmds[step][1:]
        if step == "scan":
            with apply_lock():
                if job["stopping"]:
                    return 130
                clear_library()
                return run_phase(step, cmd, profile)
        return run_phase(step, cmd, profile)
    start_job(step, [step], body, labeler="codex" if step == "codex" else args.get("labeler"),
              request={"profile": profile})


def save_categories(profile, cats):
    path = category_path(profile)
    keys = set()
    for c in cats:
        key = c.get("key", "")
        label = c.get("label", "").strip()
        folder = label.replace("/", "-").replace("&", "and")
        if not key or key in keys or key == "_dupes" or c.get("kind") not in ("photo", "video"):
            raise ValueError("Each label needs a unique key and a media type.")
        if not folder or folder in (".", "..", "_Review") or re.search(r"[\\:\x00]", folder):
            raise ValueError("Choose a valid name for each folder.")
        keys.add(key)
        c["label"] = label
        c["rule"] = c.get("rule", "").strip() or label
    if {c["kind"] for c in cats} != {"photo", "video"}:
        raise ValueError("Keep at least one photo folder and one video folder.")
    with job_lock:
        if job["running"]:
            raise ValueError("Stop the current sort before editing labels.")
        with apply_lock():
            save(path, cats)


def move_label(item_id, key):
    with job_lock:
        if job["running"]:
            raise ValueError("Stop the current sort before moving files between labels.")
        src = load(os.path.join(DATA, "source.json"), {})
        profile = src.get("profile", "standard")
        cats = load(os.path.join(DATA, "categories.json")) or categories(profile)
        recs = load(os.path.join(DATA, "inventory.json"), [])
        item = next((r for r in recs if str(r["id"]) == item_id), None)
        if item is None or not valid_labels([item], cats, {item_id: key}):
            raise ValueError("Choose a label for this file's media type.")
        with apply_lock():
            path = overrides_path(profile)
            ov = load(path, {})
            ov[item_id] = key
            save(path, ov)
            labels = load(os.path.join(DATA, "labels.json"), {})
            labels[item_id] = key
            save(os.path.join(DATA, "labels.json"), labels)


def peek(source):
    """Count what is in a folder and make a few tiny previews, right after it is picked."""
    source = os.path.expanduser(source.strip().rstrip("/"))
    if not os.path.isdir(source):
        raise ValueError("That folder does not exist.")
    photos, videos, size, pics = 0, 0, 0, []
    for dp, dirs, fs in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith(" (sorted)")]
        for f in fs:
            ext = os.path.splitext(f)[1].lower()
            if f.startswith("._"):
                continue
            if ext in IMG:
                photos += 1
                if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
                    pics.append(os.path.join(dp, f))
            elif ext in VID:
                videos += 1
            else:
                continue
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    shutil.rmtree(PEEK, ignore_errors=True)
    os.makedirs(PEEK)
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    out, stamp = [], int(time.time())
    for i, p in enumerate(random.Random(3).sample(pics, min(60, len(pics)))):
        try:
            with Image.open(p) as im:
                im.draft("RGB", (160, 160))
                im = ImageOps.exif_transpose(im).convert("RGB")
                s = min(im.size)
                im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((64, 64))
                im.save(os.path.join(PEEK, f"{i}.jpg"), quality=70)
                out.append(f"peek/{i}.jpg?{stamp}")
        except Exception:
            pass
    return {"source": source, "photos": photos, "videos": videos, "bytes": size, "previews": out,
            "suggested_dest": source + " (sorted)"}


def open_path(path, reveal=False):
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path] if reveal else ["open", path])
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select," + path] if reveal else ["explorer", path])
    else:
        subprocess.run(["xdg-open", os.path.dirname(path) if reveal else path])


def pick_folder(prompt):
    """Native macOS folder picker, shown in front of the browser."""
    if sys.platform != "darwin":
        return None
    prompt = prompt.replace('"', "'")[:120]
    script = ('tell application (path to frontmost application as text)\n'
              f'  POSIX path of (choose folder with prompt "{prompt}")\n'
              'end tell')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip().rstrip("/") or None


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DASH, **k)

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def end_headers(self):
        if self.path.startswith(("/data/", "/peek/")) or self.path.endswith((".js", ".css", ".html")) or self.path == "/":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/state":
            return self.send_json(state())
        if u.path == "/api/labels":
            return self.send_json(live_labels())
        m = re.match(r"^/api/file/(\d+)$", u.path)
        if m:
            return self.send_original(int(m.group(1)))
        return super().do_GET()

    def send_original(self, i):
        inv = load(os.path.join(DATA, "inventory.json"), [])
        rec = next((r for r in inv if r["id"] == i), None)
        if not rec or not os.path.exists(rec["path"]):
            return self.send_error(404)
        path = rec["path"]
        if rec["ext"] in (".heic", ".heif", ".tif", ".tiff", ".bmp"):  # browsers can't show these: convert
            from PIL import Image, ImageOps
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                pass
            im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            b = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if rec["ext"] == ".mov":
            ctype = "video/mp4"  # most iPhone .mov files play fine as mp4
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start = int(m.group(1) or 0)
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send_json({"error": "forbidden"}, 403)  # only this page may call the API
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/api/sort":
                sort_all(body)
            elif p == "/api/stop":
                stop_job(body.get("started"))
            elif p == "/api/run":
                run_single(body.get("step"), body)
            elif p == "/api/peek":
                return self.send_json(peek(body.get("source", "")))
            elif p == "/api/categories":
                save_categories(body.get("profile") or "standard", body["categories"])
            elif p == "/api/move":
                move_label(str(body["id"]), body["key"])
            elif p == "/api/open":
                src = load(os.path.join(DATA, "source.json"), {})
                if body.get("id") is not None:
                    rec = next(r for r in load(os.path.join(DATA, "inventory.json")) if r["id"] == int(body["id"]))
                    open_path(rec["path"], reveal=True)
                elif src.get("sorted"):
                    folder = body.get("folder")
                    path = os.path.join(src["sorted"], folder) if folder else src["sorted"]
                    open_path(path if os.path.isdir(path) else src["sorted"])
                else:
                    raise ValueError("Sort first, then the folders can be opened.")
            elif p == "/api/pick":
                return self.send_json({"path": pick_folder(body.get("prompt") or "Choose the folder with your photos")})
            else:
                return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)
        return self.send_json(state())


def interrupted(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    signal.signal(signal.SIGTERM, interrupted)
    print(f"Photo sorter running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)", flush=True)
    with ThreadingHTTPServer(("127.0.0.1", PORT), H) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            stop_job(None)
            deadline = time.monotonic() + 3
            while job["running"] and time.monotonic() < deadline:
                time.sleep(0.05)
