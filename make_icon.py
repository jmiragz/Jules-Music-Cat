# make_icon.py
# turns lilcat.png into music_cat.ico, the icon for the window and the exe.
# want your own icon? replace lilcat.png with any square pixel art png and run:
#   python make_icon.py
# needs pillow once:  python -m pip install pillow

import io
import struct
from pathlib import Path
from PIL import Image

# look for the files next to this script
here = Path(__file__).resolve().parent
source = here / "lilcat.png"
target = here / "music_cat.ico"

# every size windows might ask for (title bar, taskbar, alt-tab, explorer).
# my art is 16x16, so 16, 32, 48, 64, 96, 128 and 256 are perfectly sharp.
# a 16 pixel grid is the trick: it divides into the sizes windows uses most
SIZES = [16, 20, 24, 28, 30, 32, 36, 40, 42, 44, 48, 56, 60, 64, 96, 128, 256]


def find_cell_size(im, horizontal):
    # figures out how many screen pixels wide one art pixel is
    # look for the places where the color changes. the smallest gap between
    # two changes is one art pixel
    w, h = im.size
    px = im.load()
    length = w if horizontal else h
    other = h if horizontal else w
    edges = []
    for i in range(1, length):
        changed = 0
        for j in range(other):
            a = px[i, j] if horizontal else px[j, i]
            b = px[i - 1, j] if horizontal else px[j, i - 1]
            if sum(abs(p - q) for p, q in zip(a, b)) > 60:
                changed += 1
        # a real art edge shows up along a good chunk of the picture
        if changed > other // 20:
            edges.append(i)
    # a blurry edge can be a couple of pixels wide, so group those together
    groups = []
    for e in edges:
        if groups and e - groups[-1][-1] <= 2:
            groups[-1].append(e)
        else:
            groups.append([e])
    centers = [sum(g) / len(g) for g in groups]
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    # the most common gap is the art pixel size, ignore odd short ones
    gaps = [g for g in gaps if g > 3]
    if not gaps:
        return None, 0
    cell = min(gaps)
    return cell, centers[0]


def rebuild_art(img):
    # a big picture is probably a zoomed in screenshot of tiny pixel art
    # find the real pixels and rebuild the small version, cropping any border
    cell_x, first_x = find_cell_size(img, True)
    cell_y, first_y = find_cell_size(img, False)
    if not cell_x or not cell_y:
        return img
    # the first edge sits one cell after the art starts (or at the border)
    # so walk back to where the grid really begins
    x0 = first_x - cell_x * int(first_x // cell_x)
    y0 = first_y - cell_y * int(first_y // cell_y)
    cols = int((img.width - x0) // cell_x)
    rows = int((img.height - y0) // cell_y)
    # grab the color from the middle of every art pixel
    small = Image.new("RGBA", (cols, rows))
    for y in range(rows):
        for x in range(cols):
            cx = int(x0 + (x + 0.5) * cell_x)
            cy = int(y0 + (y + 0.5) * cell_y)
            small.putpixel((x, y), img.getpixel((cx, cy)))
    print(f"found {cols}x{rows} art pixels in the big picture")
    return small


def render(art, size):
    # nearest keeps every art pixel a hard-edged block, no smoothing at all.
    # on sizes that are an exact multiple of the art it is perfectly even,
    # on the others a few blocks end up one screen pixel wider, still crisp
    return art.resize((size, size), Image.NEAREST)


def write_ico(frames, path):
    # pillow keeps dropping sizes, so i write the .ico by hand. each size is
    # stored as a png, which every modern windows reads fine
    blobs = []
    for f in frames:
        buf = io.BytesIO()
        f.save(buf, format="PNG")
        blobs.append(buf.getvalue())
    out = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for f, blob in zip(frames, blobs):
        w = 0 if f.width >= 256 else f.width
        h = 0 if f.height >= 256 else f.height
        out += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(out + b"".join(blobs))


img = Image.open(source).convert("RGBA")

# big picture means zoomed in screenshot, so rebuild the tiny art first
if max(img.size) > 64:
    img = rebuild_art(img)

# the icon has to be square
if img.width != img.height:
    raise SystemExit(f"the picture needs to be square, but it is {img.width}x{img.height}")

frames = [render(img, s) for s in SIZES]
write_ico(frames, target)
print(f"wrote {target.name} with sizes {SIZES}")
