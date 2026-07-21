"""
Image Dedup Compare - FastAPI backend

Compares images across 2-4 local folders using imagededup hashing methods
(PHash / DHash / WHash / AHash) to find the best content match for every
image in a reference folder inside each of the other folders. Useful for
spotting missing or extra pages between two "same" scans of a comic/book.
"""

import json
import mimetypes
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, field_validator

from imagededup.methods import AHash, DHash, PHash, WHash

SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

HASHERS = {
    "phash": PHash,
    "dhash": DHash,
    "whash": WHash,
    "ahash": AHash,
}

CLIENT_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Image Dedup Compare")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def natural_sort_key(name: str):
    """Sort strings the human way: '2' before '10'."""
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", name)]


def is_blank_image(path: str, std_threshold: float = 6.0) -> bool:
    """A page is treated as blank if it is (almost) a single flat color,
    e.g. a full white or full black filler page. Measured as the standard
    deviation of a downsized grayscale copy."""
    try:
        with Image.open(path) as img:
            img = img.convert("L")
            img.thumbnail((200, 200))
            arr = np.asarray(img, dtype=np.float32)
            return float(arr.std()) < std_threshold
    except Exception:
        return False


def get_hasher(method: str):
    cls = HASHERS.get(method, PHash)
    return cls()


def list_images(folder: str, skip_blank: bool, blank_threshold: float, progress_cb=None):
    p = Path(folder).expanduser()
    if not p.exists():
        raise ValueError(f"Path does not exist: {folder}")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {folder}")

    files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXT]
    files.sort(key=lambda f: natural_sort_key(f.name))

    result = []
    blanks = 0
    total = len(files)
    for i, f in enumerate(files, start=1):
        if skip_blank and is_blank_image(str(f), blank_threshold):
            blanks += 1
        else:
            result.append(str(f.resolve()))
        if progress_cb and total:
            progress_cb(i, total)

    if not result:
        raise ValueError(f"No usable images found in: {folder}")

    return result, blanks


def encode_folder(hasher, paths: List[str], progress_cb=None):
    hashes = {}
    failed = 0
    total = len(paths)
    for i, pth in enumerate(paths, start=1):
        try:
            h = hasher.encode_image(image_file=pth)
        except Exception:
            h = None
        if h is None:
            failed += 1
        else:
            hashes[pth] = h
        if progress_cb and total:
            progress_cb(i, total)
    return hashes, failed


def greedy_match(hasher, ref_paths, ref_hashes, other_paths, other_hashes, min_similarity=50.0, progress_cb=None):
    """Content-based one-to-one matching: rank every possible pair by
    similarity, then greedily assign the best pairs first so each image
    is used at most once on either side. Pairs below min_similarity are
    never assigned - a weak "best available" pairing is worse than no
    pairing, since it hides a genuinely missing/extra page."""
    candidate_ref = [p for p in ref_paths if p in ref_hashes]
    candidate_other = [p for p in other_paths if p in other_hashes]

    total_pairs = len(candidate_ref) * len(candidate_other)
    # Report at most ~200 times rather than on every pair - reporting
    # progress on every single comparison would add real overhead once
    # folders get into the thousands of images.
    report_every = max(1, total_pairs // 200) if total_pairs else 1

    pairs = []
    max_bits = None
    done = 0
    for rp in candidate_ref:
        rh = ref_hashes[rp]
        if max_bits is None:
            max_bits = len(rh) * 4
        for op in candidate_other:
            oh = other_hashes[op]
            dist = hasher.hamming_distance(rh, oh)
            sim = (1 - dist / max_bits) * 100
            pairs.append((sim, rp, op))
            done += 1
            if progress_cb and (done % report_every == 0 or done == total_pairs):
                progress_cb(done, total_pairs)

    pairs.sort(key=lambda x: -x[0])

    matched_ref = {}
    used_other = set()
    for sim, rp, op in pairs:
        if sim < min_similarity:
            break  # sorted descending: nothing after this qualifies either
        if rp in matched_ref or op in used_other:
            continue
        matched_ref[rp] = (op, sim)
        used_other.add(op)

    unmatched_other = [p for p in candidate_other if p not in used_other]
    return matched_ref, unmatched_other


def build_comparison(paths: List[str], method: str, skip_blank: bool, blank_threshold: float, min_similarity: float = 50.0, progress_cb=None):
    hasher = get_hasher(method)
    n = len(paths)
    # Stages: scan each folder, encode each folder, match each non-reference
    # folder against the reference. Used only for a coarse overall percentage;
    # the per-stage current/total gives the finer detail.
    total_stages = (n * 2) + (n - 1)
    stage_index = 0

    def emit(stage, folder_index, current, total, message):
        if progress_cb:
            progress_cb({
                "stage": stage,
                "folder_index": folder_index,
                "stage_index": stage_index,
                "total_stages": total_stages,
                "current": current,
                "total": total,
                "message": message,
            })

    folder_images = []
    blank_counts = []
    for idx, p in enumerate(paths):
        def scan_cb(cur, tot, idx=idx):
            emit("scanning", idx, cur, tot, f"Scanning folder {idx + 1} of {n}")
        imgs, blanks = list_images(p, skip_blank, blank_threshold, progress_cb=scan_cb)
        folder_images.append(imgs)
        blank_counts.append(blanks)
        stage_index += 1

    all_hashes = []
    encode_fail_counts = []
    for idx, imgs in enumerate(folder_images):
        def encode_cb(cur, tot, idx=idx):
            emit("encoding", idx, cur, tot, f"Hashing images in folder {idx + 1} of {n}")
        hashes, failed = encode_folder(hasher, imgs, progress_cb=encode_cb)
        all_hashes.append(hashes)
        encode_fail_counts.append(failed)
        stage_index += 1

    ref_paths = [p for p in folder_images[0] if p in all_hashes[0]]
    if not ref_paths:
        raise ValueError("None of the images in the reference (first) folder could be read.")

    match_maps = []
    unmatched_lists = []
    for idx in range(1, n):
        def match_cb(cur, tot, idx=idx):
            emit("matching", idx, cur, tot, f"Matching folder {idx + 1} of {n} against reference")
        matched_ref, unmatched_other = greedy_match(
            hasher, ref_paths, all_hashes[0], folder_images[idx], all_hashes[idx], min_similarity, progress_cb=match_cb
        )
        match_maps.append(matched_ref)
        unmatched_lists.append(unmatched_other)
        stage_index += 1

    emit("finalizing", None, 1, 1, "Building comparison table")

    rows = []
    for rp in ref_paths:
        row = {
            "reference": {"path": rp, "name": os.path.basename(rp)},
            "matches": [],
        }
        for idx in range(1, len(paths)):
            m = match_maps[idx - 1].get(rp)
            if m:
                op, sim = m
                row["matches"].append({"path": op, "name": os.path.basename(op), "similarity": round(sim, 2)})
            else:
                row["matches"].append(None)
        rows.append(row)

    extra_rows = []
    for idx in range(1, len(paths)):
        leftovers = sorted(unmatched_lists[idx - 1], key=lambda x: natural_sort_key(os.path.basename(x)))
        for op in leftovers:
            extra_rows.append({
                "folder_index": idx,
                "image": {"path": op, "name": os.path.basename(op)},
            })

    stats = {
        "method": method,
        "min_similarity": min_similarity,
        "folder_counts": [len(fi) for fi in folder_images],
        "blank_skipped": blank_counts,
        "encode_failed": encode_fail_counts,
        "matched_rows": len(rows),
        "extra_count": len(extra_rows),
    }

    return rows, extra_rows, stats


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class CompareRequest(BaseModel):
    paths: List[str]
    method: Literal["phash", "dhash", "whash", "ahash"] = "phash"
    skip_blank: bool = True
    blank_threshold: float = 6.0
    min_similarity: float = 50.0

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, v):
        if len(v) < 2 or len(v) > 4:
            raise ValueError("Provide between 2 and 4 folder paths")
        cleaned = [x.strip() for x in v]
        if any(not x for x in cleaned):
            raise ValueError("Folder paths cannot be empty")
        return cleaned


