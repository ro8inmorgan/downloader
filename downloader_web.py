#!/usr/bin/env python3
"""
Robin's Downloader – Web UI (Flask + SSE)

What you get:
- Paste URLs, pick destination, set concurrency, toggle unzip, set Referrer
- Live progress in browser (download / extract / move + overall)
- Resume (.part), retry/backoff, graceful Stop

Run:
  python downloader_webui_fixed.py
Open:
  http://localhost:8000

Requirements (pip):
  flask, requests, colorama (optional on Windows)
"""
from typing import Optional
import os
import time
import json
import random
import threading
import queue
import zipfile
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, request, Response, jsonify
import requests

# ---------- Optional Windows color support ----------
try:
    import colorama  # noqa
    colorama.just_fix_windows_console()
except Exception:
    pass

# ---------- App ----------
app = Flask(__name__)

# ---------- PubSub for SSE ----------
class PubSub:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload: dict):
        data = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(data)
                except Exception:
                    pass

pubsub = PubSub()

# ---------- Download job ----------
MAX_DOWNLOAD_TRIES = 5
BACKOFF_BASE = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_JITTER = 0.25

# Default headers; Referer can be overridden from the UI
REFERRER_DEFAULT = "https://www.example.com/"
HEADERS_DEFAULT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": REFERRER_DEFAULT,
}


def _parse_content_range_total(value: str) -> int:
    try:
        return int(value.split("/")[-1])
    except Exception:
        return 0


def _head_content_length(url: str, headers: dict) -> int:
    try:
        rh = requests.head(url, headers=headers, allow_redirects=True, timeout=20)
        rh.raise_for_status()
        return int(rh.headers.get("Content-Length", 0))
    except Exception:
        return 0


def _range_probe_total(url: str, headers: dict) -> int:
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    try:
        rp = requests.get(url, headers=h, stream=True, timeout=20, allow_redirects=True)
        rp.raise_for_status()
        cr = rp.headers.get("Content-Range", "")
        return _parse_content_range_total(cr)
    except Exception:
        return 0


