#!/usr/bin/env python3
"""Checks the custom-background shrink: 4K cap, WebP re-encode, and the no-op cases.

Needs Pillow; without it there is nothing to test and the script says so.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("%-56s %s%s" % (name, "OK" if cond else "FAIL", (" - " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def jpeg(width, height, quality=88):
    from PIL import Image
    im = Image.new("RGB", (width, height))
    px = im.load()
    for y in range(0, height, 8):
        for x in range(0, width, 8):
            px[x, y] = ((x * 255) // width, (y * 255) // height, 128)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality)
    return out.getvalue()


def main():
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is not installed - nothing to test (CI installs it before this script).")
        return 0

    big = jpeg(6000, 4000)
    got = app.shrink_background(big, "image/jpeg")
    check("a 6000x4000 JPEG is shrunk", got is not None, "got None")
    if got:
        data, ctype, note = got
        with Image.open(io.BytesIO(data)) as im:
            check("  -> served as WebP", ctype == "image/webp", ctype)
            check("  -> capped at 4K", max(im.size) <= 3840 and im.size[1] <= 2160, str(im.size))
            check("  -> aspect ratio kept", abs(im.size[0] / float(im.size[1]) - 1.5) < 0.01, str(im.size))
            check("  -> smaller than the original", len(data) < len(big), "%d vs %d" % (len(data), len(big)))
        print("     %s" % note)

    small = jpeg(1920, 1280)
    got = app.shrink_background(small, "image/jpeg")
    check("an under-4K JPEG is converted, not resized", got is not None)
    if got:
        with Image.open(io.BytesIO(got[0])) as im:
            check("  -> dimensions unchanged", im.size == (1920, 1280), str(im.size))

    out = io.BytesIO()
    Image.new("RGB", (3840, 2160)).save(out, "WEBP", quality=90)
    check("an already-4K WebP is left alone", app.shrink_background(out.getvalue(), "image/webp") is None)

    im = Image.new("RGB", (4000, 6000))
    exif = im.getexif()
    exif[274] = 6                      # Orientation = rotate 90
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    got = app.shrink_background(buf.getvalue(), "image/jpeg")
    check("EXIF rotation is honoured", got is not None)
    if got:
        with Image.open(io.BytesIO(got[0])) as out_im:
            check("  -> landscape after the tag is applied", out_im.size[0] > out_im.size[1], str(out_im.size))

    check("garbage is refused, not fatal", app.shrink_background(b"not an image at all", "image/png") is None)

    print()
    if FAILURES:
        print("%d check(s) FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all background checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
