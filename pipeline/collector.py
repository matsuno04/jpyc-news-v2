"""
JPYCニュース収集スクリプト(毎日自動実行用。2026-10-01までは週次)

- GNewsを 8〜9日刻みの窓でループし、90件に近い窓は自動的に分割して再取得する
- 検索期間は、月曜(JST)は直近14日、それ以外の日は直近3日(環境変数 LOOKBACK_DAYS で上書き可)
- GoogleニュースのリンクとURL変換結果の対応表(url_map.csv)を非公開リポジトリに保存し、
  変換済みのリンクは再変換しない。元記事URLで既存の raw_articles_full.csv と突き合わせて重複をスキップする
- 本文取得に失敗した記事は fetch_failures.csv に記録し、3回失敗したら以後は再試行しない
- 新規記事のみ本文をスクレイピングし、classification_status='pending'・collected_at(収集日時)付きで追記する
"""
import os
import sys
import time
import random
import json
from datetime import date, datetime, timedelta, timezone
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
DATA_DIR = os.path.dirname(FULL_DATA_PATH)
# GoogleニュースのリンクとURL変換結果の対応表。変換は1件ごとに1秒以上かかり外部ライブラリ頼みで
# 壊れやすいので、一度変換できたリンクは再変換しない(毎日実行に合わせて導入)
URL_MAP_PATH = os.path.join(DATA_DIR, "url_map.csv")
# 本文取得・URL変換に失敗した記事の記録(YouTube・X・有料記事などは毎回失敗するため、回数で打ち切る)。
# キーは元記事URL。URL変換の失敗はGoogleのリンクをキーにする
FAILURES_PATH = os.path.join(DATA_DIR, "fetch_failures.csv")

KEYWORD = '"JPYC" OR "ジェイピーワイシー"'
WINDOW_DAYS = 8
JST = timezone(timedelta(hours=9))

# 検索期間。毎日の実行は直近3日、月曜は取りこぼしの見直しを兼ねて従来通り直近14日。
# 障害で取りこぼした期間を取り戻すときは環境変数 LOOKBACK_DAYS で広げる(空欄なら自動)
DAILY_LOOKBACK_DAYS = 3
WEEKLY_LOOKBACK_DAYS = 14

# 本文取得をこの回数失敗した記事は、以後再試行しない
MAX_FETCH_FAILURES = 3

# 故障検知のしきい値。次のどちらかなら仕組み側の故障とみなして異常終了する(GitHubの失敗通知メールで気づく)。
# - URL変換をこの件数以上試して1件も成功せず、変換済みリンクの再変換(カナリア)も失敗した
# - 初めて見つかった新規の記事(URL変換に成功し、過去に失敗したことのない記事)がこの件数以上あるのに、
#   本文を1件も取得できなかった
# 新着0件の日や、毎回失敗する記事(YouTube等)の再試行だけの日は正常。2026-09-21・28の週次実行は googlenewsdecoder 0.2.1 の戻り値の形式変更で
# URL変換が全件失敗し、62件すべてを捨てたまま「成功」で終わっていた。
ALL_FAILED_ALERT_MIN = 3

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_config = Config()
_config.browser_user_agent = _UA

# 検索(GNews)自体が例外で失敗した窓の数。全窓が失敗した場合は「候補0件」ではなく故障として扱う
SEARCH_ERRORS = []


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
        SEARCH_ERRORS.append(f"{win_start}~{win_end}: {e}")
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
        # 成功フラグのキーは googlenewsdecoder 0.1.x が "status"、0.2.x が "success"。
        # 0.2.1 への更新で "status" が無くなり、変換が全件失敗扱いになっていた(2026-09-21〜)
        if result.get("status") or result.get("success"):
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


def lookback_days(today_jst):
    override = os.environ.get("LOOKBACK_DAYS", "").strip()
    if override:
        return int(override), "環境変数LOOKBACK_DAYSで指定"
    if today_jst.weekday() == 0:
        return WEEKLY_LOOKBACK_DAYS, "月曜(週1回の見直し)"
    return DAILY_LOOKBACK_DAYS, "毎日"


def load_url_map():
    if not os.path.exists(URL_MAP_PATH):
        return {}
    m = pd.read_csv(URL_MAP_PATH, dtype=str)
    return {r.google_url: {"real_url": r.real_url, "decoded_at": r.decoded_at} for r in m.itertuples()}