class DownloaderJob:
    def __init__(self, urls, dest: Path, concurrency: int, extract_zips: bool, referrer: Optional[str] = None):
        self.urls = [u.strip() for u in urls if u.strip()]
        self.dest = Path(dest)
        self.dest.mkdir(parents=True, exist_ok=True)
        self.extract_zips = bool(extract_zips)
        self.conc = max(1, int(concurrency))

        # headers (override Referer if provided)
        self.headers = dict(HEADERS_DEFAULT)
        if referrer is not None:
            ref = referrer.strip()
            if ref:
                self.headers["Referer"] = ref
            else:
                # if blank, omit Referer entirely (some hosts are picky)
                self.headers.pop("Referer", None)

        # temp dir colocated with script
        self.temp_dir = Path(__file__).parent / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Queues
        self.q_progress = queue.Queue()  # internal -> manager -> SSE
        self.q_dl = queue.Queue()
        self.q_ex = queue.Queue()
        self.q_mv = queue.Queue()

        # Threads
        self.threads = []
        self._mgr_thread = None

        # State
        self.shutdown = threading.Event()
        self.total_jobs = len(self.urls)
        self.completed = 0

        # Per-slot smoothing state for speeds
        self.spd_state = {}  # (kind,slot) -> {last_done, spd_bytes, spd_t0, rate}
        self.SPEED_WIN = 0.5

        # Active counters for UI bottom row
        self.active = {"download": 0, "extract": 0, "move": 0}

    # --------- progress plumbing ---------
    def report(self, kind, slot, done, total, desc, job):
        try:
            self.q_progress.put_nowait((kind, slot, int(done), int(total), desc, job))
        except Exception:
            pass

    def _pub(self, payload):
        pubsub.publish(payload)

    # --------- workers ---------
    def _sleep_backoff(self, attempt):
        base = BACKOFF_BASE * (BACKOFF_FACTOR ** (attempt - 1))
        jitter = base * BACKOFF_JITTER
        time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))

    def _download_worker(self, slot):
        while True:
            got_item = False
            url = None
            filename = None
            try:
                try:
                    url = self.q_dl.get(timeout=0.25)
                    got_item = True
                except queue.Empty:
                    if self.shutdown.is_set():
                        break
                    continue

                if url is None:
                    # sentinel: exit loop; finally will task_done() once
                    break

                filename = unquote(url.split("/")[-1] or "file.zip")
                temp_final = self.temp_dir / filename
                temp_part = self.temp_dir / (filename + ".part")

                # If stopping and nothing to resume, skip processing
                if self.shutdown.is_set() and not temp_part.exists() and not temp_final.exists():
                    self.report("download", slot, 0, 0, f"{filename[:25]} CANCELLED", filename)
                    continue

                try:
                    total = _head_content_length(url, self.headers) or _range_probe_total(url, self.headers)
                    if temp_final.exists() and total and temp_final.stat().st_size >= total:
                        self.report("download", slot, total, total, f"{filename[:25]} done", filename)
                        self.q_ex.put((temp_final, filename))
                        continue

                    done = temp_part.stat().st_size if temp_part.exists() else 0
                    self.report("download", slot, done, total, f"{filename[:25]}", filename)

                    attempt = 0
                    while True:
                        if self.shutdown.is_set() and not temp_part.exists():
                            self.report("download", slot, 0, 0, f"{filename[:25]} CANCELLED", filename)
                            break
                        attempt += 1
                        h = dict(self.headers)
                        h.setdefault("Accept-Encoding", "identity")
                        if done > 0:
                            h["Range"] = f"bytes={done}-"
                        elif total:
                            h["Range"] = "bytes=0-"
                        try:
                            r = requests.get(url, headers=h, stream=True, timeout=60, allow_redirects=True)
                            if r.status_code >= 500 or r.status_code == 429:
                                raise requests.HTTPError(f"HTTP {r.status_code}")
                            r.raise_for_status()
                            if done > 0 and r.status_code == 200:
                                done = 0
                                try:
                                    temp_part.unlink(missing_ok=True)
                                except Exception:
                                    pass
                            if not total:
                                total = int(r.headers.get("Content-Length", 0)) or total
                            if not total:
                                cr = r.headers.get("Content-Range")
                                if cr:
                                    total = _parse_content_range_total(cr)

                            with open(temp_part, "ab" if done > 0 else "wb") as f:
                                for chunk in r.iter_content(chunk_size=64 * 1024):
                                    if not chunk:
                                        continue
                                    f.write(chunk)
                                    done += len(chunk)
                                    self.report("download", slot, done, total, f"{filename[:25]}", filename)
                                    if self.shutdown.is_set():
                                        break
                            # Done enough?
                            if (total and done >= total) or (not total):
                                break
                            attempt = min(attempt, MAX_DOWNLOAD_TRIES - 1)
                        except Exception:
                            if attempt >= MAX_DOWNLOAD_TRIES:
                                raise
                            self._sleep_backoff(attempt)
                            continue

                    # finalize
                    try:
                        if temp_final.exists():
                            temp_final.unlink()
                    except Exception:
                        pass
                    if temp_part.exists():
                        temp_part.replace(temp_final)
                    self.report("download", slot, total or done, total or done, f"{filename[:25]} done", filename)
                    self.q_ex.put((temp_final, filename))

                except Exception:
                    self.report("download", slot, 0, 0, f"{filename[:25]} FAILED", filename)

            finally:
                if got_item:
                    self.q_dl.task_done()

    def _extract_worker(self, slot):
        CHUNK = 1024 * 1024
        while True:
            got_item = False
            task = None
            filename = None
            try:
                try:
                    task = self.q_ex.get(timeout=0.25)
                    got_item = True
                except queue.Empty:
                    if self.shutdown.is_set():
                        break
                    continue
                if task is None:
                    break

                temp_path, filename = task
                try:
                    if self.extract_zips and zipfile.is_zipfile(temp_path):
                        extract_dir = self.temp_dir / f"ext_{slot}_{filename}"
                        extract_dir.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(temp_path, 'r') as z:
                            infos = [i for i in z.infolist() if not i.is_dir()]
                            total = sum(i.file_size for i in infos) or 1
                            done = 0
                            self.report("extract", slot, 0, total, f"{filename[:25]}", filename)
                            for info in infos:
                                target = extract_dir / info.filename
                                target.parent.mkdir(parents=True, exist_ok=True)
                                with z.open(info) as src, open(target, "wb") as out:
                                    remaining = info.file_size
                                    while remaining > 0:
                                        chunk = src.read(min(CHUNK, remaining))
                                        if not chunk:
                                            break
                                        out.write(chunk)
                                        clen = len(chunk)
                                        remaining -= clen
                                        done += clen
                                        self.report("extract", slot, done, total, f"{filename[:25]}", filename)
                        # done -> queue move and delete zip after
                        self.report("extract", slot, total, total, f"{filename[:25]} done", filename)
                        self.q_mv.put((extract_dir, filename, temp_path))
                    else:
                        size = temp_path.stat().st_size if temp_path.exists() else 1
                        self.report("extract", slot, size, size, f"{filename[:25]} done", filename)
                        self.q_mv.put((temp_path, filename, None))
                except Exception:
                    self.report("extract", slot, 0, 0, f"{filename[:25]} FAILED", filename)
            finally:
                if got_item:
                    self.q_ex.task_done()

    def _move_worker(self, slot):
        CHUNK = 1024 * 1024
        while True:
            got_item = False
            task = None
            filename = None
            try:
                try:
                    task = self.q_mv.get(timeout=0.25)
                    got_item = True
                except queue.Empty:
                    if self.shutdown.is_set():
                        break
                    continue
                if task is None:
                    break

                src, filename, zip_to_delete = task
                try:
                    if src.is_dir():
                        dest = self.dest
                        dest.mkdir(parents=True, exist_ok=True)
                        files = [f for f in src.rglob('*') if f.is_file()]
                        total = sum((f.stat().st_size for f in files), 0) or 1
                        done = 0
                        self.report("move", slot, 0, total, f"{filename[:25]}", filename)
                        for f in files:
                            rel = f.relative_to(src)
                            target = dest / rel
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with open(f, "rb") as rf, open(target, "wb") as wf:
                                while True:
                                    chunk = rf.read(CHUNK)
                                    if not chunk:
                                        break
                                    wf.write(chunk)
                                    clen = len(chunk)
                                    done += clen
                                    self.report("move", slot, done, total, f"{filename[:25]}", filename)
                        # cleanup
                        try:
                            for f in files:
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                            src.rmdir()
                        except Exception:
                            pass
                        if zip_to_delete and zipfile.is_zipfile(zip_to_delete):
                            try:
                                zip_to_delete.unlink()
                            except Exception:
                                pass
                        self.report("move", slot, total, total, f"{filename[:25]} done", filename)
                        self.completed += 1
                        self._pub({"type": "overall", "completed": self.completed, "total": self.total_jobs,
                                   "active": self.active, "stopping": self.shutdown.is_set()})
                    else:
                        dest_file = self.dest / filename
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        size = src.stat().st_size if src.exists() else 1
                        done = 0
                        self.report("move", slot, 0, size, f"{filename[:25]}", filename)
                        with open(src, "rb") as rf, open(dest_file, "wb") as wf:
                            while True:
                                chunk = rf.read(CHUNK)
                                if not chunk:
                                    break
                                wf.write(chunk)
                                clen = len(chunk)
                                done += clen
                                self.report("move", slot, done, size, f"{filename[:25]}", filename)
                        try:
                            os.remove(src)
                        except Exception:
                            pass
                        self.report("move", slot, size, size, f"{filename[:25]} done", filename)
                        self.completed += 1
                        self._pub({"type": "overall", "completed": self.completed, "total": self.total_jobs,
                                   "active": self.active, "stopping": self.shutdown.is_set()})
                except Exception:
                    self.report("move", slot, 0, 0, f"{filename[:25]} FAILED", filename)
            finally:
                if got_item:
                    self.q_mv.task_done()

    # --------- manager -> SSE ---------
    def _manager(self):
        # seed overall row
        self._pub({"type": "overall", "completed": 0, "total": self.total_jobs,
                   "active": self.active, "stopping": False})
        self._pub({"type": "status", "message": "Started"})

        last = {}  # (kind,slot) -> last_done
        while True:
            try:
                kind, slot, done, total, desc, job = self.q_progress.get(timeout=0.25)
            except queue.Empty:
                # idle heartbeat
                self._pub({"type": "heartbeat"})
                if (self.q_dl.unfinished_tasks == 0 and
                    self.q_ex.unfinished_tasks == 0 and
                    self.q_mv.unfinished_tasks == 0):
                    break
                continue

            key = (kind, slot)
            now = time.monotonic()
            st = self.spd_state.get(key)
            if st is None:
                st = {"last_done": 0, "spd_bytes": 0, "spd_t0": now, "rate": 0.0, "job": job}
                self.spd_state[key] = st
            in_prog = not ("FAILED" in (desc or "") or (desc or "").endswith(" done") or (total and done >= total))
            if key not in last:
                if in_prog:
                    self.active[kind] += 1
                    self._pub({"type": "overall", "completed": self.completed, "total": self.total_jobs,
                               "active": self.active, "stopping": self.shutdown.is_set()})

            delta = max(0, done - st["last_done"])
            st["spd_bytes"] += delta
            elapsed = now - st["spd_t0"]
            if elapsed >= self.SPEED_WIN:
                st["rate"] = st["spd_bytes"] / max(1e-6, elapsed) / (1024 * 1024)
                st["spd_bytes"] = 0
                st["spd_t0"] = now
            st["last_done"] = done

            self._pub({
                "type": "update",
                "kind": kind,
                "slot": slot,
                "done": done,
                "total": total,
                "desc": desc,
                "rate": round(st["rate"], 2),
            })

            finished = ("FAILED" in (desc or "")) or (desc or "").endswith(" done") or (total and done >= total)
            if finished and key in last:
                if self.active.get(kind, 0) > 0:
                    self.active[kind] -= 1
                self._pub({"type": "overall", "completed": self.completed, "total": self.total_jobs,
                           "active": self.active, "stopping": self.shutdown.is_set()})
                self.spd_state.pop(key, None)
                last.pop(key, None)
            else:
                last[key] = done

        self._pub({"type": "status", "message": "Finished"})
        # allow a new job to start without restarting the app
        global current_job
        with job_lock:
            current_job = None

    # --------- control ---------
    def start(self):
        for u in self.urls:
            self.q_dl.put(u)

        for i in range(self.conc):
            t = threading.Thread(target=self._download_worker, args=(i,), daemon=True)
            t.start(); self.threads.append(t)
        for i in range(self.conc):
            t = threading.Thread(target=self._extract_worker, args=(i,), daemon=True)
            t.start(); self.threads.append(t)
        for i in range(self.conc):
            t = threading.Thread(target=self._move_worker, args=(i,), daemon=True)
            t.start(); self.threads.append(t)

        self._mgr_thread = threading.Thread(target=self._manager, daemon=True)
        self._mgr_thread.start()

    def stop(self):
        self.shutdown.set()
        drained = 0
        while True:
            try:
                item = self.q_dl.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                drained += 1
            # mark the item we actually removed from the queue as done
            self.q_dl.task_done()
        if drained:
            pubsub.publish({"type": "status", "message": f"Cancelled {drained} queued downloads"})
        for _ in range(self.conc):
            self.q_dl.put(None)
            self.q_ex.put(None)
            self.q_mv.put(None)

    def join(self):
        self.q_dl.join(); self.q_ex.join(); self.q_mv.join()
        for t in self.threads:
            t.join(timeout=1)
        if self._mgr_thread:
            self._mgr_thread.join(timeout=1)


