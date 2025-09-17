# -*- coding: utf-8 -*-
"""
きら星ポータル 非公式Atomフィード（邪神ちゃんドロップキック）
- タイトルページから /pt/... の各話リンクを抽出して最新N件をAtom化
- 章番号で昇順（古→新）整列、各エントリ updated は上から+1分ずつ
"""

import os
import re
import time
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dateutil import tz
from dateutil.parser import isoparse
from feedgen.feed import FeedGenerator
from requests.adapters import HTTPAdapter, Retry

# ======= 設定 =======

BASE = "https://kirapo.jp/"

TARGET_TITLE_PAGES = [
    # 既存の単一フィード（後方互換用）
    "https://kirapo.jp/meteor/titles/jyashin",  # 邪神ちゃんドロップキック
]

VIEW_PREFIX_MAP = {
    # 既存の単一フィード（後方互換用）
    "https://kirapo.jp/meteor/titles/jyashin": "/pt/meteor/jyashin/",
}

MAX_ITEMS = 5
DELAY_TITLE_PAGE = 0.3
DELAY_DETAIL_PAGE = 0.3
DELAY_AFTER_BUILD = 0.1

HEADERS = {"User-Agent": "kirapo-jyashin-rss/1.0 (+https://github.com/)"}
TIMEOUT = 20

PUBLIC_DIR = "public"
ATOM_NAME = "atom.xml"
RSS_NAME = "compat.xml"  # 互換用（RSS 2.0）

ITEM_DATE_STORE = Path("data/item_dates.json")

CHANNEL_TITLE = "邪神ちゃんドロップキック"
CHANNEL_SUBTITLE = "邪神ちゃんドロップキック 最新話（非公式Atom） | 出典: https://kirapo.jp/"

# 複数RSS出力用のフィード設定（必要に応じて配列に追加）
# - name: 識別用スラグ（ファイル名に使用）
# - channel_title/subtitle: フィードの表示名
# - atom_name: 出力ファイル名（public/ 配下）
# - title_pages: 作品タイトルページのURLリスト
# - view_prefix_map: 各タイトルページに対応する閲覧パスの接頭辞
FEEDS = [
    {
        "name": "jyashin",
        "channel_title": "邪神ちゃんドロップキック",
        "channel_subtitle": "邪神ちゃんドロップキック 最新話（非公式Atom） | 出典: https://kirapo.jp/",
        "atom_name": "atom-jyashin.xml",
        # Drop Feeds 等のRSSクライアント向け互換出力
        "rss_name": "compat-jyashin.xml",
        "title_pages": [
            "https://kirapo.jp/meteor/titles/jyashin",
        ],
        "view_prefix_map": {
            "https://kirapo.jp/meteor/titles/jyashin": "/pt/meteor/jyashin/",
        },
    },
    # 例: 別作品を追加する場合
    # {
    #     "name": "<slug>",
    #     "channel_title": "<作品名>",
    #     "channel_subtitle": "<説明>",
    #     "atom_name": "atom-<slug>.xml",
    #     "title_pages": [
    #         "https://kirapo.jp/<label>/titles/<slug>",
    #     ],
    #     "view_prefix_map": {
    #         "https://kirapo.jp/<label>/titles/<slug>": "/pt/<label>/<slug>/",
    #     },
    # },
]

# =====================

DATE_JA_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
DATE_SLASH_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")

DETAIL_DATETIME_CACHE: Dict[str, Optional[datetime]] = {}
_CACHE_MISS = object()

DETAIL_DATETIME_META_ATTRS = (
    ("property", "article:modified_time"),
    ("property", "article:published_time"),
    ("property", "og:updated_time"),
    ("property", "og:published_time"),
    ("name", "pubdate"),
    ("name", "lastmod"),
    ("name", "last-modified"),
    ("name", "created"),
    ("name", "updated"),
    ("name", "date"),
    ("itemprop", "dateModified"),
    ("itemprop", "datePublished"),
)


CHAPTER_LINE_RE = re.compile(r"^第\s*\d+\s*話[^\n]*")        # 行頭「第◯◯話 …」
CHAPTER_IN_TEXT_RE = re.compile(r"第\s*\d+\s*話[^\n　]*")    # aテキスト内「第◯◯話 …」
CH_NO_RE = re.compile(r"第\s*(\d+)\s*話")                   # 章番号
NOISE_WORDS = ("最新話を読む", "第1話を読む", "読む", "単行本", "特集", "試し読み", "掲載期間")

TZ_JST = tz.gettz("Asia/Tokyo")