def save_url_map(url_map):
    rows = [{"google_url": g, "real_url": v["real_url"], "decoded_at": v["decoded_at"]} for g, v in url_map.items()]
    pd.DataFrame(rows, columns=["google_url", "real_url", "decoded_at"]).to_csv(
        URL_MAP_PATH, index=False, encoding="utf-8-sig"
    )


def load_failures():
    if not os.path.exists(FAILURES_PATH):
        return {}
    f = pd.read_csv(FAILURES_PATH, dtype=str)
    return {
        r.real_url: {
            "title": r.title,
            "fail_count": int(r.fail_count),
            "first_failed_at": r.first_failed_at,
            "last_failed_at": r.last_failed_at,
        }
        for r in f.itertuples()
    }


def save_failures(failures):
    cols = ["real_url", "title", "fail_count", "first_failed_at", "last_failed_at"]
    rows = [{"real_url": u, **v} for u, v in failures.items()]
    pd.DataFrame(rows, columns=cols).to_csv(FAILURES_PATH, index=False, encoding="utf-8-sig")


def main():
    now_jst = datetime.now(JST)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
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

    url_map = load_url_map()
    failures = load_failures()
    log(f"URL変換の対応表: {len(url_map)}件 / 本文取得の失敗記録: {len(failures)}件")

    days, reason = lookback_days(now_jst.date())
    range_end = now_jst.date()
    range_start = range_end - timedelta(days=days)
    log(f"収集範囲: {range_start} 〜 {range_end}(直近{days}日、{reason})")

    cur = range_start
    all_news = []
    n_windows = 0
    while cur <= range_end:
        win_end = min(cur + timedelta(days=WINDOW_DAYS), range_end)
        all_news.extend(fetch_window(cur, win_end))
        n_windows += 1
        cur = win_end + timedelta(days=1)
        time.sleep(random.uniform(1.0, 2.0))

    # url重複除去(このバッチ内)
    seen = {}
    for item in all_news:
        seen[item.get("url", "")] = item
    uniq = list(seen.values())
    log(f"取得(バッチ内重複除去後): {len(uniq)}件")

    new_rows = []
    stats = {"map_hit": 0, "decoded": 0, "decode_failed": 0, "known": 0, "gave_up": 0, "new_candidates": 0,
             "body_failed": 0, "fresh_candidates": 0, "fresh_saved": 0}
    seen_real = set()
    decode_failed_links = []
    map_hit_links = []
    for i, item in enumerate(uniq, 1):
        google_url = item.get("url", "")
        title = item.get("title", "")

        if google_url in url_map:
            real_url = url_map[google_url]["real_url"]
            stats["map_hit"] += 1
            map_hit_links.append((google_url, real_url))
        else:
            gave_up = failures.get(google_url)
            if gave_up and gave_up["fail_count"] >= MAX_FETCH_FAILURES:
                stats["gave_up"] += 1
                continue
            real_url = decode_url(google_url)
            if "news.google.com" in real_url:
                stats["decode_failed"] += 1
                decode_failed_links.append((google_url, title))
            else:
                url_map[google_url] = {"real_url": real_url, "decoded_at": now_str}
                stats["decoded"] += 1

        if real_url in existing_urls or real_url in seen_real:
            stats["known"] += 1
            continue
        seen_real.add(real_url)

        failure = failures.get(real_url)
        if failure and failure["fail_count"] >= MAX_FETCH_FAILURES:
            stats["gave_up"] += 1
            continue

        stats["new_candidates"] += 1
        decode_ok = "news.google.com" not in real_url
        # 本文取得の故障検知に使うのは、URL変換に成功し、過去に失敗したことのない記事だけ
        # (毎回失敗する記事の再試行は、仕組みが壊れた証拠にならないため)
        fresh = decode_ok and not failure
        if fresh:
            stats["fresh_candidates"] += 1
        text, article_date = get_text(real_url) if decode_ok else ("", None)
        if not text:
            stats["body_failed"] += 1
            if decode_ok:
                # URL変換に失敗した記事はここでは記録しない(実行の最後に、変換の故障でないと分かった場合だけ記録する)
                f = failures.setdefault(real_url, {"title": title, "fail_count": 0, "first_failed_at": now_str, "last_failed_at": now_str})
                f["fail_count"] += 1
                f["last_failed_at"] = now_str
                log(f"[{i}/{len(uniq)}] 本文取得失敗({f['fail_count']}回目)、スキップ: {title[:40]}")
            else:
                log(f"[{i}/{len(uniq)}] GoogleニュースURLの変換に失敗、スキップ: {title[:40]}")
            continue

        failures.pop(real_url, None)
        final_date = article_date if article_date else None
        if final_date is None:
            try:
                from email.utils import parsedate_to_datetime
                gd = parsedate_to_datetime(item.get("published date", ""))
                final_date = gd.astimezone(JST)
            except Exception:
                final_date = None

        if fresh:
            stats["fresh_saved"] += 1
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
            "collected_at": now_str,
            "collected_at_source": "runtime",
        })
        existing_urls.add(real_url)
        log(f"[{i}/{len(uniq)}] 新規取得: {title[:40]} ({len(text)}文字)")
        time.sleep(random.uniform(1.0, 2.0))

    log(
        f"\n対応表で判定: {stats['map_hit']}件 / 新たに変換: {stats['decoded']}件 / 変換失敗: {stats['decode_failed']}件"
        f"\n保存済み: {stats['known']}件 / 失敗{MAX_FETCH_FAILURES}回で打ち切り済み: {stats['gave_up']}件"
        f"\n本当に新規の候補: {stats['new_candidates']}件 → 保存 {len(new_rows)}件 / 失敗 {stats['body_failed']}件"
    )

    # URL変換の失敗は、この実行で変換が他に1件以上成功していた場合だけ失敗回数に数える。
    # 1件も成功していなければ変換ライブラリ側の故障の可能性が高く、数えると故障中に記事が
    # 打ち切られて永久に取りこぼすため数えない(3件以上試して全滅なら下で異常終了する)
    decode_outage = stats["decoded"] == 0 and stats["decode_failed"] > 0
    if decode_outage and map_hit_links:
        # 新着が無い日は、変換を試すのが「毎回失敗するリンク」だけになり、故障と見分けがつかない。
        # 変換済みのリンクを1件だけ再変換して(カナリア)、ライブラリが動いているかを確かめる
        canary_google, canary_real = map_hit_links[0]
        canary_ok = decode_url(canary_google) == canary_real
        log(f"URL変換の動作確認(変換済みリンクを1件再変換): {'成功' if canary_ok else '失敗'}")
        decode_outage = not canary_ok
    if not decode_outage:
        for google_url, title in decode_failed_links:
            f = failures.setdefault(google_url, {"title": title, "fail_count": 0, "first_failed_at": now_str, "last_failed_at": now_str})
            f["fail_count"] += 1
            f["last_failed_at"] = now_str
    elif decode_failed_links:
        log(f"⚠️ この実行ではURL変換が1件も成功していないため、変換失敗{len(decode_failed_links)}件は失敗回数に数えない")

    os.makedirs(DATA_DIR or ".", exist_ok=True)
    save_url_map(url_map)
    save_failures(failures)

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([df, new_df], ignore_index=True) if len(df) else new_df
        max_id = df["article_id"].max() if "article_id" in df.columns and len(df) else 0
        if pd.isna(max_id):
            max_id = 0
        if "article_id" not in combined.columns:
            combined["article_id"] = None
        next_id = int(max_id) + 1
        for idx in combined.index[combined["article_id"].isna()]:
            combined.at[idx, "article_id"] = next_id
            next_id += 1
        combined.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig")
        log(f"保存完了: {FULL_DATA_PATH} (合計 {len(combined)}件)")
    else:
        log("新規記事なし。ファイルは変更しません。")

    log("=== 収集完了 ===")

    if n_windows and len(SEARCH_ERRORS) >= n_windows:
        log(f"\n❌ 検索(GNews)がすべての窓で失敗しました: {SEARCH_ERRORS}")
        sys.exit(1)
    if decode_outage and stats["decode_failed"] >= ALL_FAILED_ALERT_MIN:
        log(f"\n❌ GoogleニュースURLの変換を{stats['decode_failed']}件試して、1件も成功しませんでした。")
        log("   URL変換ライブラリの仕様変更や故障の可能性があります。異常終了します。")
        sys.exit(1)
    if stats["fresh_candidates"] >= ALL_FAILED_ALERT_MIN and stats["fresh_saved"] == 0:
        log(f"\n❌ 初めて見つかった新規の記事{stats['fresh_candidates']}件のうち、本文を取得できた記事が0件です。")
        log("   本文取得の仕組みや取得先サイトの仕様変更の可能性があります。異常終了します。")
        sys.exit(1)


if __name__ == "__main__":
    main()