# ---------- Global job holder ----------
job_lock = threading.Lock()
current_job: Optional[DownloaderJob] = None  # type: ignore


# ---------- Routes ----------
@app.route("/")
def index():
    return INDEX_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/start")
def start_job():
    global current_job
    with job_lock:
        if current_job is not None:
            return jsonify({"ok": False, "error": "Job already running"}), 400
        data = request.get_json(force=True, silent=True) or {}
        urls_text = (data.get("urls") or "").strip()
        dest = (data.get("dest") or "").strip()
        conc = int(data.get("concurrency") or 4)
        do_extract = bool(data.get("extract", True))
        referrer = (data.get("referrer") or "").strip()  # optional
        if not urls_text or not dest:
            return jsonify({"ok": False, "error": "urls and dest are required"}), 400
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        current_job = DownloaderJob(
            urls=urls,
            dest=Path(dest),
            concurrency=conc,
            extract_zips=do_extract,
            referrer=referrer
        )
        current_job.start()
        return jsonify({"ok": True})


@app.post("/stop")
def stop_job():
    global current_job
    with job_lock:
        if current_job is None:
            return jsonify({"ok": True, "message": "No job"})
        current_job.stop()
    pubsub.publish({"type": "status", "message": "Stopping… letting active tasks finish"})
    return jsonify({"ok": True})


