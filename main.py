import requests
from pathlib import Path
from urllib.parse import unquote
from tqdm import tqdm
import shutil
import zipfile
import threading
import queue
import os
import time
import random
import signal

# Optional: ANSI colors on Windows
try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    pass

# ---------------- Configuration ----------------
# --- CLI: urls, concurrency, no-extract ---
import argparse

def parse_cli_args():
    p = argparse.ArgumentParser(
        description="Robin's file downloader with progress bars, resume, unzip, and graceful shutdown."
    )
    p.add_argument(
        "-u", "--urls",
        required=True,
        help="Path to text file with one URL per line (required)"
    )
    p.add_argument(
        "-d", "--dest",
        required=True,
        help=r"Destination folder (local or UNC) (required), e.g. \\192.168.2.8\roms\Games\roms\psp"
    )
    p.add_argument(
        "-c", "--concurrency",
        type=int,
        default=4,
        help="Parallel workers per stage (downloads & extract/move). Default: 4"
    )
    p.add_argument(
        "--no-extract",
        action="store_true",
        help="Do not extract zip files; just move them to the destination."
    )
    p.add_argument("--retries", type=int, default=5,
        help="Max download attempts per URL. Default: 5")
    
    p.add_argument( "-r", "--referrer", default="https://www.example.com",
        help="Referrer some sites require correct referrer: https://www.example.com")
    return p.parse_args()

args = parse_cli_args()

# ---------------- Configuration ----------------
url_file = args.urls

download_dir = Path(args.dest)
download_dir.mkdir(exist_ok=True, parents=True)

temp_dir = Path(__file__).parent / "temp"
temp_dir.mkdir(exist_ok=True)

# Flip based on --no-extract
extract_zips = not args.no_extract

# Use the same concurrency for downloads and for extract/move
max_downloads = max(1, args.concurrency)
max_extracts  = max(1, args.concurrency)
MAX_DOWNLOAD_TRIES = max(1, args.retries)

referrer = args.referrer
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": referrer
}


# Graceful shutdown + cancellation counters
shutdown_event = threading.Event()
cancelled_downloads = 0
cancel_lock = threading.Lock()

# ---------------- Queues ----------------
progress_queue = queue.Queue()      # for progress updates
download_queue = queue.Queue()      # download tasks
extract_queue = queue.Queue()       # extract tasks
move_queue = queue.Queue()          # move tasks

# ---------------- Helpers ----------------
def _parse_content_range_total(value: str) -> int:
    try:
        return int(value.split("/")[-1])
    except Exception:
        return 0

def _head_content_length(url: str, headers: dict) -> int:
    try:
        rh = requests.head(url, headers=headers, allow_redirects=True, timeout=30)
        rh.raise_for_status()
        return int(rh.headers.get("Content-Length", 0))
    except Exception:
        return 0

def _range_probe_total(url: str, headers: dict) -> int:
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    try:
        rp = requests.get(url, headers=h, stream=True, timeout=30, allow_redirects=True)
        rp.raise_for_status()
        cr = rp.headers.get("Content-Range", "")
        return _parse_content_range_total(cr)
    except Exception:
        return 0

def report(kind, slot, done_bytes, total_bytes, desc, job):
    """Send progress (BYTES) + a job key (filename) to the UI loop."""
    progress_queue.put((kind, slot, int(done_bytes), int(total_bytes), desc, job))

def _sleep_backoff(attempt: int):
    base = BACKOFF_BASE * (BACKOFF_FACTOR ** (attempt - 1))
    jitter = base * BACKOFF_JITTER
    time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))

