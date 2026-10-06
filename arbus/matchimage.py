"""Per-game market image: home logo  VS  away logo, uploaded to Supabase Storage.

The picture is built from the two team logos (BetExplorer PNGs), so every game
market shows exactly who plays. Any failure (logo missing, upload refused)
returns None and the caller falls back to the league's default image — a
missing picture never blocks a market.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from . import config

W, H = 1200, 675
BG_TOP, BG_BOTTOM = (16, 22, 38), (34, 52, 92)
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
          "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf")
BUCKET = "market-images"


def _font(size: int):
    from PIL import ImageFont

    for p in _FONTS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _logo(data: bytes, box: int):
    from PIL import Image

    head = data[:200].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        import resvg_py                       # LKL ships vector logos; render crisp

        data = bytes(resvg_py.svg_to_bytes(svg_string=data.decode("utf-8"), width=box * 2))
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    bbox = img.getbbox()                      # trim transparent margins
    if bbox:
        img = img.crop(bbox)
    img.thumbnail((box, box), Image.LANCZOS)
    if img.width < box * 0.6 and img.height < box * 0.6:   # tiny source: upscale
        scale = box * 0.8 / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def compose(home_logo: bytes, away_logo: bytes, caption: str = "") -> bytes:
    """1200x675 PNG: gradient, both logos on white discs, a big VS between."""
    from PIL import Image, ImageDraw

    canvas = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(canvas)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    r, cy = 210, H // 2 - 10
    for cx, data in ((W // 4 + 20, home_logo), (3 * W // 4 - 20, away_logo)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255))
        logo = _logo(data, int(r * 1.35))
        canvas.paste(logo, (cx - logo.width // 2, cy - logo.height // 2), logo)
    vs = _font(110)
    d.text((W // 2, cy), "VS", font=vs, fill=(255, 255, 255), anchor="mm",
           stroke_width=4, stroke_fill=(10, 14, 24))
    if caption:
        d.text((W // 2, H - 45), caption, font=_font(38), fill=(220, 228, 245), anchor="mm")
    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def _project_url() -> str | None:
    m = re.match(r"(https://[^/]+\.supabase\.co)", config.ARBUS_API_URL or "")
    return m.group(1) if m else None


def upload(png: bytes, name: str) -> str | None:
    """Upload to the public market-images bucket; returns the public URL."""
    import requests

    base, key = _project_url(), config.ARBUS_WRITE_KEY
    if not base or not key:
        return None
    path = f"auto/games/{name}.png"
    r = requests.post(f"{base}/storage/v1/object/{BUCKET}/{path}", data=png, timeout=30,
                      headers={"Authorization": f"Bearer {key}", "apikey": key,
                               "Content-Type": "image/png", "x-upsert": "true"})
    if r.status_code >= 300:
        return None
    return f"{base}/storage/v1/object/public/{BUCKET}/{path}"


def game_image(logo_urls: list[str], name: str, caption: str = "", fetch_bytes=None) -> str | None:
    """Build + upload the VS picture; None on any failure."""
    if len(logo_urls) < 2:
        return None
    try:
        if fetch_bytes is None:
            import requests

            def fetch_bytes(u):
                resp = requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                return resp.content
        png = compose(fetch_bytes(logo_urls[0]), fetch_bytes(logo_urls[1]), caption)
        return upload(png, name)
    except Exception:
        return None
