"""Shared paths and helpers. Everything the tool writes lives inside this repo:
  dashboard/data/   inventory, thumbnails, labels (what the dashboard reads)
  work/             contact sheets and raw labels (scratch)
Your original photos are only ever read, never changed.
"""
import glob
import hashlib
import json
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "dashboard", "data")
THUMBS = os.path.join(DATA, "thumbs")
WORK = os.path.join(ROOT, "work")
LABELS = os.path.join(WORK, "labels")      # Claude and manual labels
CODEX_LABELS = os.path.join(WORK, "codex")
JEV_OUT = os.path.join(WORK, "jev.json")   # Jev labels (optional)
CATEGORIES = os.path.join(ROOT, "categories.json")

IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VID = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save(path, obj):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as f:
            temp = f.name
            json.dump(obj, f, indent=1)
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.remove(temp)


def codex_cache(recs, cats):
    files = [(r["id"], r["path"], r["sha256"]) for r in recs]
    fingerprint = hashlib.sha256(json.dumps([files, cats], sort_keys=True).encode()).hexdigest()
    return os.path.join(CODEX_LABELS, fingerprint)


def read_codex_labels(recs, cats):
    labels = {}
    for path in glob.glob(os.path.join(codex_cache(recs, cats), "*.json")):
        labels.update(load(path, {}))
    return valid_labels(recs, cats, labels)


def valid_labels(recs, cats, labels):
    kinds = {c["key"]: c["kind"] for c in cats}
    out = {}
    for r in recs:
        key = str(r["id"])
        label = labels.get(key)
        if isinstance(label, str) and kinds.get(label) == r["kind"]:
            out[key] = label
    return out


def sort_destination(source, dest):
    if not dest and not source:
        raise ValueError("Choose where to save the sorted copies.")
    path = os.path.realpath(os.path.expanduser(dest)) if dest else os.path.realpath(source) + " (sorted)"
    if source and is_within(source, path):
        raise ValueError("Choose a destination outside the photos folder.")
    return path


def is_within(folder, path):
    folder = os.path.normcase(os.path.realpath(folder))
    path = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([folder, path]) == folder
    except ValueError:  # different drives on Windows
        return False


def categories():
    cats = load(CATEGORIES)
    keys = [c["key"] for c in cats]
    assert len(keys) == len(set(keys)), "categories.json has duplicate keys"
    for kind in ("photo", "video"):
        assert any(c["kind"] == kind for c in cats), f"categories.json needs at least one {kind} folder"
    return cats