# ---------------- Workers ----------------
def download_worker(slot):
    while True:
        url = download_queue.get()  # BLOCK
        try:
            if url is None:  # sentinel
                break

            filename = unquote(url.split("/")[-1] or "file.zip")
            temp_final = temp_dir / filename
            temp_partial = temp_dir / (filename + ".part")

            # If shutdown requested: skip brand-new downloads (but allow resumes/active)
            if shutdown_event.is_set() and not temp_partial.exists() and not temp_final.exists():
                report("download", slot, 0, 0, f"{filename[:25]} CANCELLED", filename)
                break

            total = _head_content_length(url, headers) or _range_probe_total(url, headers)

            # Already fully present from a previous run?
            if temp_final.exists() and (total > 0) and temp_final.stat().st_size >= total:
                report("download", slot, total, total, f"{filename[:25]} done", filename)
                extract_queue.put((temp_final, filename))
                continue

            done = temp_partial.stat().st_size if temp_partial.exists() else 0
            report("download", slot, done, total, f"{filename[:25]}", filename)

            attempt = 0
            while True:
                attempt += 1
                h = dict(headers)
                h.setdefault("Accept-Encoding", "identity")
                if done > 0:
                    h["Range"] = f"bytes={done}-"
                elif total > 0:
                    h["Range"] = "bytes=0-"

                try:
                    r = requests.get(url, headers=h, stream=True, timeout=60, allow_redirects=True)
                    if r.status_code >= 500 or r.status_code == 429:
                        raise requests.HTTPError(f"HTTP {r.status_code}")
                    r.raise_for_status()

                    # If resume ignored by server, start from scratch
                    if done > 0 and r.status_code == 200:
                        done = 0
                        try:
                            temp_partial.unlink(missing_ok=True)
                        except Exception:
                            pass

                    if total == 0:
                        total = int(r.headers.get("Content-Length", 0)) or total
                    if total == 0:
                        cr = r.headers.get("Content-Range")
                        if cr:
                            total = _parse_content_range_total(cr)

                    with open(temp_partial, "ab" if done > 0 else "wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            report("download", slot, done, total, f"{filename[:25]}", filename)

                    # Completed?
                    if (total > 0 and done >= total) or (total == 0):
                        break

                    attempt = min(attempt, MAX_DOWNLOAD_TRIES - 1)

                except Exception:
                    if shutdown_event.is_set() and not temp_partial.exists():
                        report("download", slot, 0, 0, f"{filename[:25]} CANCELLED", filename)
                        break
                    if attempt >= MAX_DOWNLOAD_TRIES:
                        raise
                    _sleep_backoff(attempt)
                    continue

            # Move .part -> final
            try:
                if temp_final.exists():
                    temp_final.unlink()
            except Exception:
                pass
            if temp_partial.exists():
                temp_partial.replace(temp_final)

            report("download", slot, total or done, total or done, f"{filename[:25]} done", filename)
            extract_queue.put((temp_final, filename))

        except Exception:
            report("download", slot, 0, 0, f"{filename[:25]} FAILED", filename)
        finally:
            download_queue.task_done()

def extract_worker(slot):
    CHUNK = 1024 * 1024  # 1 MiB
    while True:
        try:
            task = extract_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            if task is None:  # sentinel
                break

            temp_path, filename = task
            if extract_zips and zipfile.is_zipfile(temp_path):
                extract_dir = temp_dir / f"ext_{slot}_{filename}"
                extract_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(temp_path, 'r') as z:
                    infos = [i for i in z.infolist() if not i.is_dir()]
                    total = sum(i.file_size for i in infos) or 1
                    done = 0

                    report("extract", slot, 0, total, f"{filename[:25]}", filename)

                    for info in infos:
                        target = extract_dir / info.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(info) as source, open(target, "wb") as out:
                            remaining = info.file_size
                            while remaining > 0:
                                chunk = source.read(min(CHUNK, remaining))
                                if not chunk:
                                    break
                                out.write(chunk)
                                clen = len(chunk)
                                remaining -= clen
                                done += clen
                                report("extract", slot, done, total, f"{filename[:25]}", filename)

                # extraction completed OK — queue the move and pass the zip path so it can be deleted after move
                report("extract", slot, total, total, f"{filename[:25]} done", filename)
                move_queue.put((extract_dir, filename, temp_path))  # <-- pass zip path here
            else:
                # Not a zip (or extraction disabled)
                try:
                    size = temp_path.stat().st_size
                except Exception:
                    size = 1
                if size <= 0:
                    size = 1
                report("extract", slot, size, size, f"{filename[:25]} done", filename)
                move_queue.put((temp_path, filename))
        except Exception:
            report("extract", slot, 0, 0, f"{filename[:25]} FAILED", filename)
        finally:
            extract_queue.task_done()


def move_worker(slot):
    CHUNK = 1024 * 1024  # 1 MiB
    while True:
        try:
            task = move_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            if task is None:  # sentinel
                break

            # Support both (src, filename) and (src, filename, zip_to_delete)
            if isinstance(task, tuple) and len(task) == 3:
                src, filename, zip_to_delete = task
            else:
                src, filename = task
                zip_to_delete = None

            if src.is_dir():
                dest = download_dir
                dest.mkdir(parents=True, exist_ok=True)
                files = [f for f in src.rglob('*') if f.is_file()]
                total = sum((f.stat().st_size for f in files), 0) or 1
                done = 0

                report("move", slot, 0, total, f"{filename[:25]}", filename)

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
                            report("move", slot, done, total, f"{filename[:25]}", filename)

                # clean extracted folder
                shutil.rmtree(src, ignore_errors=True)

                # delete original zip *after* a successful move
                if zip_to_delete and zip_to_delete.exists():
                    try:
                        zip_to_delete.unlink()
                    except Exception:
                        pass

                report("move", slot, total, total, f"{filename[:25]} done", filename)
                # bump overall
                report("overall", 0, 1, TOTAL_JOBS, f"{filename[:25]} done", "__overall__")

            else:
                # Single file (non-zip)
                dest_file = download_dir / filename
                dest_file.parent.mkdir(parents=True, exist_ok=True)

                try:
                    size = src.stat().st_size
                except Exception:
                    size = 1
                if size <= 0:
                    size = 1
                done = 0

                report("move", slot, 0, size, f"{filename[:25]}", filename)

                with open(src, "rb") as rf, open(dest_file, "wb") as wf:
                    while True:
                        chunk = rf.read(CHUNK)
                        if not chunk:
                            break
                        wf.write(chunk)
                        clen = len(chunk)
                        done += clen
                        report("move", slot, done, size, f"{filename[:25]}", filename)

                # remove the source file (this was the original downloaded file)
                try:
                    os.remove(src)
                except Exception:
                    pass

                report("move", slot, size, size, f"{filename[:25]} done", filename)
                report("overall", 0, 1, TOTAL_JOBS, f"{filename[:25]} done", "__overall__")

        except Exception:
            report("move", slot, 0, 0, f"{filename[:25]} FAILED", filename)
        finally:
            move_queue.task_done()

# ---------------- Main progress manager ----------------
def progress_manager():
    import time

    bars = {}   # (kind,slot) -> tqdm bar
    state = {}  # (kind,slot) -> per-bar state OR for overall: {'count'}
    active = {"download": 0, "extract": 0, "move": 0}  # live task counts

    # colors (download=cyan, extract=green, move=red, overall=magenta)
    COLORS = {"download": "\033[96m", "extract": "\033[92m", "move": "\033[91m", "overall": "\033[95m"}
    RESET = "\033[0m"
    SPEED_WINDOW_SEC = 0.35  # fast smoothing for snappy UI

    # --- layout positions (title + 4 dividers + status line at bottom) ---
    POS_TITLE   = 0
    POS_DIV0    = POS_TITLE + 1                 # NEW: "DOWNLOADS"
    POS_DL_START= POS_DIV0 + 1
    POS_DIV1    = POS_DL_START + max_downloads  # "EXTRACT"
    POS_EX_START= POS_DIV1 + 1
    POS_DIV2    = POS_EX_START + max_extracts   # "MOVE"
    POS_MV_START= POS_DIV2 + 1
    POS_DIV3    = POS_MV_START + max_extracts   # NEW: "OVERALL"
    POS_OVERALL = POS_DIV3 + 1
    POS_STATUS  = POS_OVERALL + 1               # persistent status line

    def row_pos(kind, slot):
        if kind == "download":
            return POS_DL_START + slot
        elif kind == "extract":
            return POS_EX_START + slot
        elif kind == "move":
            return POS_MV_START + slot
        elif kind == "overall":
            return POS_OVERALL
        else:
            return POS_STATUS

    def make_bar(kind, slot, total, color):
        total_val = None if total == 0 else total
        if kind == "overall":
            BAR_FMT = f"{color}" + "{desc}: {n_fmt}/{total_fmt} [{elapsed}] {postfix}" + f"{RESET}"
            return tqdm(
                total=total_val,
                position=row_pos(kind, slot),
                unit="it",
                leave=True,
                bar_format=BAR_FMT,
                mininterval=0.05,
                dynamic_ncols=True,
            )
        else:
            BAR_FMT_KNOWN   = "{desc}: {percentage:3.0f}%|" + color + "{bar}" + RESET + "| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}"
            BAR_FMT_UNKNOWN = "{desc}: " + color + "{n_fmt}" + RESET + " [{elapsed}] {postfix}"
            return tqdm(
                total=total_val,
                position=row_pos(kind, slot),
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                leave=True,
                bar_format=BAR_FMT_UNKNOWN if total_val is None else BAR_FMT_KNOWN,
                mininterval=0.05,
                dynamic_ncols=True,
            )

    def fmt_dur(sec):
        if not sec or sec <= 0:
            return "--"
        s = int(sec)
        if s < 60:
            return f"{s}s"
        m, s2 = divmod(s, 60)
        if m < 60:
            return f"{m}:{s2:02d}"
        h, m2 = divmod(m, 60)
        return f"{h}:{m2:02d}"

    # rolling timing stats per stage
    stage_stats = {
        "download": {"count": 0, "total_time": 0.0},
        "extract":  {"count": 0, "total_time": 0.0},
        "move":     {"count": 0, "total_time": 0.0},
    }

    # --- STATIC LINES: title + dividers + status line ---
    title = tqdm(total=1, position=POS_TITLE, leave=True, bar_format="\033[95;1m{desc}\033[0m", mininterval=0)
    title.desc = "★ ROBINS FILE DOWNLOADER ★"
    title.n = 1
    title.refresh()

    def make_divider(position, text):
        line = tqdm(total=1, position=position, leave=True, bar_format="\033[90m{desc}\033[0m", mininterval=0)
        if text:
            line.desc = "─" * 8 + f" {text} " + "─" * 32
        else:
            line.desc = "─" * 48
        line.n = 1
        line.refresh()
        return line

    # NEW: divider under header before downloads
    div0 = make_divider(POS_DIV0, "DOWNLOADS")
    # Existing section dividers
    div1 = make_divider(POS_DIV1, "EXTRACT")
    div2 = make_divider(POS_DIV2, "MOVE")
    # NEW: divider above overall progress
    div3 = make_divider(POS_DIV3, "OVERALL")

    status = tqdm(total=1, position=POS_STATUS, leave=True, bar_format="\033[90m{desc}\033[0m", mininterval=0)
    status.desc = ""
    status.n = 1
    status.refresh()
    bars[("status", 0)] = status

    def update_status_line():
        if shutdown_event.is_set():
            status.desc = "Ctrl-C received — stopping new downloads, letting active tasks finish…"
            status.refresh()

    def update_overall_postfix():
        obar = bars.get(("overall", 0))
        if obar is None:
            return

        dl_a, ex_a, mv_a = active["download"], active["extract"], active["move"]

        dl_avg = (stage_stats["download"]["total_time"] / stage_stats["download"]["count"]) if stage_stats["download"]["count"] else None
        ex_avg = (stage_stats["extract"]["total_time"]  / stage_stats["extract"]["count"])  if stage_stats["extract"]["count"]  else None
        mv_avg = (stage_stats["move"]["total_time"]     / stage_stats["move"]["count"])     if stage_stats["move"]["count"]     else None

        total_avg = None
        parts = [t for t in (dl_avg, ex_avg, mv_avg) if t is not None]
        if parts:
            total_avg = sum(parts)

        completed = state.get(("overall", 0), {}).get("count", 0)
        remaining = max(0, TOTAL_JOBS - completed)
        eta = (remaining * total_avg) if (total_avg and remaining) else None

        try:
            canc = cancelled_downloads
            canc_str = f" | cancelled:{canc}" if canc else ""
        except NameError:
            canc_str = ""

        extra = " | STOPPING…" if shutdown_event.is_set() else ""
        obar.set_postfix_str(
            "DL:{dl} EX:{ex} MV:{mv} | d:{d} e:{e} m:{m} | item≈{item} ETA≈{eta}{c}{x}".format(
                dl=dl_a, ex=ex_a, mv=mv_a,
                d=fmt_dur(dl_avg), e=fmt_dur(ex_avg), m=fmt_dur(mv_avg),
                item=fmt_dur(total_avg), eta=fmt_dur(eta),
                c=canc_str, x=extra,
            )
        )
        obar.refresh()

    def is_in_progress(done, total, desc):
        d = (desc or "")
        if "FAILED" in d:
            return False
        if d.endswith(" done"):
            return False
        if total > 0 and done >= total:
            return False
        return True

    # --------- MAIN LOOP: drain & coalesce updates each tick ----------
    while True:
        try:
            first = progress_queue.get(timeout=0.1)
        except queue.Empty:
            if (download_queue.unfinished_tasks == 0 and
                extract_queue.unfinished_tasks == 0 and
                move_queue.unfinished_tasks == 0):
                break
            update_status_line()
            update_overall_postfix()  # keep bottom line fresh
            continue

        update_status_line()

        drained = [first]
        while True:
            try:
                drained.append(progress_queue.get_nowait())
            except queue.Empty:
                break

        latest = {}
        overall_incr = 0
        status_msg = None

        for kind, slot, done, total, desc, job in drained:
            if kind == "overall":
                overall_incr += max(0, done)
                continue
            if kind == "status":
                status_msg = desc
                continue
            latest[(kind, slot)] = (kind, slot, done, total, desc, job)

        if status_msg is not None:
            try:
                bars[("status", 0)].desc = status_msg
                bars[("status", 0)].refresh()
            except KeyError:
                pass

        if overall_incr:
            key = ("overall", 0)
            obar = bars.get(key)
            ost  = state.get(key)
            if obar is None:
                obar = make_bar("overall", 0, TOTAL_JOBS, COLORS.get("overall", ""))
                bars[key] = obar
                ost = state[key] = {"count": 0}
                obar.desc = "Overall"
            ost["count"] = min(TOTAL_JOBS, ost["count"] + overall_incr)
            obar.n = ost["count"]
            obar.total = TOTAL_JOBS
            update_overall_postfix()

        now = time.monotonic()
        for (kind, slot), (kind, slot, done, total, desc, job) in latest.items():
            color = COLORS.get(kind, "")
            key = (kind, slot)
            bar = bars.get(key)
            st  = state.get(key)

            if bar is None:
                bar = make_bar(kind, slot, total, color)
                bars[key] = bar
                st = state[key] = {
                    "last_done": 0,
                    "spd_time": now,
                    "spd_bytes": 0,
                    "rate": 0.0,
                    "color": color,
                    "job": job,
                    "active": False,
                    "job_start_time": now,
                }
                if is_in_progress(done, total, desc):
                    st["active"] = True
                    st["job_start_time"] = now
                    active[kind] += 1
                    update_overall_postfix()

            if st["job"] != job:
                if st["active"]:
                    active[kind] = max(0, active[kind] - 1)
                st["job"] = job
                st["last_done"] = 0
                st["spd_time"] = now
                st["spd_bytes"] = 0
                st["rate"] = 0.0
                st["active"] = False
                st["job_start_time"] = now

                bar.n = 0
                bar.last_print_n = 0
                if total > 0:
                    bar.total = total
                    bar.bar_format = "{desc}: {percentage:3.0f}%|" + color + "{bar}" + RESET + "| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}"
                else:
                    bar.total = None
                    bar.bar_format = "{desc}: " + color + "{n_fmt}" + RESET + " [{elapsed}] {postfix}"
                bar.refresh()

                if is_in_progress(done, total, desc):
                    st["active"] = True
                    st["job_start_time"] = now
                    active[kind] += 1
                update_overall_postfix()

            if bar.total is None and total > 0:
                bar.total = total
                bar.bar_format = "{desc}: {percentage:3.0f}%|" + color + "{bar}" + RESET + "| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}"

            delta = max(0, done - bar.n)
            if delta:
                bar.update(delta)
            else:
                bar.n = done
            bar.desc = f"{color}{kind.capitalize()} {slot+1}{RESET}: {desc}"

            if not st["active"] and is_in_progress(done, total, desc):
                st["active"] = True
                st["job_start_time"] = now
                active[kind] += 1
                update_overall_postfix()

            advance = max(0, done - st["last_done"])
            st["spd_bytes"] += advance
            st["last_done"] = done
            elapsed_rate = now - st["spd_time"]
            if elapsed_rate >= SPEED_WINDOW_SEC:
                st["rate"] = st["spd_bytes"] / max(1e-6, elapsed_rate) / (1024 * 1024)
                st["spd_bytes"] = 0
                st["spd_time"] = now

            bar.set_postfix_str(f"{st['rate']:.2f} MB/s")
            bar.refresh()

            success_finish = (total > 0 and done >= total) or (desc or "").endswith(" done")
            failed_finish  = "FAILED" in (desc or "")
            if (success_finish or failed_finish) and st["active"]:
                if success_finish:
                    duration = time.monotonic() - st["job_start_time"]
                    stage_stats[kind]["count"] += 1
                    stage_stats[kind]["total_time"] += max(0.0, duration)
                st["active"] = False
                active[kind] = max(0, active[kind] - 1)
                update_overall_postfix()

        update_overall_postfix()

        if (download_queue.unfinished_tasks == 0 and
            extract_queue.unfinished_tasks == 0 and
            move_queue.unfinished_tasks == 0 and
            progress_queue.empty()):
            break

    for bar in bars.values():
        bar.close()

# ---------------- Run ----------------
with open(url_file) as f:
    urls = [u.strip() for u in f if u.strip()]

TOTAL_JOBS = len(urls)

# overall bar at bottom
report("overall", 0, 0, TOTAL_JOBS, f"{TOTAL_JOBS} URLs", "__overall__")

# enqueue
for u in urls:
    download_queue.put(u)

# start workers
threads_dl, threads_ex, threads_mv = [], [], []
for i in range(max_downloads):
    t = threading.Thread(target=download_worker, args=(i,), daemon=True)
    t.start()
    threads_dl.append(t)
for i in range(max_extracts):
    t = threading.Thread(target=extract_worker, args=(i,), daemon=True)
    t.start()
    threads_ex.append(t)
for i in range(max_extracts):
    t = threading.Thread(target=move_worker, args=(i,), daemon=True)
    t.start()
    threads_mv.append(t)

# ---- SIGINT handler: stop + drain pending downloads (not yet started) ----
def drain_pending_downloads():
    global cancelled_downloads
    drained = 0
    while True:
        try:
            item = download_queue.get_nowait()
        except queue.Empty:
            break
        if item is not None:  # ignore future sentinels
            drained += 1
        download_queue.task_done()
    if drained:
        with cancel_lock:
            cancelled_downloads += drained
        report("status", 0, 0, 0, f"Cancelled {drained} queued downloads", "__status__")

def _on_sigint(signum, frame):
    shutdown_event.set()
    drain_pending_downloads()
    report("status", 0, 0, 0, "Ctrl-C received — stopping new downloads, letting active tasks finish…", "__status__")

signal.signal(signal.SIGINT, _on_sigint)

# UI loop
progress_manager()

# ---------- Graceful shutdown / cleanup ----------
# Stop downloads (send sentinels, then join)
for _ in range(max_downloads):
    download_queue.put(None)
download_queue.join()
for t in threads_dl:
    t.join(timeout=2)

# Drain extract, then stop extract workers
extract_queue.join()
for _ in range(max_extracts):
    extract_queue.put(None)
for t in threads_ex:
    t.join(timeout=2)

# Drain move, then stop move workers
move_queue.join()
for _ in range(max_extracts):
    move_queue.put(None)
for t in threads_mv:
    t.join(timeout=2)

print("All done!")