@app.get("/stream")
def stream():
    q = pubsub.subscribe()

    def gen():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                except queue.Empty:
                    yield "\n\n"
                    continue
                yield f"data: {data}\n\n"
        finally:
            pubsub.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------- Minimal UI (HTML + JS) ----------
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Robin's File Downloader</title>
  <style>
    :root { --bg:#0f1220; --card:#171a2b; --text:#e7e9ff; --muted:#a8b0c6; --accent:#7c9cff; --ok:#38d39f; --warn:#ffd36b; --err:#ff6b6b; }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Roboto,Inter,Arial}
    header{padding:18px 20px;border-bottom:1px solid #242842;background:#131626;position:sticky;top:0;z-index:10}
    h1{margin:0;font-size:18px;letter-spacing:.6px}
    main{max-width:1200px;margin:18px auto;padding:0 16px;display:grid;grid-template-columns:380px 1fr;gap:16px}
    .card{background:var(--card);border:1px solid #23273a;border-radius:14px;padding:14px}
    label{display:block;margin:8px 0 6px;color:var(--muted)} textarea,input,select{width:100%;background:#0f1220;border:1px solid #2a2f47;border-radius:10px;color:var(--text);padding:10px}
    textarea{min-height:140px;resize:vertical}
    .row{display:flex;gap:8px;align-items:center}
    button{background:#1c2140;color:var(--text);border:1px solid #2a2f47;border-radius:10px;padding:10px 12px;cursor:pointer}
    button.primary{background:var(--accent);color:#0a0d1c;border-color:#6b85ff}
    button.danger{background:#3a1c24;border-color:#6b2a39;color:#ffdfe3}
    button:disabled{opacity:.6;cursor:not-allowed}
    .cols{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
    .section-title{margin:8px 0 12px;color:var(--muted);font-weight:600;letter-spacing:.5px}
    .bar{height:16px;background:#0b0e1c;border-radius:8px;overflow:hidden;border:1px solid #252a45}
    .fill{height:100%;background:linear-gradient(90deg, #6b85ff, #8fa2ff);width:0%}
    .meta{display:flex;justify-content:space-between;color:#aeb6d0;font-size:12px;margin-top:6px}
    .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
    .stage{margin-bottom:14px}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;margin-left:6px}
    .cyan{background:#0f2a3a;color:#98d9ff;border:1px solid #235a7a}
    .green{background:#11321f;color:#a7f3c6;border:1px solid #2b6e46}
    .red{background:#3a1b1b;color:#ffd0d0;border:1px solid #6b2a2a}
    .overall{margin-top:10px}
    .mono{font-family:SFMono-Regular,Consolas,Monaco,monospace;font-size:12px}
  </style>
</head>
<body>
  <header><h1>★ ROBIN'S FILE DOWNLOADER ★</h1></header>
  <main>
    <section class="card">
      <div>
        <label>URLs (one per line)</label>
        <textarea id="urls" placeholder="https://host/file1.zip\nhttps://host/file2.zip"></textarea>
      </div>
      <div>
        <label>Destination path</label>
        <input id="dest" placeholder="C:\\Downloads" />
      </div>
      <div>
        <label>Referrer (optional some sites require this to download or get full speed)</label>
        <input id="referrer" placeholder="https://example.com/page-that-links-to-these-files" />
      </div>
      <div class="row">
        <div style="flex:1">
          <label>Concurrency</label>
          <input id="conc" type="number" min="1" max="16" value="4" />
        </div>
        <div style="flex:1">
          <label>&nbsp;</label>
          <label class="row" style="gap:6px"><input id="extract" type="checkbox" checked /> Extract ZIPs</label>
        </div>
      </div>
      <div class="row" style="margin-top:10px">
        <button id="start" class="primary">Start</button>
        <button id="stop" class="danger" disabled>Stop</button>
      </div>
      <div id="status" class="mono" style="margin-top:8px;color:#a8b0c6"></div>
    </section>

    <section class="card">
      <div class="section-title">Live Progress</div>
      <div id="sections"></div>
      <div class="overall">
        <div class="bar"><div id="overallFill" class="fill"></div></div>
        <div class="meta"><span id="overallText">0 / 0</span><span id="activeText" class="mono"></span></div>
      </div>
    </section>
  </main>

  <script>
  const state = {
    conc: 4,
    slots: { download: [], extract: [], move: [] },
    overall: { completed: 0, total: 0, active: {download:0, extract:0, move:0}, stopping:false },
  };

  function el(tag, cls){ const e=document.createElement(tag); if(cls) e.className=cls; return e; }

  function makeStage(title, kind, color){
    const wrap = el('div','stage');
    const h = el('div',''); h.innerHTML = `${title} <span class="pill ${color}">${kind}</span>`; wrap.appendChild(h);
    const grid = el('div','grid');
    for(let i=0;i<state.conc;i++){
      const card = el('div','');
      const bar = el('div','bar'); const fill = el('div','fill'); bar.appendChild(fill);
      const meta = el('div','meta');
      const left = el('span','mono'); left.textContent = `${kind} ${i+1}: –`;
      const right = el('span','mono'); right.textContent = '0.00 MB/s';
      meta.append(left,right);
      card.append(bar,meta);
      grid.appendChild(card);
      state.slots[kind][i] = { left, right, fill, bar, total:0 };
    }
    wrap.appendChild(grid);
    return wrap;
  }

  function renderSections(){
    const box = document.getElementById('sections');
    box.innerHTML = '';
    box.appendChild(makeStage('Downloads','download','cyan'));
    box.appendChild(makeStage('Extract','extract','green'));
    box.appendChild(makeStage('Move','move','red'));
  }

  function setOverall(o){
    state.overall = o;
    const pct = o.total? (o.completed/o.total*100):0;
    document.getElementById('overallFill').style.width = pct+'%';
    document.getElementById('overallText').textContent = `${o.completed} / ${o.total}`;
    document.getElementById('activeText').textContent = `DL:${o.active.download} EX:${o.active.extract} MV:${o.active.move}${o.stopping?' | STOPPING…':''}`;
  }

  function onUpdate(u){
    const slot = state.slots[u.kind]?.[u.slot];
    if(!slot) return;
    const total = u.total || 0;
    const pct = total? (u.done/total*100) : 0;
    slot.fill.style.width = Math.max(0,Math.min(100,pct))+'%';
    slot.left.textContent = `${u.kind} ${u.slot+1}: ${u.desc}`;
    slot.right.textContent = `${(u.rate||0).toFixed(2)} MB/s`;
  }

  // Controls
  document.getElementById('start').onclick = async () => {
    const urls = document.getElementById('urls').value.trim();
    const dest = document.getElementById('dest').value.trim();
    const referrer = document.getElementById('referrer').value.trim();
    const conc = parseInt(document.getElementById('conc').value||'4',10);
    const extract = document.getElementById('extract').checked;
    if(!urls || !dest){ alert('Please provide URLs and destination path'); return; }
    state.conc = conc; renderSections();
    document.getElementById('start').disabled = true;
    document.getElementById('stop').disabled = false;
    document.getElementById('status').textContent = '';
    const res = await fetch('/start', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({urls,dest,concurrency:conc,extract,referrer})
    });
    const j = await res.json().catch(()=>({}));
    if(!res.ok){
      document.getElementById('status').textContent = j.error || 'Failed to start';
      document.getElementById('start').disabled=false;
      document.getElementById('stop').disabled=true;
    }
  };
  document.getElementById('stop').onclick = async () => {
    document.getElementById('stop').disabled = true;
    await fetch('/stop', {method:'POST'});
  };

  // SSE
  renderSections();
  const es = new EventSource('/stream');
  es.onmessage = (e)=>{
    if(!e.data) return;
    let data; try{ data = JSON.parse(e.data); }catch{ return; }
    if(data.type==='update') onUpdate(data);
    else if(data.type==='overall') setOverall(data);
    else if(data.type==='status') document.getElementById('status').textContent = data.message||'';
  };
  es.onerror = ()=>{ /* ignore */ };
  </script>
</body>
</html>
"""


# ---------- Entrypoint ----------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run the web UI.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)