def make_session() -> requests.Session:
    sess = requests.Session()
    retries = Retry(
        total=4, connect=4, read=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_site_date_optional(txt: str) -> Optional[datetime]:
    m = DATE_JA_RE.search(txt)
    if m:
        y, mo, d = map(int, m.groups())
        return normalize_story_datetime(datetime(y, mo, d, 0, 0, 0, tzinfo=TZ_JST))
    m = DATE_SLASH_RE.search(txt)
    if m:
        y, mo, d = map(int, m.groups())
        return normalize_story_datetime(datetime(y, mo, d, 0, 0, 0, tzinfo=TZ_JST))
    return None


def parse_site_date_anywhere(txt: str, default_dt: datetime) -> datetime:
    dt = parse_site_date_optional(txt)
    if dt is not None:
        return dt
    return normalize_story_datetime(default_dt)


def normalize_story_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_JST)
    else:
        dt = dt.astimezone(TZ_JST)
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt.replace(hour=9)
    return dt.replace(microsecond=0)



def ensure_unique_utc(dt: datetime, seen: Dict[datetime, int]) -> datetime:
    base = dt.astimezone(tz.gettz("UTC"))
    count = seen.get(base)
    if count is None:
        seen[base] = 1
        return base
    adjusted = base + timedelta(minutes=count)
    seen[base] = count + 1
    return adjusted

def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        dt = isoparse(cleaned)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_JST)
    else:
        dt = dt.astimezone(TZ_JST)
    return normalize_story_datetime(dt)


def load_item_dates() -> Dict[str, datetime]:
    store: Dict[str, datetime] = {}
    if not ITEM_DATE_STORE.exists():
        return store
    try:
        raw = json.loads(ITEM_DATE_STORE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warn] item date store load failed: {exc}")
        return store
    for link, value in raw.items():
        dt = parse_iso_datetime(value) if isinstance(value, str) else None
        if dt:
            store[link] = dt
    return store

def save_item_dates(store: Dict[str, datetime]) -> None:
    ITEM_DATE_STORE.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v.astimezone(TZ_JST).isoformat() for k, v in store.items()}
    try:
        ITEM_DATE_STORE.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"[warn] item date store write failed: {exc}")

def pick_chapter_lines(txt: str) -> List[str]:
    lines = []
    for line in txt.splitlines():
        if CHAPTER_LINE_RE.match(line):
            lines.append(line.strip())
    return lines


def is_special_title(title: str) -> bool:
    t = title or ""
    keywords = (
        "\u7279\u5225\u7de8",
        "\u7279\u5225",
        "\u30b9\u30da\u30b7\u30e3\u30eb",
        "\u756a\u5916",
        "\u90aa\u30d5\u30a7\u30b9",
    )
    return any(key in t for key in keywords)


def pick_special_title_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.replace("\\n", "\n")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in ("読む", "読んだ", "最新話はこちら"):
            continue
        if any(noise in line for noise in NOISE_WORDS):
            continue
        if is_special_title(line):
            return line
    return None

def normalize_chapter_title(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"(掲載期間[:：].*)$", "", s)
    s = re.sub(r"(最新話を読む|第1話を読む|読む)\s*$", "", s)
    m = re.search(r"(第\s*\d+\s*話\s*[^｜|/／\-\–—\[\(（＜<　]*?)\s*$", s)
    if m:
        s = m.group(1)
    return s.strip()



