"""
JPYCニュース収集スクリプト(週次自動実行用)

- GNewsを 8〜9日刻みの窓でループし、90件に近い窓は自動的に分割して再取得する
- 既存の raw_articles_full.csv (本文付き・非公開リポジトリ側) の url と突き合わせて重複をスキップする
- 新規記事のみ本文をスクレイピングし、classification_status='pending' として追記する
"""
import os
import sys
import time
import random
import json
from datetime import date, timedelta, timezone
from urllib.parse import urlparse

import pandas as pd
import requests
from newspaper import Article, Config
import trafilatura
from gnews import GNews
from googlenewsdecoder import gnewsdecoder

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FULL_DATA_PATH = os.environ.get(
    "FULL_DATA_PATH", os.path.join("..", "jpyc-news-data", "raw_articles_full.csv")
)

KEYWORD = '"JPYC" OR "ジェイピーワイシー"'
WINDOW_DAYS = 8
LOOKBACK_DAYS = 14  # 前回実行からの取りこぼしに備えて、直近14日分を毎回見直す
JST = timezone(timedelta(hours=9))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_config = Config()
_config.browser_user_agent = _UA


def log(msg):
    print(msg, flush=True)


def fetch_window(win_start, win_end, depth=0):
    gn = GNews(language="ja", country="JP", max_results=100)
    gn.start_date = (win_start.year, win_start.month, win_start.day)
    gn.end_date = (win_end.year, win_end.month, win_end.day)
    try:
        result = gn.get_news(KEYWORD)
    except Exception as e:
        log(f"  ERROR fetching {win_start}~{win_end}: {e}")
        return []
    indent = "  " * depth
    log(f"{indent}{win_start}~{win_end}: {len(result)}件")

    if len(result) >= 90 and win_end > win_start:
        log(f"{indent}  ⚠️ 上限(100件)に近いため分割して再取得します")
        mid = win_start + (win_end - win_start) // 2
        time.sleep(random.uniform(1.0, 2.0))
        first_half = fetch_window(win_start, mid, depth + 1)
        second_half = fetch_window(mid + timedelta(days=1), win_end, depth + 1)
        return first_half + second_half
    return result


def decode_url(google_url):
    try:
        result = gnewsdecoder(google_url, interval=1)
        if result.get("status"):
            decoded = result.get("decoded_url")
            if decoded and "news.google.com" not in decoded:
                return decoded
    except Exception:
        pass
    try:
        resp = requests.get(
            google_url, headers={"User-Agent": _UA}, allow_redirects=True, timeout=10
        )
        if "news.google.com" not in resp.url:
            return resp.url
    except Exception:
        pass
    return google_url


def get_text(url):
    text = ""
    publish_date = None
    try:
        article = Article(url, config=_config)
        article.download()
        article.parse()
        if article.publish_date:
            pd_ = article.publish_date
            pd_ = pd_.replace(tzinfo=JST) if pd_.tzinfo is None else pd_.astimezone(JST)
            publish_date = pd_
        if article.text and len(article.text) > 200:
            text = article.text
    except Exception:
        pass
    if not text:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                extracted = trafilatura.extract(downloaded)
                if extracted and len(extracted) > 200:
                    text = extracted
        except Exception:
            pass
    return text, publish_date


def domain_of(url):
    try:
        netloc = urlparse(str(url)).netloc
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def main():
    log("=" * 50)
    log("記事収集開始")
    log("=" * 50)

    if os.path.exists(FULL_DATA_PATH):
        df = pd.read_csv(FULL_DATA_PATH)
        existing_urls = set(df["url"].dropna().astype(str))
        log(f"既存データ: {len(df)}件読み込みました")
    else:
        df = pd.DataFrame()
        existing_urls = set()
        log("既存データなし。新規作成します。")

    range_end = date.today()
    range_start = range_end - timedelta(days=LOOKBACK_DAYS)
    log(f"収集範囲: {range_start} 〜 {range_end}")

    cur = range_start
    all_news = []
    while cur <= range_end:
        win_end = min(cur + timedelta(days=WINDOW_DAYS), range_end)
        all_news.extend(fetch_window(cur, win_end))
        cur = win_end + timedelta(days=1)
        time.sleep(random.uniform(1.0, 2.0))

    # url重複除去(このバッチ内)
    seen = {}
    for item in all_news:
        seen[item.get("url", "")] = item
    uniq = list(seen.values())
    log(f"取得(バッチ内重複除去後): {len(uniq)}件")

    todo = [item for item in uniq if item.get("url") not in existing_urls]
    log(f"新規候補(既存urlと未突合): {len(todo)}件")

    new_rows = []
    for i, item in enumerate(todo, 1):
        google_url = item.get("url", "")
        title = item.get("title", "")
        real_url = decode_url(google_url)

        if real_url in existing_urls or google_url in existing_urls:
            continue

        text, article_date = get_text(real_url)
        if not text:
            log(f"[{i}/{len(todo)}] 本文取得失敗、スキップ: {title[:40]}")
            existing_urls.add(google_url)
            existing_urls.add(real_url)
            continue

        final_date = article_date if article_date else None
        if final_date is None:
            try:
                from email.utils import parsedate_to_datetime
                gd = parsedate_to_datetime(item.get("published date", ""))
                final_date = gd.astimezone(JST)
            except Exception:
                final_date = None

        new_rows.append({
            "date": final_date.strftime("%Y-%m-%d %H:%M:%S") if final_date else "",
            "title": title,
            "url": real_url,
            "domain": domain_of(real_url),
            "text": text,
            "text_source": "scraped",
            "tags": "",
            "is_commentary": None,
            "entities": "",
            "relevance": None,
            "event_id": None,
            "burst_start_date": None,
            "classification_status": "pending",
        })
        existing_urls.add(google_url)
        existing_urls.add(real_url)
        log(f"[{i}/{len(todo)}] 新規取得: {title[:40]} ({len(text)}文字)")
        time.sleep(random.uniform(1.0, 2.0))

    log(f"\n新規追加: {len(new_rows)}件")

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([df, new_df], ignore_index=True) if len(df) else new_df
        max_id = df["article_id"].max() if "article_id" in df.columns and len(df) else 0
        if pd.isna(max_id):
            max_id = 0
        needs_id = combined["article_id"].isna() if "article_id" in combined.columns else pd.Series([True] * len(combined))
        if "article_id" not in combined.columns:
            combined["article_id"] = None
        next_id = int(max_id) + 1
        for idx in combined.index[combined["article_id"].isna()]:
            combined.at[idx, "article_id"] = next_id
            next_id += 1
        os.makedirs(os.path.dirname(FULL_DATA_PATH), exist_ok=True)
        combined.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig")
        log(f"保存完了: {FULL_DATA_PATH} (合計 {len(combined)}件)")
    else:
        log("新規記事なし。ファイルは変更しません。")

    log("=== 収集完了 ===")


if __name__ == "__main__":
    main()
