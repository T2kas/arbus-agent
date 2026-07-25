"""Stage 6b — attach an illustrating image to each market.

The app shows a picture next to every market, so each candidate needs one. The
image is taken from the market's OWN source article via its Open Graph
(`og:image`) tag: that picture is by construction about the exact subject —
the politician, the team, the company — with no generation cost, no API key and
no prompt engineering. When a source exposes no usable image the market simply
carries none; a missing picture is never a reason to lose a market.

RIGHTS: og:image URLs point at press photos owned by the outlet. They are fine
for internal review, but before showing them to users, clear the rights with
the outlet or swap in your own artwork. `image_source` records where each image
came from so that audit is possible later.
"""

from __future__ import annotations

import logging
import re
from html import unescape

import requests

from . import config

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ArbusMarketAgent/1.0)"

# <meta property="og:image" content="..."> in either attribute order, plus the
# Twitter equivalent some outlets use instead.
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)\s*=\s*[\"'](?:og:image(?::url)?|twitter:image)[\"'][^>]*"
    r"content\s*=\s*[\"']([^\"']+)[\"']"
    r"|<meta[^>]+content\s*=\s*[\"']([^\"']+)[\"'][^>]*"
    r"(?:property|name)\s*=\s*[\"'](?:og:image(?::url)?|twitter:image)[\"']",
    re.IGNORECASE,
)


def extract_og_image(html: str, base_url: str = "") -> str | None:
    """Return the first Open Graph / Twitter image URL found in `html`."""
    m = _META_RE.search(html)
    if not m:
        return None
    url = unescape((m.group(1) or m.group(2) or "").strip())
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") and base_url:
        root = re.match(r"(https?://[^/]+)", base_url)
        return root.group(1) + url if root else None
    return url if url.startswith("http") else None


def image_for_sources(sources: list[str]) -> tuple[str, str]:
    """First usable (image_url, source_page) across a candidate's sources."""
    for url in sources[: config.IMAGE_MAX_SOURCES]:
        try:
            resp = requests.get(url, headers={"User-Agent": UA},
                                timeout=config.IMAGE_TIMEOUT)
            resp.raise_for_status()
            # og:image lives in <head>; reading the whole page is wasteful.
            img = extract_og_image(resp.text[:60000], url)
            if img:
                return img, url
        except Exception as exc:  # dead link, paywall, timeout, no meta tag
            log.debug("image lookup failed for %s: %s", url, exc)
    return "", ""


def attach_images(candidates: list) -> int:
    """Set .image_url / .image_source on each candidate. Returns how many got one."""
    if not config.IMAGES_ENABLED:
        return 0
    found = 0
    for cand in candidates:
        img, src = image_for_sources(cand.sources)
        cand.image_url, cand.image_source = img, src
        found += bool(img)
    return found
