"""Resolution check reads data-file sources (CSV/XLSX), not just prose pages."""

from arbus import aicheck


class _Resp:
    def __init__(self, url, text="", content=None, ctype="", status=200):
        self.url = url
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = {"content-type": ctype}
        self.status_code = status


def test_looks_like_csv():
    assert aicheck._looks_like_csv("a;b;c\n1;2;3\n4;5;6")
    assert aicheck._looks_like_csv("Filmas,Žiūrovai\nDune,12345\nBarbie,9000")
    assert not aicheck._looks_like_csv("just some prose line\nand another sentence")


def test_csv_text_preserves_rows_and_sniffs_semicolon():
    raw = "Filmas;Žiūrovai\nDune;12345\nBarbie;9000"
    out = aicheck._csv_text(raw, 1000)
    assert "Filmas | Žiūrovai" in out
    assert "Dune | 12345" in out
    assert out.count("\n") == 2                    # rows kept as separate lines


def test_render_source_csv_by_content_type():
    r = _Resp("http://x/report.csv", text="a;b\n1;2", ctype="text/csv")
    assert "a | b" in aicheck._render_source(r, r.url, 1000)


def test_render_source_html_is_stripped():
    r = _Resp("http://x", text="<html><h1>Labas</h1> pasaulis</html>", ctype="text/html")
    out = aicheck._render_source(r, r.url, 1000)
    assert "Labas" in out and "pasaulis" in out and "<" not in out


def test_fetch_follows_a_data_file_link(monkeypatch):
    page = '<html><body>Savaitės ataskaita: <a href="/files/week.csv">CSV</a></body></html>'
    csvraw = "Filmas;Žiūrovai\nDune;12345\nOppenheimer;8000"

    def fake_get(url, headers=None, timeout=12, allow_redirects=True):
        if url.endswith(".csv"):
            return _Resp("http://kc.lt/files/week.csv", text=csvraw, ctype="text/csv")
        return _Resp("http://kc.lt/report", text=page, ctype="text/html")

    monkeypatch.setattr(aicheck.requests, "get", fake_get)
    out = aicheck.fetch_source_text("http://kc.lt/report")
    assert "Dune | 12345" in out                    # the linked table was read
    assert "duomenų failas" in out


def test_fetch_csv_directly(monkeypatch):
    csvraw = "Filmas;Žiūrovai\nDune;12345\nBarbie;9000"
    monkeypatch.setattr(aicheck.requests, "get",
                        lambda url, **k: _Resp(url, text=csvraw, ctype="text/csv"))
    out = aicheck.fetch_source_text("http://kc.lt/week.csv")
    assert "Dune | 12345" in out and "Barbie | 9000" in out


def test_source_facts_accepts_a_short_but_real_table(monkeypatch):
    # A wide table can be <200 chars of prose yet be the whole answer.
    csvraw = "Filmas;Žiūrovai\n" + "\n".join(f"F{i};{1000-i}" for i in range(20))
    monkeypatch.setattr(aicheck.requests, "get",
                        lambda url, **k: _Resp(url, text=csvraw, ctype="text/csv"))
    facts = aicheck.source_facts("Šaltinis: http://kc.lt/week.csv")
    assert "F0 | 1000" in facts
