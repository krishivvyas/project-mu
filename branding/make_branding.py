r"""Generate Project Mu's replacements for Chromium's logo and icon files.

Everything under branding/src/ mirrors a path in the Chromium checkout and is
copied over it by `build.py gen`, so the app icon, window icon, taskbar tiles
and in-browser logos are Mu's in every build (no post-build resource editing).

Run on Windows after changing the art (needs Pillow and the Yu fonts), then
commit the output: `python branding/make_branding.py C:\mu-build\src` (the
Chromium checkout is only read, to recolour its internal-page tab icons).
"""
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "installer"))
import make_art  # noqa: E402  (shared seal drawing)

THEME = HERE / "src" / "chrome" / "app" / "theme"
CHROMIUM = THEME / "chromium"


def logo(size: int) -> Image.Image:
    return make_art.icon_frame(size, make_art.INKAN + (255,), make_art.WASHI + (255,))


def logo_name(scale: int, text_color) -> Image.Image:
    """Seal + "Project Mu" wordmark, the size of Chromium's product_logo_name_22."""
    W, H = 97 * scale, 22 * scale
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    im.alpha_composite(logo(H), (0, 0))
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(make_art.SERIF, 13 * scale)
    d.text((H + 4 * scale, H / 2), "Project Mu", font=font, fill=text_color, anchor="lm")
    return im


# Chromium's internal-page tab icons drawn in Google blue; Mu draws them in
# washi grey (same shapes: only the colour changes, alpha is kept).
BLUE_FAVICONS = ["bookmarks", "downloads", "management", "plugins", "settings"]


def recolor_favicons(chromium_src: Path) -> None:
    for folder in ("default_100_percent", "default_200_percent"):
        out = THEME / folder / "common"
        out.mkdir(parents=True, exist_ok=True)
        for name in BLUE_FAVICONS:
            src = chromium_src / "chrome" / "app" / "theme" / folder / "common" / f"favicon_{name}.png"
            alpha = Image.open(src).convert("RGBA").getchannel("A")
            im = Image.new("RGBA", alpha.size, make_art.WASHI + (255,))
            im.putalpha(alpha)
            im.save(out / f"favicon_{name}.png")


def main() -> None:
    (CHROMIUM / "win" / "tiles").mkdir(parents=True, exist_ok=True)
    # Windows icons inside chrome.exe and chrome.dll (app, window, taskbar).
    make_art.write_ico(CHROMIUM / "win" / "chromium.ico", make_art.INKAN + (255,), make_art.WASHI + (255,))
    shutil.copyfile(CHROMIUM / "win" / "chromium.ico", CHROMIUM / "win" / "app_list.ico")
    make_art.write_ico(CHROMIUM / "win" / "incognito.ico", make_art.SUMI + (255,),
                       make_art.WASHI + (255,), border=make_art.INKAN + (255,))
    # Start menu tiles.
    logo(600).save(CHROMIUM / "win" / "tiles" / "Logo.png")
    logo(176).save(CHROMIUM / "win" / "tiles" / "SmallLogo.png")
    # Product logos used across the UI (About page, profile menu, dialogs).
    for s in (16, 24, 48, 64, 128, 256):
        logo(s).save(CHROMIUM / f"product_logo_{s}.png")
    for scale, folder in ((1, "default_100_percent"), (2, "default_200_percent")):
        out = THEME / folder / "chromium"
        out.mkdir(parents=True, exist_ok=True)
        for s in (16, 32):
            logo(s * scale).save(out / f"product_logo_{s}.png")
        logo_name(scale, (32, 33, 36, 255)).save(out / "product_logo_name_22.png")
        logo_name(scale, (255, 255, 255, 255)).save(out / "product_logo_name_22_white.png")
    # Recolouring needs Chromium's originals: pass the checkout's src folder.
    if len(sys.argv) > 1:
        recolor_favicons(Path(sys.argv[1]))
    else:
        print("no Chromium src given: tab icons for internal pages left as they are")
    print("branding written to", HERE / "src")


if __name__ == "__main__":
    main()
