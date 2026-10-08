"""楽待・健美家の検索結果を巡回し、新着物件を通知するツール。

使い方:
    python watcher.py                 # 巡回して新着があれば通知
    python watcher.py --dry-run       # 通知せず標準出力に表示（状態も保存しない）
    python watcher.py --notify-first-run  # 初回でも既存物件を通知する

通知先は環境変数で設定します（設定されたものすべてに送信）:
    DISCORD_WEBHOOK_URL
    SLACK_WEBHOOK_URL
    LINE_CHANNEL_ACCESS_TOKEN + LINE_TO        (LINE Messaging API の push)
    SMTP_USER, SMTP_PASSWORD (, MAIL_TO, MAIL_FROM, SMTP_HOST, SMTP_PORT)
        Gmail なら SMTP_USER=Gmailアドレス, SMTP_PASSWORD=アプリパスワード の2つだけでOK。
        MAIL_TO 省略時は自分宛て、SMTP_HOST 省略時は smtp.gmail.com:587。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup, Tag

BASE_DIR = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
REQUEST_INTERVAL_SEC = 3
STATE_RETENTION_DAYS = 365

# 物件詳細ページのURLから物件IDを取り出す正規表現
DETAIL_PATTERNS = {
    # 例: https://www.rakumachi.jp/syuuekibukken/kyushu/fukuoka/dim1003/3123456/show.html
    "rakumachi": re.compile(r"rakumachi\.jp/syuuekibukken/.+?/(\d+)/show\.html"),
    # 例: https://www.kenbiya.com/pp3/s/fukuoka/fukuoka-shi_chuo-ku/re_12345abcd/
    "kenbiya": re.compile(r"kenbiya\.com/.*?/re_([0-9a-zA-Z]+)/?"),
}
SITE_LABELS = {"rakumachi": "楽待", "kenbiya": "健美家"}


@dataclass
class Listing:
    site: str
    id: str
    url: str
    title: str
    price: str | None = None
    price_man: float | None = None
    yield_pct: float | None = None
    location: str | None = None
    search_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.site}:{self.id}"


# ---------------------------------------------------------------- 解析

PRICE_RE = re.compile(r"(?:(\d+(?:,\d{3})*)\s*億)?\s*(\d+(?:,\d{3})*(?:\.\d+)?)?\s*万円")
YIELD_RE = re.compile(r"(?:表面)?利回り?[^\d]{0,6}(\d{1,2}(?:\.\d+)?)\s*[%％]")
YIELD_FALLBACK_RE = re.compile(r"(\d{1,2}\.\d{1,2})\s*[%％]")
LOCATION_RE = re.compile(r"((?:北海道|東京都|京都府|大阪府|[^\s　]{2,3}県)[^\s　/|｜]{1,20}?[市区町村郡][^\s　/|｜]{0,15})")


def parse_price(text: str) -> tuple[str | None, float | None]:
    for m in PRICE_RE.finditer(text):
        oku, man = m.group(1), m.group(2)
        if not oku and not man:
            continue
        value = 0.0
        if oku:
            value += float(oku.replace(",", "")) * 10000
        if man:
            value += float(man.replace(",", ""))
        return m.group(0).strip(), value
    return None, None


def parse_yield(text: str) -> float | None:
    m = YIELD_RE.search(text) or YIELD_FALLBACK_RE.search(text)
    return float(m.group(1)) if m else None


def _ids_in(node: Tag, site: str, base_url: str) -> set[str]:
    ids = set()
    for a in node.find_all("a", href=True):
        m = DETAIL_PATTERNS[site].search(urljoin(base_url, a["href"]))
        if m:
            ids.add(m.group(1))
    return ids


def _card_for(anchor: Tag, site: str, base_url: str) -> Tag:
    """リンクを含む「物件カード」要素を推定する。

    同じ物件へのリンクしか含まない範囲で、できるだけ上位の祖先要素を選ぶ。
    サイトのHTML構造に依存しないため、多少のデザイン変更には耐えられる。
    """
    card = anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in ("body", "html"):
            break
        if len(_ids_in(parent, site, base_url)) > 1:
            break
        card = parent
    return card


def _title_for(card: Tag, anchors: list[Tag]) -> str:
    for sel in ("h1", "h2", "h3", "h4", "[class*=title]", "[class*=name]"):
        el = card.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    texts = [a.get_text(" ", strip=True) for a in anchors]
    texts = [t for t in texts if t and t not in ("詳細", "詳細を見る", "もっと見る")]
    return max(texts, key=len) if texts else "(タイトル不明)"


def parse_listings(html: str, site: str, base_url: str, search_name: str = "") -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    pattern = DETAIL_PATTERNS[site]

    anchors_by_id: dict[str, list[Tag]] = {}
    url_by_id: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        m = pattern.search(url)
        if not m:
            continue
        pid = m.group(1)
        anchors_by_id.setdefault(pid, []).append(a)
        url_by_id.setdefault(pid, url.split("#")[0])

    listings = []
    for pid, anchors in anchors_by_id.items():
        card = _card_for(anchors[0], site, base_url)
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        price, price_man = parse_price(text)
        loc = LOCATION_RE.search(text)
        listings.append(
            Listing(
                site=site,
                id=pid,
                url=url_by_id[pid],
                title=_title_for(card, anchors)[:80],
                price=price,
                price_man=price_man,
                yield_pct=parse_yield(text),
                location=loc.group(1) if loc else None,
                search_name=search_name,
            )
        )
    return listings


# ---------------------------------------------------------------- 取得

def fetch(url: str, session: requests.Session, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            res = session.get(url, timeout=30)
            res.raise_for_status()
            res.encoding = res.apparent_encoding or res.encoding
            return res.text
        except requests.RequestException as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"取得失敗: {url}: {last_err}")


# ---------------------------------------------------------------- 状態

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"seen": {}}


def save_state(path: Path, state: dict) -> None:
    cutoff = (datetime.now(JST) - timedelta(days=STATE_RETENTION_DAYS)).date().isoformat()
    state["seen"] = {k: v for k, v in sorted(state["seen"].items()) if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 絞り込み

def passes_filters(item: Listing, filters: dict) -> bool:
    max_price = filters.get("max_price_man")
    min_yield = filters.get("min_yield")
    if max_price is not None and item.price_man is not None and item.price_man > max_price:
        return False
    if min_yield is not None and item.yield_pct is not None and item.yield_pct < min_yield:
        return False
    return True


# ---------------------------------------------------------------- 通知

def format_item(item: Listing) -> str:
    parts = [f"【{SITE_LABELS[item.site]}】{item.title}"]
    details = []
    if item.price:
        details.append(item.price)
    if item.yield_pct is not None:
        details.append(f"利回り{item.yield_pct}%")
    if item.location:
        details.append(item.location)
    if details:
        parts.append(" / ".join(details))
    parts.append(item.url)
    return "\n".join(parts)


def build_messages(items: list[Listing], max_items: int) -> tuple[str, list[str]]:
    header = f"新着収益物件 {len(items)}件（{datetime.now(JST):%m/%d %H:%M}）"
    bodies = [format_item(i) for i in items[:max_items]]
    if len(items) > max_items:
        bodies.append(f"…ほか {len(items) - max_items} 件")
    return header, bodies


def _chunks(header: str, bodies: list[str], limit: int) -> list[str]:
    """文字数上限ごとにメッセージを分割する。"""
    out, cur = [], header
    for b in bodies:
        if len(cur) + len(b) + 2 > limit:
            out.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    out.append(cur)
    return out


def notify_discord(url: str, header: str, bodies: list[str]) -> None:
    for chunk in _chunks(header, bodies, 1900):
        requests.post(url, json={"content": chunk}, timeout=30).raise_for_status()
        time.sleep(1)


def notify_slack(url: str, header: str, bodies: list[str]) -> None:
    for chunk in _chunks(header, bodies, 3500):
        requests.post(url, json={"text": chunk}, timeout=30).raise_for_status()


def notify_line(token: str, to: str, header: str, bodies: list[str]) -> None:
    chunks = _chunks(header, bodies, 4800)
    # 1リクエストあたり最大5メッセージ
    for i in range(0, len(chunks), 5):
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"to": to, "messages": [{"type": "text", "text": c} for c in chunks[i : i + 5]]},
            timeout=30,
        ).raise_for_status()


def notify_email(header: str, bodies: list[str]) -> None:
    msg = MIMEText("\n\n".join(bodies), "plain", "utf-8")
    msg["Subject"] = header
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("MAIL_TO") or os.environ["SMTP_USER"]
    port = int(os.environ.get("SMTP_PORT") or "587")
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"].replace(" ", ""))
        server.send_message(msg)


def notify(items: list[Listing], max_items: int) -> int:
    header, bodies = build_messages(items, max_items)
    env = os.environ
    sent = 0
    channels = [
        ("Discord", env.get("DISCORD_WEBHOOK_URL"), lambda: notify_discord(env["DISCORD_WEBHOOK_URL"], header, bodies)),
        ("Slack", env.get("SLACK_WEBHOOK_URL"), lambda: notify_slack(env["SLACK_WEBHOOK_URL"], header, bodies)),
        (
            "LINE",
            env.get("LINE_CHANNEL_ACCESS_TOKEN") and env.get("LINE_TO"),
            lambda: notify_line(env["LINE_CHANNEL_ACCESS_TOKEN"], env["LINE_TO"], header, bodies),
        ),
        ("メール", env.get("SMTP_USER") and env.get("SMTP_PASSWORD"), lambda: notify_email(header, bodies)),
    ]
    for name, enabled, send in channels:
        if not enabled:
            continue
        try:
            send()
            sent += 1
            print(f"{name} に通知しました")
        except Exception as e:  # 1つの通知先が失敗しても他は送る
            print(f"{name} への通知に失敗: {e}", file=sys.stderr)
    if not any(enabled for _, enabled, _ in channels):
        print("通知先が設定されていません。以下を標準出力に表示します。\n")
        print(header + "\n\n" + "\n\n".join(bodies))
        sent = 1
    return sent


# ---------------------------------------------------------------- メイン

def run(config_path: Path, state_path: Path, dry_run: bool, notify_first_run: bool) -> int:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    state = load_state(state_path)
    first_run = not state["seen"]
    today = datetime.now(JST).date().isoformat()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})

    found: dict[str, Listing] = {}
    errors = 0
    for i, search in enumerate(config.get("searches", [])):
        site = search["site"]
        if site not in DETAIL_PATTERNS:
            print(f"未対応のサイト: {site}", file=sys.stderr)
            errors += 1
            continue
        if i:
            time.sleep(REQUEST_INTERVAL_SEC)
        try:
            html = fetch(search["url"], session)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            errors += 1
            continue
        listings = parse_listings(html, site, search["url"], search.get("name", ""))
        print(f"{search.get('name', search['url'])}: {len(listings)} 件取得")
        if not listings:
            print("  ※ 物件が0件でした。URLかサイトの構造が変わっていないか確認してください", file=sys.stderr)
        for item in listings:
            found.setdefault(item.key, item)

    new_items = [it for k, it in found.items() if k not in state["seen"]]
    filters = config.get("filters") or {}
    to_notify = [it for it in new_items if passes_filters(it, filters)]
    print(f"新着 {len(new_items)} 件（条件一致 {len(to_notify)} 件）")

    if dry_run:
        for it in to_notify:
            print("\n" + format_item(it))
        return 1 if errors and not found else 0

    if to_notify and (not first_run or notify_first_run):
        if not notify(to_notify, int(config.get("max_items_per_notify", 30))):
            # 全通知先で失敗した場合は既読にせず次回再送する
            return 1
    elif first_run and new_items:
        print("初回実行のため、現在の掲載物件を既読として登録しました（通知なし）")

    for k in found:
        state["seen"].setdefault(k, today)
    state["last_run"] = datetime.now(JST).isoformat(timespec="seconds")
    save_state(state_path, state)
    return 1 if errors and not found else 0


def main() -> None:
    p = argparse.ArgumentParser(description="楽待・健美家の新着物件通知")
    p.add_argument("--config", type=Path, default=BASE_DIR / "config.yaml")
    p.add_argument("--state", type=Path, default=BASE_DIR / "state" / "seen.json")
    p.add_argument("--dry-run", action="store_true", help="通知も状態保存もしない")
    p.add_argument("--notify-first-run", action="store_true", help="初回実行でも通知する")
    args = p.parse_args()
    sys.exit(run(args.config, args.state, args.dry_run, args.notify_first_run))


if __name__ == "__main__":
    main()
