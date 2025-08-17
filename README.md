# Robin’s File Downloader

Multi-threaded downloader with resume, ZIP extraction, pretty progress bars, and a simple Flask web UI.
Works great for bulk file grabs or any large URL list.

---

## Features

* ✅ Parallel downloads, extracts, and moves (`--concurrency`)
* ✅ Robust retries with exponential backoff
* ✅ Resume broken downloads (`.part` files)
* ✅ ZIP extraction (can be disabled with `--no-extract`)
* ✅ Pretty, colored progress bars with per-stage speeds
* ✅ Overall progress line with averages & ETA
* ✅ Graceful Ctrl-C (stop queuing new work; let active tasks finish)
* ✅ Windows-friendly (UNC paths supported)

---

## Requirements

* **Python 3.8+** (3.10+ recommended)
* Works on Windows, macOS, Linux

If you’re on Python **< 3.10** and you use modern type hints, either add
`from __future__ import annotations` at the top of your scripts **or**
install `typing-extensions` (included below).

---

## Install

Clone or copy the project files, then:

```bash
# (optional) create a virtual environment
python -m venv .venv
# Windows:
#   .venv\Scripts\activate
# macOS/Linux:
#   source .venv/bin/activate

# upgrade pip (good practice)
python -m pip install -U pip

# install deps
pip install -r requirements.txt
```

**requirements.txt**

```txt
requests>=2.31.0
tqdm>=4.66.0
Flask>=2.3
colorama>=0.4.6; platform_system=="Windows"    # optional: nicer colors on Windows
typing-extensions>=4.7; python_version<"3.10"  # only if you keep modern hints on <3.10
```

Don’t want to use `requirements.txt`? You can do:

```bash
pip install "requests>=2.31.0" "tqdm>=4.66.0" "Flask>=2.3" \
  "colorama>=0.4.6; platform_system=='Windows'" \
  "typing-extensions>=4.7; python_version<'3.10'"
```

---

## Prepare your URL list

Create a plain text file (e.g. `urls.txt`) with **one URL per line**:

```
https://example.com/file1.zip
https://example.com/file2.zip
...
```

---

## CLI Usage

The core script accepts these arguments:

```bash
python main.py --urls <FILE> --dest <FOLDER> [--concurrency N] [--no-extract]
```

### Arguments

* `-u, --urls FILE` **(required)**
  Path to a text file with one URL per line.

* `-d, --dest FOLDER` **(required)**
  Destination folder (local or UNC).
  Windows UNC example: `\\192.168.2.8\downloads`

* `-c, --concurrency N` (default: `4`)
  Number of parallel workers for download & extract/move.

* `--no-extract`
  Don’t extract ZIPs; just download/move them.

* `-r, --referrer`
  Some sites don't allow download with incorrect referrer or slow download speeds, you can set this optionally

### Examples

**Basic:**

```bash
python main.py -u urls.txt -d ./downloads -r "https://www.example.com"
```

**UNC destination on Windows (CMD):**

```bat
python main.py -u urls.txt -d \\192.168.2.8\downloads
```

**UNC destination on PowerShell (quote the path):**

```powershell
python .\main.py -u .\urls.txt -d '\\192.168.2.8\downloads'
```

**Change concurrency + disable extraction:**

```bash
python main.py -u urls.txt -d ./downloads -c 8 --no-extract
```

---

## Start the Flask Web UI

There’s a simple web UI for configuring and running jobs in your browser.

```bash
python downloader_web.py
```

Then open: **[http://127.0.0.1:5000/](http://127.0.0.1:5000/)**

From the UI you can:

* Select/enter the URL list file and destination folder
* Set concurrency, toggle extraction
* Start a job and watch live progress bars (mirrors the CLI display)
* Cancel gracefully via the terminal (Ctrl-C) if needed

> Want a different host/port? Edit `app.run(host=..., port=...)` near the bottom of `downloader_web.py`.

---

## Runtime Notes

* **Resume:** Partially downloaded files are kept as `*.part`. The script will resume them automatically when re-run.
* **Retries:** Each request is retried with exponential backoff on transient errors (5xx, 429, timeouts).
* **Ctrl-C:** Press once to stop queuing new downloads; current downloads/extracts/moves will finish. Press again if you truly must terminate.
* **ZIPs:** When extraction is enabled, completed extractions are moved to `--dest`, and the original ZIP in the temp folder is removed.

---

## Temp & Destination Folders

* Temporary files live under `./temp` next to the script (created automatically).
* Destination is whatever you pass as `--dest`. Make sure you have write permissions.
* On Windows UNC shares, ensure you’re authenticated/mounted with the right credentials.

---

## Troubleshooting

* **Progress looks “laggy”:** The UI coalesces updates to keep the display smooth.
  Actual file writes happen in worker threads; the bars may trail slightly.
* **Totals show “unknown”:** Some servers omit `Content-Length`. The script probes
  via `HEAD` and `Range`, but not all servers cooperate.
* **Permission denied (UNC):** Verify your user has write access to the share and
  that the path exists. Try mapping the share or launching the terminal with proper creds.
* **Type hint errors on Python 3.8/3.9:** Either add
  `from __future__ import annotations` at the top of the files, or rely on
  `typing-extensions` (installed above).

---

## License

Use it freely for your own projects. If you redistribute, please include attribution.
