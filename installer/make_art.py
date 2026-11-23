"""Generate the Project Mu installer artwork.

Run once on Windows (needs Pillow and the Yu Mincho / Georgia fonts) and commit
the BMPs: `python installer/make_art.py installer`. CI only uses the BMPs.
"""
from PIL import Image, ImageDraw, ImageFont
import math, sys
from pathlib import Path
OUT = Path(sys.argv[1])
SUMI, KIRI, WASHI, INKAN = (9,10,12), (30,32,36), (226,232,240), (220,38,38)
MINCHO = "C:/Windows/Fonts/yumindb.ttf"
SERIF = "C:/Windows/Fonts/georgia.ttf"

def enso(d, cx, cy, r, w, color):
    # brush-like open circle: varying width arc, gap at the top right
    steps = 400
    for i in range(steps):
        t = i / steps
        a = math.radians(-60 + 330 * t)
        ww = w * (0.35 + 0.65 * math.sin(math.pi * min(1, t * 1.15)))
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        d.ellipse([x - ww/2, y - ww/2, x + ww/2, y + ww/2], fill=color)

def seal(d, x, y, s, glyph_font):
    d.rounded_rectangle([x, y, x + s, y + s], radius=max(2, s // 10), fill=INKAN)
    d.text((x + s/2, y + s/2), "無", font=glyph_font, fill=WASHI, anchor="mm")

def wizard(scale):
    W, H = round(164 * scale), round(314 * scale)
    im = Image.new("RGB", (W, H), SUMI); d = ImageDraw.Draw(im)
    cx, cy = W / 2, H * 0.36
    enso(d, cx, cy, W * 0.36, 7 * scale, KIRI)
    d.text((cx, cy), "無", font=ImageFont.truetype(MINCHO, round(70 * scale)), fill=WASHI, anchor="mm")
    d.text((cx, H * 0.68), "Project Mu", font=ImageFont.truetype(SERIF, round(17 * scale)), fill=WASHI, anchor="mm")
    d.text((cx, H * 0.75), "zero-trace browser", font=ImageFont.truetype(SERIF, round(9.5 * scale)), fill=(140,148,160), anchor="mm")
    s = round(20 * scale)
    seal(d, cx - s/2, H * 0.86, s, ImageFont.truetype(MINCHO, round(14 * scale)))
    return im

def small(scale):
    S = round(55 * scale)
    im = Image.new("RGB", (S, S), (255,255,255)); d = ImageDraw.Draw(im)
    m = round(4 * scale)
    seal(d, m, m, S - 2*m, ImageFont.truetype(MINCHO, round(34 * scale)))
    return im

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
GOTHIC = "C:/Windows/Fonts/YuGothB.ttc"

def icon_frame(size, bg, fg, border=None):
    """One icon size: a rounded seal with 無. Small sizes use a heavier
    gothic glyph and less margin so the character stays readable."""
    ss = 4  # supersample
    S = size * ss
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    m = round(S * (0.04 if size <= 24 else 0.07))
    r = round(S * 0.22)
    d.rounded_rectangle([m, m, S - m - 1, S - m - 1], radius=r, fill=bg,
                        outline=border, width=max(ss, round(S * 0.035)) if border else 0)
    small_px = size <= 32
    font = ImageFont.truetype(GOTHIC if small_px else MINCHO, round(S * (0.74 if small_px else 0.64)))
    d.text((S / 2, S / 2 + S * 0.01), "無", font=font, fill=fg, anchor="mm")
    return im.resize((size, size), Image.LANCZOS)

def write_ico(path, bg, fg, border=None):
    frames = [icon_frame(s, bg, fg, border) for s in ICON_SIZES]
    # BMP-format entries: the classic icon layout every Windows API reads.
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES], bitmap_format="bmp",
                    append_images=frames[:-1])

write_ico(OUT / "mu.ico", INKAN + (255,), WASHI + (255,))
write_ico(OUT / "mu-incognito.ico", SUMI + (255,), WASHI + (255,), border=INKAN + (255,))
(OUT / "mu-newtab" / "icons").mkdir(parents=True, exist_ok=True)
for s in (16, 32, 48, 128):  # tab icon of the Mu New Tab page
    icon_frame(s, INKAN + (255,), WASHI + (255,)).save(OUT / "mu-newtab" / "icons" / f"mu-{s}.png")
print("icons done")

for pct in (100, 150, 200):
    wizard(pct/100).save(OUT / f"wizard-image-{pct}.bmp")
    small(pct/100).save(OUT / f"wizard-small-{pct}.bmp")
    print(pct, "done")
