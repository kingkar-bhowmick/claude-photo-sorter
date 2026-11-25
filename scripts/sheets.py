"""Contact sheets for the labelers.  python scripts/sheets.py [--sample]

Default: every file, 30 per sheet, each tagged with its id -> work/sheets/sheet_NN.jpg
--sample: 3 sheets of random files -> work/sample/, used to suggest folders that fit the library.
Videos are tagged "VIDEO 12s", PNGs "PNG" (usually screenshots).
"""
import os, random, shutil, sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(__file__))
from common import DATA, THUMBS, WORK, load

font = None
for f in ("/System/Library/Fonts/Helvetica.ttc", "arial.ttf", "DejaVuSans.ttf"):
    try:
        font = ImageFont.truetype(f, 22)
        break
    except OSError:
        pass
font = font or ImageFont.load_default()

FILES_PER_SHEET = 30


def write_sheet(chunk, path):
    C = 230
    im = Image.new("RGB", (6 * C, 5 * C), "white")
    d = ImageDraw.Draw(im)
    for k, r in enumerate(chunk):
        x, y = (k % 6) * C, (k // 6) * C
        with Image.open(os.path.join(THUMBS, f"{r['id']}.jpg")) as thumb:
            im.paste(thumb.resize((C - 8, C - 8)), (x + 4, y + 4))
        tag = str(r["id"])
        if r["kind"] == "video":
            tag += f" VIDEO {r.get('duration', 0):.0f}s"
        elif r["ext"] == ".png":
            tag += " PNG"
        d.rectangle((x + 4, y + 4, x + 14 + 13 * len(tag), y + 32), fill="black")
        d.text((x + 8, y + 6), tag, fill="yellow", font=font)
    im.save(path, quality=82)


if __name__ == "__main__":
    sample = "--sample" in sys.argv
    recs = load(os.path.join(DATA, "inventory.json"))
    out = os.path.join(WORK, "sample" if sample else "sheets")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    if sample:
        recs = random.Random(1).sample(recs, min(90, len(recs)))
    starts = range(0, len(recs), FILES_PER_SHEET)
    for n, start in enumerate(starts):
        write_sheet(recs[start:start + FILES_PER_SHEET], os.path.join(out, f"sheet_{n:02d}.jpg"))
    print(f"{len(starts)} sheets in {os.path.relpath(out)}")
