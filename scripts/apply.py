"""Build sorted copies and write sorted.csv and duplicates.csv.

Run as apply.py [destination] [--labeler codex]. The default label source still
combines manual/Claude labels with Jev, whose labels win at 70% confidence.
"""
import argparse
import csv
import errno
import glob
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager

from common import (DATA, JEV_OUT, LABELS, PROFILE_ENV, WORK, categories, is_within, load,
                    overrides_path, read_codex_labels, save, sort_destination)


@contextmanager
def apply_lock():
    os.makedirs(WORK, exist_ok=True)
    with open(os.path.join(WORK, ".apply.lock"), "a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError("Another copy run is already active. Wait for it to finish.") from error
            raise
        yield


def choose_labels(recs, cats, labeler):
    if labeler == "codex":
        labels = read_codex_labels(recs, list(cats.values()))
        if len(labels) != len(recs):
            raise RuntimeError(f"Codex labelled {len(labels)} of {len(recs)} files. Finish labelling before copying.")
        return labels, 0, 0

    manual = {}
    for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
        manual.update(load(f, {}))
    jev = load(JEV_OUT, {})
    if not manual and not jev:
        raise RuntimeError("No labels yet. Run labelling first.")

    labels, fixed, missing = {}, 0, 0
    fallback = {c["kind"]: c["key"] for c in cats.values()}
    for r in recs:
        k = str(r["id"])
        j = jev.get(k, {})
        pick = j.get("key") if j.get("confidence", 0) >= 0.7 or k not in manual else manual.get(k)
        pick = pick or manual.get(k)
        if pick is None:
            missing += 1
        if pick not in cats or cats[pick]["kind"] != r["kind"]:
            fixed += pick is not None
            pick = fallback[r["kind"]]
        labels[k] = pick
    return labels, fixed, missing


def copy(src, dst):
    # Clone within an APFS volume when possible. Publish only a complete copy.
    with tempfile.TemporaryDirectory(prefix=".photo-sorter-", dir=os.path.dirname(dst)) as tmp:
        staged = os.path.join(tmp, "copy")
        if sys.platform != "darwin" or subprocess.run(["cp", "-c", "-p", src, staged], capture_output=True).returncode:
            shutil.copy2(src, staged)
        os.replace(staged, dst)


def safe_target(dest, source, entry):
    path = os.path.join(dest, entry)
    if not is_within(dest, path) or (source and is_within(source, path)):
        raise ValueError("A sorted output path points outside the destination or into the photos folder.")
    return path


def remove_previous(manifest, dest, source):
    if os.path.isfile(manifest):
        with open(manifest, newline="") as f:
            for row in csv.DictReader(f):
                path = safe_target(dest, source, row["file"])
                if os.path.isfile(path):
                    os.remove(path)


def check_copy_space(recs, dest, rebuilding):
    device = os.stat(dest).st_dev
    needed = 0
    for r in recs:
        try:
            original = os.stat(r["path"])
        except FileNotFoundError as error:
            raise RuntimeError(f"Could not find {r['name']}. Reconnect the source drive or choose the photos again.") from error
        if sys.platform != "darwin" or original.st_dev != device:
            needed += original.st_size
    if not rebuilding:
        free = shutil.disk_usage(dest).free
        if needed > free:
            raise RuntimeError(f"The copies need {needed / 1e9:.2f} GB and the destination has {free / 1e9:.2f} GB free. "
                               "Free up space or choose another destination.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", nargs="?", default="")
    parser.add_argument("--labeler", choices=["codex"])
    args = parser.parse_args()
    source_file = os.path.join(DATA, "source.json")
    source_info = load(source_file, {})
    source = source_info.get("source", "")
    dest = sort_destination(source, args.dest)
    recs = load(os.path.join(DATA, "inventory.json"), [])
    if not recs:
        raise RuntimeError("Scan a folder before building the sorted copies.")
    cats = {c["key"]: c for c in categories()}
    labels, fixed, missing = choose_labels(recs, cats, args.labeler)
    profile = os.environ.get(PROFILE_ENV, "standard")
    for k, v in load(overrides_path(profile), {}).items():
        if k in labels and v in cats:
            labels[k] = v

    dupes = load(os.path.join(DATA, "dupes.json"), [])
    dupe_of = {d: g for g in dupes for d in g["dupes"]}
    byid = {r["id"]: r for r in recs}
    sorted_csv = os.path.join(dest, "sorted.csv")
    sorted_pending = os.path.join(dest, ".sorted.csv.in-progress")
    dupes_pending = os.path.join(dest, ".duplicates.csv.in-progress")
    os.makedirs(dest, exist_ok=True)
    for manifest in (sorted_csv, sorted_pending, dupes_pending, os.path.join(dest, "duplicates.csv")):
        if os.path.islink(manifest):
            raise ValueError("Choose a destination without linked sorter reports.")
    check_copy_space(recs, dest, os.path.exists(sorted_csv) or os.path.exists(sorted_pending))
    if source_info.get("sorted") and os.path.realpath(source_info["sorted"]) == dest:
        source_info.pop("sorted")
        save(source_file, source_info)
    remove_previous(sorted_csv, dest, source)
    remove_previous(sorted_pending, dest, source)

    with open(sorted_pending, "w", newline="") as fc, open(dupes_pending, "w", newline="") as fd:
        wc, wd = csv.writer(fc), csv.writer(fd)
        wc.writerow(["file", "folder", "original_path"])
        wd.writerow(["duplicate", "kept_copy", "type", "duplicate_path", "kept_path"])
        fc.flush()
        for n, r in enumerate(recs, 1):
            folder = cats[labels[str(r["id"])]]["label"].replace("&", "and").replace("/", "-")
            if r["id"] in dupe_of:
                g = dupe_of[r["id"]]
                k = byid[g["keep"]]
                wd.writerow([r["name"], k["name"], g["type"], r["path"], k["path"]])
                folder = os.path.join("_Review", "duplicates")
            out_dir = safe_target(dest, source, folder)
            os.makedirs(out_dir, exist_ok=True)
            target = os.path.join(out_dir, r["name"])
            if os.path.lexists(target):
                base, ext = os.path.splitext(r["name"])
                target = os.path.join(out_dir, f"{base}__{r['id']}{ext}")
                suffix = 2
                while os.path.lexists(target):
                    target = os.path.join(out_dir, f"{base}__{r['id']}_{suffix}{ext}")
                    suffix += 1
            safe_target(dest, source, target)
            # Record the planned copy before writing it so a retry can clean up after interruption.
            wc.writerow([os.path.relpath(target, dest), folder, r["path"]])
            fc.flush()
            copy(r["path"], target)
            if n % 25 == 0 or n == len(recs):
                print(f"PROGRESS {n} {len(recs)}", flush=True)
            if n % 500 == 0:
                print(f"  copied {n}/{len(recs)}", flush=True)
    os.replace(dupes_pending, os.path.join(dest, "duplicates.csv"))
    os.replace(sorted_pending, sorted_csv)
    save(os.path.join(DATA, "categories.json"), list(cats.values()))
    save(os.path.join(DATA, "labels.json"), labels)
    source_info["sorted"] = dest
    source_info["profile"] = profile
    save(source_file, source_info)

    counts = {}
    for r in recs:
        if r["id"] not in dupe_of:
            label = labels[str(r["id"])]
            counts[label] = counts.get(label, 0) + 1
    print(f"Sorted into {dest}")
    for k, c in cats.items():
        print(f"  {c['label']:24} {counts.get(k, 0)}")
    print(f"  {'_Review/duplicates':24} {len(dupe_of)}")
    if missing or fixed:
        print(f"  ({missing} missing labels and {fixed} wrong-type labels went to the catch-all folders)")


def interrupted(_signal, _frame):
    raise SystemExit(130)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with apply_lock():
            main()
    except OSError as error:
        if error.errno == errno.ENOSPC:
            sys.exit("The destination ran out of space. Free up space or choose another destination, then try again.")
        sys.exit(str(error))
    except (RuntimeError, ValueError) as error:
        sys.exit(str(error))