def extract_detail_datetime(session: requests.Session, link: str, fallback_dt: datetime) -> datetime:
    cached = DETAIL_DATETIME_CACHE.get(link, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached or fallback_dt

    try:
        soup = get_soup(session, link)
        time.sleep(DELAY_DETAIL_PAGE)
    except Exception as exc:
        print(f"[warn] detail fetch failed {link}: {exc}")
        DETAIL_DATETIME_CACHE[link] = None
        return fallback_dt

    dt = None

    for attr_name, attr_value in DETAIL_DATETIME_META_ATTRS:
        meta = soup.find("meta", attrs={attr_name: attr_value})
        if meta and meta.get("content"):
            dt = parse_iso_datetime(meta["content"])
            if dt:
                break

    if not dt:
        for time_tag in soup.find_all("time"):
            candidates = [
                time_tag.get("datetime"),
                time_tag.get("data-time"),
                time_tag.get("data-updated"),
            ]
            for cand in candidates:
                parsed = parse_iso_datetime(cand) if cand else None
                if parsed:
                    dt = parsed
                    break
            if dt:
                break
            text = time_tag.get_text(" ", strip=True)
            parsed_text = parse_site_date_optional(text)
            if parsed_text:
                dt = parsed_text
                break

    if not dt:
        text = soup.get_text("\n", strip=True)
        dt = parse_site_date_optional(text)

    if dt:
        DETAIL_DATETIME_CACHE[link] = dt
        return dt

    DETAIL_DATETIME_CACHE[link] = None
    return fallback_dt



def extract_items_from_title_page(
    session: requests.Session,
    soup: BeautifulSoup,
    title_url: str,
    view_prefix: str,
    stored_dates: Dict[str, datetime],
    now: datetime,
) -> List[Tuple[datetime, str, str, str]]:
    txt = soup.get_text("\n", strip=True)
    base_dt = parse_site_date_anywhere(txt, default_dt=now)
    chapter_lines = pick_chapter_lines(txt)

    anchors = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if view_prefix in href:
            full = urljoin(BASE, href)
            if full not in seen:
                seen.add(full)
                anchors.append((a, full))

    if not anchors:
        a_latest = soup.find("a", string=re.compile("最新話はこちら"))
        if a_latest and a_latest.get("href"):
            full = urljoin(BASE, a_latest["href"])
            anchors.append((a_latest, full))

    if not anchors:
        for a in soup.find_all("a", href=True):
            if CHAPTER_IN_TEXT_RE.search(a.get_text(strip=True) or ""):
                full = urljoin(BASE, a["href"])
                if full not in seen:
                    seen.add(full)
                    anchors.append((a, full))

    if not anchors:
        return []

    items: List[Tuple[datetime, str, str, str]] = []
    seen_titles = set()
    count = min(MAX_ITEMS, len(anchors))
    line_idx = 0
    last_regular_dt: Optional[datetime] = None
    first_item_special = False

    for i in range(count):
        a, link = anchors[i]
        raw_text = (a.get_text(strip=True) or "").strip()

        m = CHAPTER_IN_TEXT_RE.search(raw_text)
        chapter_title = m.group(0).strip() if m else None

        if not chapter_title:
            parent = a.parent
            hops = 0
            while parent is not None and hops < 5 and not chapter_title:
                t = parent.get_text("\n", strip=True)
                if t:
                    special_candidate = pick_special_title_from_text(t)
                    if special_candidate:
                        chapter_title = special_candidate
                        break
                    m2 = CHAPTER_IN_TEXT_RE.search(t)
                    if m2:
                        chapter_title = m2.group(0).strip()
                parent = parent.parent
                hops += 1
        if not chapter_title and line_idx < len(chapter_lines):
            chapter_title = chapter_lines[line_idx]
            line_idx += 1

        if not chapter_title:
            continue

        chapter_title = normalize_chapter_title(chapter_title)
        if not chapter_title:
            continue
        if "第1話" in chapter_title:
            continue
        if chapter_title in seen_titles:
            continue
        seen_titles.add(chapter_title)

        is_regular = bool(CH_NO_RE.search(chapter_title))
        is_special = is_special_title(chapter_title)
        if i == 0:
            first_item_special = is_special

        cached_dt = stored_dates.get(link)

        if cached_dt:
            item_dt = normalize_story_datetime(cached_dt)
        else:
            item_dt = normalize_story_datetime(extract_detail_datetime(session, link, base_dt))
            if item_dt.date() > base_dt.date():
                item_dt = base_dt

            if is_regular:
                if last_regular_dt is not None:
                    expected_dt = last_regular_dt - timedelta(days=14)
                else:
                    expected_dt = base_dt - timedelta(days=14) if first_item_special else base_dt
                if item_dt.date() > expected_dt.date():
                    item_dt = expected_dt
            else:
                if item_dt.date() > base_dt.date():
                    item_dt = base_dt

            item_dt = normalize_story_datetime(item_dt)

        stored_dates[link] = item_dt

        if is_regular:
            last_regular_dt = item_dt

        body_html = f'<p><a href="{link}">{chapter_title}</a></p>'
        items.append((item_dt, chapter_title, link, body_html))

    return items


def write_atom(
    items: List[Tuple[datetime, str, str, str]],
    now: datetime,
    *,
    channel_title: str,
    channel_subtitle: str,
    atom_name: str,
) -> None:
    os.makedirs(PUBLIC_DIR, exist_ok=True)

    fa = FeedGenerator()
    fa.id("https://tatitle.github.io/kirapo-jyashin-rss/")  # 固定ID（フィード共通）
    fa.title(channel_title)
    fa.link(
        href=f"https://tatitle.github.io/kirapo-jyashin-rss/{atom_name}",
        rel="self",
        type="application/atom+xml",
    )
    fa.link(href=BASE, rel="alternate")
    fa.subtitle(channel_subtitle)
    fa.language("ja")
    fa.updated(now.astimezone(tz.gettz("UTC")))

    seen_updated: Dict[datetime, int] = {}
    for dt, item_title, link, body_html in items:
        ent = fa.add_entry()
        ent.id(link)
        ent.title(item_title)
        ent.link(href=link)
        ent.updated(ensure_unique_utc(dt, seen_updated))
        ent.content(body_html + '<br>出典: <a href="https://kirapo.jp/">キラポポータル</a>', type='html')

    fa.atom_file(os.path.join(PUBLIC_DIR, atom_name))


def write_rss(
    items: List[Tuple[datetime, str, str, str]],
    now: datetime,
    *,
    channel_title: str,
    channel_subtitle: str,
    rss_name: str,
) -> None:
    os.makedirs(PUBLIC_DIR, exist_ok=True)

    fr = FeedGenerator()
    fr.id("https://tatitle.github.io/kirapo-jyashin-rss/")  # 固定ID（フィード共通）
    fr.title(channel_title)
    # RSS 2.0 の自己参照リンクは atom:link 拡張として付与される
    fr.link(
        href=f"https://tatitle.github.io/kirapo-jyashin-rss/{rss_name}",
        rel="self",
        type="application/rss+xml",
    )
    fr.link(href=BASE, rel="alternate")
    # RSS 2.0 では channel/description を使用
    fr.description(channel_subtitle)
    fr.language("ja")
    # RSS 2.0 では lastBuildDate を設定
    fr.lastBuildDate(now.astimezone(tz.gettz("UTC")))

    seen_pubdates: Dict[datetime, int] = {}
    for dt, item_title, link, body_html in items:
        ent = fr.add_entry()
        # RSS 2.0 では guid を明示的に permalink=true で設定
        ent.guid(link, permalink=True)
        ent.title(item_title)
        ent.link(href=link)
        ent.pubDate(ensure_unique_utc(dt, seen_pubdates))
        ent.description(body_html + '<br>出典: <a href="https://kirapo.jp/">キラポポータル</a>')

    fr.rss_file(os.path.join(PUBLIC_DIR, rss_name))


def build_feed(items: List[Tuple[datetime, str, str, str]], now: datetime) -> None:
    """後方互換: 既存の単一フィード(atom.xml)を生成し、index.htmlでリダイレクト。

    従来のワークフローが参照している `public/atom.xml` のみを対象。
    複数フィードは main() 後半で別途生成する。
    """
    write_atom(
        items,
        now,
        channel_title=CHANNEL_TITLE,
        channel_subtitle=CHANNEL_SUBTITLE,
        atom_name=ATOM_NAME,
    )
    # 互換用RSSも同時出力
    write_rss(
        items,
        now,
        channel_title=CHANNEL_TITLE,
        channel_subtitle=CHANNEL_SUBTITLE,
        rss_name=RSS_NAME,
    )

    # ルート -> atom.xml に即リダイレクト（既存の挙動を維持）
    abs_atom_url = f"{ATOM_NAME}"
    abs_rss_url = f"{RSS_NAME}"
    index_html = f"""<!doctype html>
<meta charset="utf-8">
<title>{CHANNEL_TITLE}</title>
<link rel="alternate" type="application/atom+xml" title="{CHANNEL_TITLE}" href="{abs_atom_url}">
<link rel="alternate" type="application/rss+xml" title="{CHANNEL_TITLE} (RSS互換)" href="{abs_rss_url}">
<meta http-equiv="refresh" content="0; url={abs_atom_url}">
<p>自動的に <a href="{abs_atom_url}">{ATOM_NAME}</a> へ移動します。RSS互換版は <a href="{abs_rss_url}">{RSS_NAME}</a>。</p>
"""
    with open(os.path.join(PUBLIC_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)


def collect_items_for(
    session: requests.Session,
    title_pages: List[str],
    view_prefix_map: dict,
    stored_dates: Dict[str, datetime],
    now: datetime,
) -> List[Tuple[datetime, str, str, str]]:
    """指定のタイトルページ群からアイテムを収集し、章番号で昇順に整列。"""
    items_all: List[Tuple[datetime, str, str, str]] = []
    for title_url in title_pages:
        try:
            soup = get_soup(session, title_url)
            view_prefix = view_prefix_map.get(title_url)
            if not view_prefix:
                m = re.search(r"https://kirapo\.jp/([^/]+)/titles/([^/]+)", title_url)
                if m:
                    label, slug = m.group(1), m.group(2)
                    view_prefix = f"/pt/{label}/{slug}/"
                else:
                    print(f"[warn] VIEW_PREFIX not found for {title_url}")
                    continue
            items = extract_items_from_title_page(session, soup, title_url, view_prefix, stored_dates, now)
            items_all.extend(items)
            time.sleep(DELAY_TITLE_PAGE)
        except Exception as e:
            print(f"[warn] skip {title_url}: {e}")

    def _ch_no_or_date(item):
        dt, title, link, body = item
        m = CH_NO_RE.search(title)
        if m:
            return (int(m.group(1)), dt)
        return (10**9, dt)

    items_all.sort(key=_ch_no_or_date)
    return items_all


def main():
    now = datetime.now(tz=TZ_JST)
    session = make_session()
    stored_dates = load_item_dates()


    all_items: List[Tuple[datetime, str, str, str]] = []

    for title_url in TARGET_TITLE_PAGES:
        try:
            soup = get_soup(session, title_url)
            view_prefix = VIEW_PREFIX_MAP.get(title_url)
            if not view_prefix:
                m = re.search(r"https://kirapo\.jp/([^/]+)/titles/([^/]+)", title_url)
                if m:
                    label, slug = m.group(1), m.group(2)
                    view_prefix = f"/pt/{label}/{slug}/"
                else:
                    print(f"[warn] VIEW_PREFIX not found for {title_url}")
                    continue

            items = extract_items_from_title_page(session, soup, title_url, view_prefix, stored_dates, now)
            all_items.extend(items)
            time.sleep(DELAY_TITLE_PAGE)
        except Exception as e:
            print(f"[warn] skip {title_url}: {e}")

    # ---- 並び順：章番号で昇順（古→新） ----
    def _ch_no_or_date(item):
        dt, title, link, body = item
        m = CH_NO_RE.search(title)
        if m:
            return (int(m.group(1)), dt)
        return (10**9, dt)  # 番号なしは最後

    all_items.sort(key=_ch_no_or_date)

    build_feed(all_items, now)
    time.sleep(DELAY_AFTER_BUILD)
    print(f"OK: {len(all_items)} item(s) -> {os.path.join(PUBLIC_DIR, ATOM_NAME)} (+ index.html)")

    # ---- 複数RSS（個別フィード）を生成 ----
    generated = []
    for feed in FEEDS:
        items = collect_items_for(
            session=session,
            title_pages=feed.get("title_pages", []),
            view_prefix_map=feed.get("view_prefix_map", {}),
            stored_dates=stored_dates,
            now=now,
        )
        if not items:
            print(f"[warn] no items for feed: {feed.get('name')}")
            continue

        atom_name = feed.get("atom_name") or f"atom-{feed.get('name','feed')}.xml"
        write_atom(
            items,
            now,
            channel_title=feed.get("channel_title", "Kirapo 非公式フィード"),
            channel_subtitle=feed.get("channel_subtitle", "きら星ポータル 非公式Atom | 出典: https://kirapo.jp/"),
            atom_name=atom_name,
        )
        # 任意でRSS互換ファイルも出力（キー: rss_name）
        rss_name = feed.get("rss_name")
        if rss_name:
            write_rss(
                items,
                now,
                channel_title=feed.get("channel_title", "Kirapo 非公式フィード"),
                channel_subtitle=feed.get("channel_subtitle", "きら星ポータル 非公式Atom | 出典: https://kirapo.jp/"),
                rss_name=rss_name,
            )

        generated.append((feed.get("channel_title", feed.get("name", "feed")), atom_name))

    # 一覧ページ（feeds.html）を生成（任意参照用）
    if generated:
        lines = [
            "<!doctype html>",
            "<meta charset=\"utf-8\">",
            "<title>Kirapo 非公式フィード一覧</title>",
            "<h1>Kirapo 非公式フィード一覧</h1>",
            "<ul>",
        ]
        for title, fname in generated:
            lines.append(f"  <li><a href=\"./{fname}\">{title}</a> ({fname})</li>")
        lines.append("</ul>")
        with open(os.path.join(PUBLIC_DIR, "feeds.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"OK: generated {len(generated)} feed(s) -> feeds.html list")


    save_item_dates(stored_dates)
if __name__ == "__main__":
    main()
