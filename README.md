# Page Diff — Image Comparison Tool

Compares images across 2-4 local folders to find which pages match which,
using [imagededup](https://github.com/idealo/imagededup) perceptual
hashing. Built for cases like "I downloaded two scans of the same comic
and one has 123 pages, the other 125 — which is missing pages, and
where?"

Matching is **content-based**, not filename- or position-based: for every
image in the first ("reference") folder, the tool finds the closest
matching image in each of the other folders by visual similarity,
wherever it happens to sit in that folder. Pages with no good match show
up as empty slots (missing pages); pages in the other folders that never
got matched to anything show up in a separate "Extra / unmatched" list
(likely extra/duplicate pages).

## Architecture

- **Server**: FastAPI + uvicorn (`main.py`). Reads folders from disk,
  hashes images with imagededup, does the matching, and serves image
  bytes back to the browser. It also mounts a `static/` folder (which
  should contain `index.html`) and serves it at `/`.
- **Client**: a single self-contained `index.html` (plain HTML/CSS/vanilla
  JS, no build step, no frameworks) served from `static/`.
- **Jobs run in the background**: a comparison can take a while on large
  folders (blank-page detection, hashing every image, then an
  O(reference × other) pairwise match per folder), so `POST /api/compare`
  just starts a background thread and returns a `job_id` immediately.
  The client then opens a Server-Sent Events connection
  (`GET /api/compare/stream/{job_id}`) to receive live progress updates
  (per-stage: scanning → hashing → matching, per folder) and the final
  result. Finished jobs are kept in memory for about an hour, then
  cleaned up.

## Setup

1. Clone the repository to your local machine:

```bash
git clone https://github.com/makercyf/page-diff.git
```

2. Navigate to the project directory:

```bash
cd page-diff
```

3. Install the required dependencies:

```bash
uv sync
```

Alternatively, install the dependencies with `pip`:

```bash
pip install -r requirements.txt
```

### Run the application

If you installed the dependencies with `uv`:

```bash
uv run main.py
```

If you installed them with `pip`:

```bash
python main.py
```

Then open **http://127.0.0.1:65500** in your browser.

(`imagededup` pulls in a fair number of dependencies the first time —
the install can take a while.)

### Run the application (Easy way in Windows)

With `uv` installed, double-click `start.bat`, or run it from Command
Prompt:

```bat
start.bat
```

This starts the server in a separate Command Prompt window and opens
**http://127.0.0.1:65500** in your browser. Keep the server window open
while using the app; close it to stop the server.

## Usage

1. Enter 2-4 **absolute** folder paths (paths are read from the machine
   running the server). The first path is the reference folder. You can
   add up to 4 folders or remove rows down to a minimum of 2.
2. Pick a hash method:
   - **PHash** (default) — robust general-purpose choice, tolerant of
     resizing/recompression.
   - **DHash** — fast, edge-sensitive; often good for line art/manga.
   - **WHash** — tolerant of scan noise and small distortions.
   - **AHash** — fastest/simplest, best for near-identical pages only.
3. "Skip blank pages" filters out near-solid white/black filler pages
   before comparison (on by default).
4. Click **Compare pages**. A progress bar and status line track the run
   live (scanning → hashing → matching, per folder) via a
   Server-Sent-Events stream, so you can see where a large comparison is
   without the page hanging.

Each result row shows the reference page next to its best match in every
other folder, with a similarity percentage badge (green ≥90%, amber
70-89%, red <70%). An empty dashed slot means no good match was found in
that folder — a likely missing page. The "Extra / unmatched pages"
section below lists pages in the other folders that didn't match
anything in the reference folder — likely extra or duplicate pages.
Clicking any thumbnail opens it full-size in a new tab.

**Scroll lock** toggle: when locked (default), everything scrolls
together as one list, so a row always shows commensurate pages side by
side. Unlocked splits each folder into its own independently-scrolling
pane, useful for browsing one folder ahead/behind without disturbing the
others.

**Advanced** (collapsed by default):
- *Minimum match similarity* — pairs scoring below this are left
  unmatched rather than forced together. Raise it if you're seeing
  obviously-wrong low-confidence pairings; lower it if genuinely matching
  pages aren't being paired.
- *Blank page sensitivity* — how strict the blank-page filter is.

## How matching works

For each reference-folder image, the tool ranks every possible pairing
against the other folder by hash similarity, then greedily assigns the
highest-similarity pairs first (each image used at most once per folder
pair). This is done independently per folder pair, so with 3-4 folders
each non-reference folder is matched against the reference on its own —
matches across two non-reference folders aren't computed directly.

This is a heuristic, not a guarantee: near-duplicate or visually simple
pages (e.g. mostly blank panels, repeated borders) can occasionally
produce a low-confidence match. Treat amber/red badges as "worth a
visual double-check," not certainty.

## Notes / limitations

- This is meant for **local, single-user use**. The image endpoint will
  serve any file path it's given with an image extension, and CORS is
  wide open — don't expose this server on an untrusted network.
- Supported extensions: jpg, jpeg, png, bmp, webp, gif, tif, tiff.
- Large folders (hundreds of high-resolution images) mean hundreds of
  thousands of pairwise comparisons — this is fast for hashing but can
  take a few seconds to minutes; there's no pagination on the results
  list yet.
- Job results live in server memory only (not persisted to disk) and are
  discarded after about an hour, or if the server restarts.
