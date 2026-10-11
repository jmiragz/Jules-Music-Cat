# make_icon.py
# turns lilcat.png into music_cat.ico, the icon for the window and the exe.
# want your own icon? replace lilcat.png with any square pixel art png and run:
#   python make_icon.py
# needs pillow once:  python -m pip install pillow

from pathlib import Path
from PIL import Image

# look for the files next to this script
here = Path(__file__).resolve().parent
source = here / "lilcat.png"
target = here / "music_cat.ico"

# the sizes windows asks for. 20, 40 and 80 are exact multiples of my 20 pixel
# art, so those come out perfectly sharp. the others are still crisp, just not
# every art pixel is exactly the same width
SIZES = [16, 20, 24, 32, 40, 48, 64, 80, 128, 256]


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
        changed = False
        for j in range(0, other, 2):
            a = px[i, j] if horizontal else px[j, i]
            b = px[i - 1, j] if horizontal else px[j, i - 1]
            if sum(abs(p - q) for p, q in zip(a, b)) > 40:
                changed = True
                break
        if changed:
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
    return min(gaps) if gaps else None


# open the picture and keep the see through background
img = Image.open(source).convert("RGBA")

# a big picture is probably a zoomed in screenshot of tiny pixel art
# so find the real pixels and rebuild the small version
if max(img.size) > 64:
    cell_x = find_cell_size(img, True)
    cell_y = find_cell_size(img, False)
    if cell_x and cell_y:
        cols = round(img.width / cell_x)
        rows = round(img.height / cell_y)
        # grab the color from the middle of every art pixel
        small = Image.new("RGBA", (cols, rows))
        for y in range(rows):
            for x in range(cols):
                cx = int((x + 0.5) * img.width / cols)
                cy = int((y + 0.5) * img.height / rows)
                small.putpixel((x, y), img.getpixel((cx, cy)))
        img = small
        print(f"found {cols}x{rows} art pixels in the big picture")

# the icon has to be square
if img.width != img.height:
    raise SystemExit(f"the picture needs to be square, but it is {img.width}x{img.height}")

# nearest keeps every edge sharp. no smoothing, no mush
frames = [img.resize((s, s), Image.NEAREST) for s in SIZES]

# save every size inside one .ico file
frames[-1].save(target, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
print(f"wrote {target.name} with sizes {SIZES}")
