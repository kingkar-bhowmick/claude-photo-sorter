"""Build sorted copies and write sorted.csv and duplicates.csv.

Run as apply.py [destination] [--labeler codex] [--output-mode add|replace]. The default label source still
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
from collections import defaultdict
from contextlib import contextmanager

from common import (DATA, JEV_OUT, LABELS, PROFILE_ENV, WORK, categories, is_within, load,
                    overrides_path, read_codex_labels, save, sha256_file, sort_destination)

SORT_FIELDS = ("file", "folder", "original_path", "sha256")
DUPE_FIELDS = ("duplicate", "kept_copy", "type", "duplicate_path", "kept_path")
OUTPUT_MODES = ("add", "replace")
REVIEW_FOLDER = os.path.join("_Review", "duplicates")


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
        if os.path.lexists(dst):
            raise FileExistsError(f"Another file appeared at {dst}. Try again to use another name.")
        os.replace(staged, dst)


def safe_target(dest, source, entry):
    path = os.path.join(dest, entry)
    if not is_within(dest, path) or (source and is_within(source, path)):
        raise ValueError("A sorted output path points outside the destination or into the photos folder.")
    return path


def read_report(path, required, pending=False):
    if os.path.islink(path):
        raise ValueError("Choose a destination without linked sorter reports.")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if pending and reader.fieldnames is None:
                return []
            if not reader.fieldnames or not all(field in reader.fieldnames for field in required):
                raise ValueError(f"{os.path.basename(path)} does not look like a sorter report. Choose another destination.")
            return list(reader)
    except FileNotFoundError:
        return []


def write_report(path, fields, rows):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), newline="", encoding="utf-8",
                                         prefix=".photo-sorter-", delete=False) as f:
            temp = f.name
            writer = csv.DictWriter(f, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def local_copies(dest, source, rows):
    """Trust only regular copies inside the destination, away from any recorded original."""
    originals = {os.path.normcase(os.path.realpath(row["original_path"])) for row in rows if row.get("original_path")}
    result = {}
    for row in rows:
        entry = row.get("file")
        if not entry or os.path.isabs(entry) or not row.get("original_path"):
            continue
        target = os.path.abspath(os.path.join(dest, entry))
        if os.path.normcase(target) != os.path.normcase(os.path.realpath(target)):
            continue
        if os.path.normcase(target) in originals or not os.path.isfile(target):
            continue
        if not is_within(dest, target) or (source and is_within(source, target)):
            continue
        entry = os.path.relpath(target, dest)
        result[entry] = {**row, "file": entry, "folder": os.path.dirname(entry)}
    return result


def same_contents(path, digest):
    try:
        return sha256_file(path) == digest
    except (OSError, ValueError):
        return False


def keep_copy(dest, candidates, rec, folder, replacing):
    for row in candidates:
        if replacing and row["folder"] != folder:
            continue
        if row.get("sha256") not in (None, "", rec["sha256"]):
            continue
        if same_contents(os.path.join(dest, row["file"]), rec["sha256"]):
            row["sha256"] = rec["sha256"]
            return row
    return None


def remove_obsolete(dest, previous, kept):
    removed = 0
    for entry, row in previous.items():
        if entry in kept:
            continue
        target = os.path.join(dest, entry)
        digest = row.get("sha256")
        if not digest:
            try:
                digest = sha256_file(row["original_path"])
            except (OSError, ValueError):
                pass
        if digest and os.path.normcase(target) == os.path.normcase(os.path.realpath(target)) and same_contents(target, digest):
            os.remove(target)
            removed += 1
        else:
            kept[entry] = row
    return removed


def check_copy_space(recs, dest):
    device = os.stat(dest).st_dev
    needed = 0
    for r in recs:
        try:
            original = os.stat(r["path"])
        except FileNotFoundError as error:
            raise RuntimeError(f"Could not find {r['name']}. Reconnect the source drive or choose the photos again.") from error
        if sys.platform != "darwin" or original.st_dev != device:
            needed += original.st_size
    free = shutil.disk_usage(dest).free
    if needed > free:
        raise RuntimeError(f"The new copies need {needed / 1e9:.2f} GB and the destination has {free / 1e9:.2f} GB free. "
                           "Free up space or choose another destination.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", nargs="?", default="")
    parser.add_argument("--labeler", choices=["codex"])
    parser.add_argument("--output-mode", choices=OUTPUT_MODES, default="add")
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
    dupes_csv = os.path.join(dest, "duplicates.csv")
    dupes_pending = os.path.join(dest, ".duplicates.csv.in-progress")
    os.makedirs(dest, exist_ok=True)
    pending = read_report(sorted_pending, SORT_FIELDS[:3], pending=True)
    previous = local_copies(dest, source, read_report(sorted_csv, SORT_FIELDS[:3]) + pending)
    prior_dupes = read_report(dupes_csv, DUPE_FIELDS) + read_report(dupes_pending, DUPE_FIELDS, pending=True)
    by_original = defaultdict(list)
    for row in previous.values():
        by_original[os.path.realpath(row["original_path"])].append(row)
    replacing = args.output_mode == "replace"
    kept = {} if replacing else previous.copy()
    pending_by_entry = local_copies(dest, source, pending)
    added, reused, new_dupes = [], 0, []
    labels_by_folder = {(c["kind"], c["label"].replace("&", "and").replace("/", "-")): key for key, c in cats.items()}
    for r in recs:
        folder = cats[labels[str(r["id"])]]["label"].replace("&", "and").replace("/", "-")
        match = dupe_of.get(r["id"])
        if match:
            folder = REVIEW_FOLDER
        row = keep_copy(dest, by_original[os.path.realpath(r["path"])], r, folder, replacing)
        if row:
            reused += 1
            kept[row["file"]] = row
            folder = row["folder"]
            label = labels_by_folder.get((r["kind"], folder))
            if label:
                labels[str(r["id"])] = label
        else:
            added.append((r, folder))
        if match and folder == REVIEW_FOLDER:
            original = byid[match["keep"]]
            new_dupes.append(dict(zip(DUPE_FIELDS, [r["name"], original["name"], match["type"], r["path"], original["path"]])))

    check_copy_space([r for r, _ in added], dest)
    print(f"Reusing {reused} existing copies. Copying {len(added)} new files.", flush=True)
    print(f"PROGRESS {reused} {len(recs)}", flush=True)
    # The journal contains only new paths. Keep it until both reports are published.
    write_report(sorted_pending, SORT_FIELDS, pending_by_entry.values())
    with open(sorted_pending, "a", newline="", encoding="utf-8") as fc:
        wc = csv.DictWriter(fc, SORT_FIELDS)
        for n, (r, folder) in enumerate(added, reused + 1):
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
            entry = os.path.relpath(target, dest)
            row = dict(zip(SORT_FIELDS, [entry, folder, r["path"], r["sha256"]]))
            wc.writerow(row)
            fc.flush()
            copy(r["path"], target)
            kept[entry] = row
            if n % 25 == 0 or n == len(recs):
                print(f"PROGRESS {n} {len(recs)}", flush=True)
            if n % 500 == 0:
                print(f"  copied {n}/{len(recs)}", flush=True)
    removed = remove_obsolete(dest, previous, kept) if replacing else 0
    review_paths = {r["original_path"] for r in kept.values() if r["folder"] == REVIEW_FOLDER}
    duplicate_rows = {tuple(row.get(k) for k in DUPE_FIELDS): row for row in prior_dupes + new_dupes
                      if row.get("duplicate_path") in review_paths}
    write_report(dupes_csv, DUPE_FIELDS, duplicate_rows.values())
    write_report(sorted_csv, SORT_FIELDS, kept.values())
    for report in (sorted_pending, dupes_pending):
        if os.path.exists(report):
            os.unlink(report)
    save(os.path.join(DATA, "categories.json"), list(cats.values()))
    save(os.path.join(DATA, "labels.json"), labels)
    source_info["sorted"] = dest
    source_info["profile"] = profile
    source_info["copy_summary"] = {"added": len(added), "reused": reused, "removed": removed}
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
