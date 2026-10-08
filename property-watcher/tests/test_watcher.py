import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watcher  # noqa: E402

RAKUMACHI_HTML = """
<html><body><div class="list">
  <div class="propertyBlock">
    <a href="/syuuekibukken/kyushu/fukuoka/dim1003/3100001/show.html"><img></a>
    <p class="propertyBlock__name"><a href="/syuuekibukken/kyushu/fukuoka/dim1003/3100001/show.html">博多区 一棟アパート</a></p>
    <div>福岡県福岡市博多区博多駅南 価格 1億2,500万円 表面利回り 7.8％</div>
  </div>
  <div class="propertyBlock">
    <p class="propertyBlock__name"><a href="https://www.rakumachi.jp/syuuekibukken/kyushu/fukuoka/dim1001/3100002/show.html">北九州 区分マンション</a></p>
    <div>福岡県北九州市小倉北区 980万円 利回り 12.4%</div>
  </div>
</div></body></html>
"""

KENBIYA_HTML = """
<html><body><ul>
  <li><a href="/pp3/s/fukuoka/fukuoka-shi_chuo-ku/re_ab12cd34/"><h3>中央区 一棟マンション</h3>
      <span>4,800万円</span><span>9.10%</span><span>福岡県福岡市中央区</span></a></li>
  <li><a href="/pp1/s/fukuoka/kitakyushu-shi/re_zz9988/"><h3>区分</h3><span>350万円</span><span>15.0%</span></a></li>
</ul></body></html>
"""


def test_parse_rakumachi():
    items = watcher.parse_listings(RAKUMACHI_HTML, "rakumachi", "https://www.rakumachi.jp/syuuekibukken/area/")
    assert [i.id for i in items] == ["3100001", "3100002"]
    a, b = items
    assert a.title == "博多区 一棟アパート"
    assert a.price_man == 12500
    assert a.yield_pct == 7.8
    assert a.url.startswith("https://www.rakumachi.jp/")
    assert a.location.startswith("福岡県福岡市博多区")
    assert b.price_man == 980 and b.yield_pct == 12.4


def test_parse_kenbiya():
    items = watcher.parse_listings(KENBIYA_HTML, "kenbiya", "https://www.kenbiya.com/pp0/s/fukuoka/")
    assert [i.id for i in items] == ["ab12cd34", "zz9988"]
    assert items[0].title == "中央区 一棟マンション"
    assert items[0].price_man == 4800 and items[0].yield_pct == 9.1


def test_filters():
    item = watcher.Listing("kenbiya", "x", "u", "t", price_man=5000, yield_pct=6.0)
    assert watcher.passes_filters(item, {})
    assert not watcher.passes_filters(item, {"max_price_man": 4000})
    assert not watcher.passes_filters(item, {"min_yield": 8})
    unknown = watcher.Listing("kenbiya", "y", "u", "t")
    assert watcher.passes_filters(unknown, {"max_price_man": 1, "min_yield": 99})


def test_run_first_then_new(tmp_path, monkeypatch, capsys):
    pages = {"r": RAKUMACHI_HTML}
    monkeypatch.setattr(watcher, "fetch", lambda url, session: pages["r"])
    monkeypatch.setattr(watcher, "REQUEST_INTERVAL_SEC", 0)
    for k in ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "LINE_CHANNEL_ACCESS_TOKEN", "SMTP_USER"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text('searches:\n  - {name: t, site: rakumachi, url: "https://www.rakumachi.jp/x/"}\n', encoding="utf-8")
    state = tmp_path / "seen.json"

    assert watcher.run(cfg, state, dry_run=False, notify_first_run=False) == 0
    assert "通知なし" in capsys.readouterr().out

    pages["r"] = RAKUMACHI_HTML.replace("3100002", "3100003")
    assert watcher.run(cfg, state, dry_run=False, notify_first_run=False) == 0
    out = capsys.readouterr().out
    assert "新着 1 件" in out and "3100003" in out and "3100001" not in out.split("新着 1 件")[1]


def test_chunks_split():
    chunks = watcher._chunks("H", ["a" * 60, "b" * 60], 100)
    assert len(chunks) == 2 and all(len(c) <= 100 for c in chunks)