# ---------------------------------------------------------------------------
# Background jobs + SSE
#
# The comparison can take a long time on large folders (image I/O for blank
# detection, hashing every image, then an O(ref x other) pairwise match), so
# it runs in a background thread instead of inside the request handler. The
# client starts a job, then opens a Server-Sent Events connection to receive
# progress updates and the final result. SSE (rather than WebSockets) is
# enough here since only the server needs to push updates.
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    status: str = "queued"  # queued -> running -> done | error
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 3600  # how long a finished job's data is kept around


def cleanup_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items() if j.status in ("done", "error") and j.created_at < cutoff]
        for jid in stale:
            JOBS.pop(jid, None)


def run_compare_job(job_id: str, req: "CompareRequest"):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.status = "running"

    def progress_cb(info: Dict[str, Any]):
        with JOBS_LOCK:
            job.progress = info

    try:
        rows, extra_rows, stats = build_comparison(
            req.paths, req.method, req.skip_blank, req.blank_threshold, req.min_similarity,
            progress_cb=progress_cb,
        )
        with JOBS_LOCK:
            job.result = {
                "folder_labels": req.paths,
                "rows": rows,
                "extra_rows": extra_rows,
                "stats": stats,
            }
            job.status = "done"
    except ValueError as e:
        with JOBS_LOCK:
            job.error = str(e)
            job.status = "error"
    except Exception as e:
        with JOBS_LOCK:
            job.error = f"Unexpected error: {e}"
            job.status = "error"


def sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/compare", status_code=202)
def start_compare(req: CompareRequest):
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = Job(id=job_id)
    threading.Thread(target=run_compare_job, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/compare/stream/{job_id}")
def stream_compare(job_id: str):
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(status_code=404, detail="Job not found")

    def event_gen():
        last_progress = None
        while True:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    yield sse_event("job_error", {"detail": "Job not found"})
                    return
                status = job.status
                progress = job.progress
                result = job.result
                error = job.error

            if status == "running" and progress and progress != last_progress:
                yield sse_event("progress", progress)
                last_progress = progress

            if status == "done":
                yield sse_event("result", result)
                return
            if status == "error":
                yield sse_event("job_error", {"detail": error})
                return

            time.sleep(0.25)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/image")
def get_image(path: str = Query(...)):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    if p.suffix.lower() not in SUPPORTED_EXT:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    media_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=media_type)

app.mount("/static", StaticFiles(directory=str(CLIENT_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(CLIENT_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=65500)
