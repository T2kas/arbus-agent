"""Offline tests for the pulse parsers — fixtures only, no network."""

from arbus import pulse
from arbus.pulse import Signal

# ── Google Trends daily-trends RSS (ht: namespace) ───────────────────────────

GOOGLE_TRENDS_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trends/trendingsearches/daily" version="2.0">
<channel>
  <title>Daily Search Trends</title>
  <item>
    <title>Zalgiris Rytas</title>
    <ht:approx_traffic>20 000+</ht:approx_traffic>
    <link>https://trends.google.com/trends/x</link>
    <ht:news_item>
      <ht:news_item_title>Zalgiris nugalejo</ht:news_item_title>
      <ht:news_item_url>https://www.lrt.lt/zalgiris</ht:news_item_url>
      <ht:news_item_source>LRT</ht:news_item_source>
    </ht:news_item>
  </item>
  <item>
    <title>Jessica Shy</title>
    <ht:approx_traffic>5 000+</ht:approx_traffic>
    <ht:news_item>
      <ht:news_item_title>Naujas singlas</ht:news_item_title>
      <ht:news_item_url>https://www.15min.lt/shy</ht:news_item_url>
    </ht:news_item>
  </item>
  <item>
    <title>oras rytoj</title>
  </item>
</channel>
</rss>"""


def test_google_trends_parses_term_traffic_and_news_url():
    sigs = pulse._parse_google_trends(GOOGLE_TRENDS_RSS, cap=10)
    assert len(sigs) == 3
    first = sigs[0]
    assert first.title == "Zalgiris Rytas"
    assert first.metric == "20 000+ paieškų"
    assert first.kind == "search"
    assert first.url == "https://www.lrt.lt/zalgiris"
    assert first.source == "Google Trends LT"


def test_google_trends_without_traffic_still_yields_signal():
    sigs = pulse._parse_google_trends(GOOGLE_TRENDS_RSS, cap=10)
    last = sigs[-1]
    assert last.title == "oras rytoj"
    assert last.metric == "trending paieška"
    assert last.url == ""


def test_google_trends_respects_cap():
    assert len(pulse._parse_google_trends(GOOGLE_TRENDS_RSS, cap=1)) == 1


# ── Reddit listing JSON ──────────────────────────────────────────────────────

REDDIT_JSON = {
    "data": {
        "children": [
            {"data": {"stickied": True, "title": "Subreddit rules",
                      "score": 999, "num_comments": 0, "permalink": "/r/lietuva/rules"}},
            {"data": {"title": "Kodel Vilniuje toks brangus busto nuomos?",
                      "score": 312, "num_comments": 87, "permalink": "/r/lietuva/abc"}},
            {"data": {"title": "", "score": 5, "num_comments": 1, "permalink": "/x"}},
            {"data": {"title": "Naujas lietuviskas filmas", "score": 44,
                      "num_comments": 12, "permalink": "/r/lietuva/def"}},
        ]
    }
}


def test_reddit_skips_stickied_and_empty_titles():
    sigs = pulse._parse_reddit(REDDIT_JSON, "lietuva", cap=10)
    titles = [s.title for s in sigs]
    assert "Subreddit rules" not in titles  # stickied mod post dropped
    assert titles == ["Kodel Vilniuje toks brangus busto nuomos?",
                      "Naujas lietuviskas filmas"]  # empty-title row dropped


def test_reddit_metric_and_url():
    sigs = pulse._parse_reddit(REDDIT_JSON, "lietuva", cap=10)
    s = sigs[0]
    assert s.metric == "312 balsų, 87 komentarų (r/lietuva)"
    assert s.url == "https://www.reddit.com/r/lietuva/abc"
    assert s.kind == "discussion"
    assert s.source == "Reddit r/lietuva"


# ── Wikipedia LT top-pageviews JSON ──────────────────────────────────────────

WIKI_JSON = {
    "items": [{
        "year": "2026", "month": "07", "day": "21",
        "articles": [
            {"article": "Pagrindinis_puslapis", "views": 90000, "rank": 1},
            {"article": "Specialus:Paieška", "views": 40000, "rank": 2},
            {"article": "Jonas_Valančiūnas", "views": 12500, "rank": 3},
            {"article": "Eurovizija", "views": 8000, "rank": 4},
        ],
    }]
}


def test_wikipedia_filters_noise_pages():
    sigs = pulse._parse_wikipedia(WIKI_JSON, cap=10)
    titles = [s.title for s in sigs]
    assert titles == ["Jonas Valančiūnas", "Eurovizija"]  # main page + special dropped


def test_wikipedia_metric_and_url():
    sigs = pulse._parse_wikipedia(WIKI_JSON, cap=10)
    s = sigs[0]
    assert s.metric == "12 500 peržiūrų (2026-07-21)"
    assert s.url == "https://lt.wikipedia.org/wiki/Jonas_Valančiūnas"
    assert s.kind == "pageview"


# ── YouTube Trending JSON (keyed source) ─────────────────────────────────────

YOUTUBE_JSON = {
    "items": [
        {"id": "abc123", "snippet": {"title": "Vaidas naujas vlogas",
                                     "channelTitle": "Vaidas"},
         "statistics": {"viewCount": "150000"}},
        {"id": "def456", "snippet": {"title": "", "channelTitle": "X"},
         "statistics": {"viewCount": "10"}},
    ]
}


def test_youtube_parses_views_channel_and_skips_empty():
    sigs = pulse._parse_youtube(YOUTUBE_JSON, cap=10)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.title == "Vaidas naujas vlogas"
    assert s.metric == "150 000 peržiūrų, Vaidas"
    assert s.url == "https://www.youtube.com/watch?v=abc123"
    assert s.kind == "video"


def test_youtube_inert_without_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert pulse._youtube(cap=10) == []


# ── TikTok Creative Center JSON ──────────────────────────────────────────────

TIKTOK_HASHTAGS = {
    "data": {"list": [
        {"hashtag_name": "lietuva", "rank": 1, "video_views": 1200000},
        {"hashtag_name": "vilnius", "rank": 2},
        {"hashtag_name": "", "rank": 3, "video_views": 5},
    ]}
}

TIKTOK_SOUNDS = {
    "data": {"sound_list": [
        {"title": "Vasara", "author": "Some Artist", "rank": 1, "link": "https://t/1"},
        {"title": "", "author": "X", "rank": 2},
    ]}
}


def test_tiktok_hashtags_parse_and_skip_empty():
    sigs = pulse._parse_tiktok_hashtags(TIKTOK_HASHTAGS, cap=10)
    assert [s.title for s in sigs] == ["#lietuva", "#vilnius"]
    assert sigs[0].metric == "1 200 000 vaizdo įrašų peržiūrų (TikTok #1)"
    assert sigs[1].metric == "TikTok trending #2"       # no view count
    assert sigs[0].url == "https://www.tiktok.com/tag/lietuva"
    assert sigs[0].kind == "tiktok"


APPLE_JSON = {
    "feed": {"results": [
        {"name": "Šalis nepaliesta", "artistName": "Jessica Shy",
         "url": "https://music.apple.com/lt/1"},
        {"name": "", "artistName": "X", "url": "https://music.apple.com/lt/2"},
        {"name": "Kita daina", "artistName": "Omerta", "url": ""},
    ]}
}


def test_apple_chart_parses_rank_and_skips_empty():
    sigs = pulse._parse_apple(APPLE_JSON, "Apple Music LT", "chart",
                              "populiariausia daina", cap=10)
    assert [s.title for s in sigs] == ["Šalis nepaliesta — Jessica Shy",
                                       "Kita daina — Omerta"]
    assert sigs[0].metric == "populiariausia daina #1 Lietuvoje"
    assert sigs[0].url == "https://music.apple.com/lt/1"


def test_apple_chart_respects_cap():
    assert len(pulse._parse_apple(APPLE_JSON, "s", "chart", "n", cap=1)) == 1


def test_wiki_urls_walk_back_from_yesterday():
    from datetime import date
    urls = pulse._wiki_urls(date(2026, 7, 25), days_back=3)
    assert len(urls) == 3
    assert urls[0].endswith("/2026/07/24")   # yesterday first
    assert urls[1].endswith("/2026/07/23")
    assert "lt.wikipedia" in urls[0]


def test_tiktok_urls_offer_fallback_paths():
    urls = pulse._tiktok_urls("hashtag", "LT", 7, 6)
    assert len(urls) >= 2
    assert all("country_code=LT" in u and "hashtag" in u for u in urls)


def test_tiktok_sounds_parse_and_skip_empty():
    sigs = pulse._parse_tiktok_sounds(TIKTOK_SOUNDS, cap=10)
    assert len(sigs) == 1
    assert sigs[0].title == "Vasara — Some Artist"
    assert sigs[0].source == "TikTok Sounds LT"


# ── pulse_block rendering ────────────────────────────────────────────────────

def test_pulse_block_groups_by_source():
    signals = [
        Signal("Google Trends LT", "Zalgiris", "20 000+ paieškų", "search", "https://x"),
        Signal("Reddit r/lietuva", "Busto nuoma", "312 balsų, 87 komentarų (r/lietuva)",
               "discussion", "https://y"),
    ]
    block = pulse.pulse_block(signals)
    assert "[Google Trends LT]" in block
    assert "[Reddit r/lietuva]" in block
    assert "Zalgiris — 20 000+ paieškų https://x" in block


def test_pulse_block_empty_is_graceful():
    block = pulse.pulse_block([])
    assert "no live pulse signals" in block


def test_pulse_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(pulse.config, "PULSE_ENABLED", False)
    assert pulse.pulse() == []
