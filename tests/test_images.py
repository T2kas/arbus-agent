"""Offline tests for og:image extraction."""

from arbus.images import extract_og_image


def test_extracts_standard_og_image():
    html = '<head><meta property="og:image" content="https://lrt.lt/a.jpg"></head>'
    assert extract_og_image(html) == "https://lrt.lt/a.jpg"


def test_extracts_reversed_attribute_order():
    html = '<meta content="https://delfi.lt/b.jpg" property="og:image">'
    assert extract_og_image(html) == "https://delfi.lt/b.jpg"


def test_falls_back_to_twitter_image():
    html = '<meta name="twitter:image" content="https://15min.lt/c.jpg">'
    assert extract_og_image(html) == "https://15min.lt/c.jpg"


def test_protocol_relative_url_gets_scheme():
    html = '<meta property="og:image" content="//cdn.lrt.lt/d.jpg">'
    assert extract_og_image(html) == "https://cdn.lrt.lt/d.jpg"


def test_root_relative_url_resolved_against_page():
    html = '<meta property="og:image" content="/img/e.jpg">'
    assert extract_og_image(html, "https://vz.lt/straipsnis/1") == "https://vz.lt/img/e.jpg"


def test_html_entities_unescaped():
    html = '<meta property="og:image" content="https://x.lt/f.jpg?w=1&amp;h=2">'
    assert extract_og_image(html) == "https://x.lt/f.jpg?w=1&h=2"


def test_no_image_returns_none():
    assert extract_og_image("<html><head><title>x</title></head></html>") is None
    assert extract_og_image('<meta property="og:image" content="">') is None


def test_non_http_value_rejected():
    html = '<meta property="og:image" content="data:image/png;base64,AAA">'
    assert extract_og_image(html) is None
