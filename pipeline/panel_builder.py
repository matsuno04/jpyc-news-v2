"""
JPYCニュース event_summary / daily_panel / 公開用raw_articles / recent_events 生成スクリプト(毎日自動実行用。2026-10-01までは週次)

- 非公開リポジトリの raw_articles_full.csv (本文付き) を入力とする
- event_summary.csv: event_id単位の集計(severity_index等)
- daily_panel.csv: 日次集計(オンチェーンパネルとdate列の型・粒度を合わせる: YYYY-MM-DD)
- raw_articles.csv: text/text_source を除いた公開版(著作権上の理由)
- recent_events.json: 直近30日に収集された記事を含む出来事の一覧(デイリーニュースダッシュボード表示用、本文なし)
すべて公開リポジトリの data/ ディレクトリに保存する。
"""
import os
import sys
import math
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FULL_DATA_PATH = os.environ.get(
    "FULL_DATA_PATH", os.path.join("..", "jpyc-news-data", "raw_articles_full.csv")
)
PUBLIC_DATA_DIR = os.environ.get("PUBLIC_DATA_DIR", "data")

BURST_WINDOW_DAYS = 14
RECENT_EVENTS_DAYS = 30
JST = timezone(timedelta(hours=9))
TAG_COLS = {
    "リスク・懸念": "is_リスク懸念_t",
    "制度・規制": "is_制度規制_t",
    "発行・資金": "is_発行資金_t",
    "取扱い・対応": "is_取扱い対応_t",
    "活用事例": "is_活用事例_t",
    "実証実験": "is_実証実験_t",
    "提携・連携": "is_提携連携_t",
    "市場・統計": "is_市場統計_t",
    "競合・市場環境": "is_競合市場環境_t",
}


def log(msg):
    print(msg, flush=True)


def _str_or_none(v):
    return None if pd.isna(v) or v == "" else str(v)


def build_recent_events(df, now_jst):
    """直近RECENT_EVENTS_DAYS日に収集された記事を1件でも含む出来事を、ダッシュボード表示用にまとめる。

    - 初出(first_collected_at)は、代表記事の公開日ではなく、出来事に属する記事の最も早い収集日時
      (公開日が古い記事が後から見つかることがあるため、「いつダッシュボードに新しく現れたか」は収集日時で決める)
    - 代表記事は event_summary.csv と同じく、公開日が最も古い記事
    - 本文(text)は含めない
    - relevance 30未満で event_id が付かない記事は、定義上出来事ではないので含めない
    """
    since = now_jst.replace(tzinfo=None) - timedelta(days=RECENT_EVENTS_DAYS)
    ev = df[df["event_id"].notna()].copy()
    ev["_collected"] = pd.to_datetime(ev.get("collected_at"), errors="coerce")
    recent_ids = set(ev.loc[ev["_collected"] >= since, "event_id"])

    events = []
    for eid in sorted(recent_ids):
        grp = ev[ev["event_id"] == eid].sort_values(["date", "url"], kind="mergesort")
        rep = grp.iloc[0]
        tag_counter = {}
        for tags_str in grp["tags"].dropna():
            for t in str(tags_str).split("、"):
                t = t.strip()
                if t:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
        tags = sorted(tag_counter, key=lambda t: -tag_counter[t])
        collected = grp["_collected"].dropna()
        events.append({
            "event_id": eid,
            "burst_start_date": _str_or_none(rep.get("burst_start_date")),
            "first_collected_at": collected.min().strftime("%Y-%m-%d %H:%M:%S") if len(collected) else None,
            "last_collected_at": collected.max().strftime("%Y-%m-%d %H:%M:%S") if len(collected) else None,
            "article_count": int(len(grp)),
            "tags": tags,
            "tags_mode": tags[0] if tags else "",
            "representative": {
                "title": _str_or_none(rep["title"]),
                "domain": _str_or_none(rep.get("domain")),
                "url": _str_or_none(rep["url"]),
                "published_at": rep["date"].strftime("%Y-%m-%d %H:%M:%S") if pd.notna(rep["date"]) else None,
                "summary": _str_or_none(rep.get("summary")),
            },
            "articles": [
                {
                    "title": _str_or_none(a["title"]),
                    "domain": _str_or_none(a.get("domain")),
                    "url": _str_or_none(a["url"]),
                    "published_at": a["date"].strftime("%Y-%m-%d %H:%M:%S") if pd.notna(a["date"]) else None,
                    "collected_at": a["_collected"].strftime("%Y-%m-%d %H:%M:%S") if pd.notna(a["_collected"]) else None,
                }
                for _, a in grp.iterrows()
            ],
        })
    # 新しく動きのあった出来事が先。同じ収集日時(同じ実行で収集)の出来事どうしは event_id の降順で固定する
    events.sort(key=lambda e: (e["last_collected_at"] or "", e["event_id"]), reverse=True)
    return {
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": RECENT_EVENTS_DAYS,
        "timezone": "JST(+09:00)",
        "events": events,
    }


def main():
    if not os.path.exists(FULL_DATA_PATH):
        log(f"データファイルが見つかりません: {FULL_DATA_PATH}")
        sys.exit(1)

    df = pd.read_csv(FULL_DATA_PATH)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["day"] = df["date"].dt.date
    log(f"total: {len(df)}")

    os.makedirs(PUBLIC_DATA_DIR, exist_ok=True)

    # ------------------------------------------------------------
    # 公開用 raw_articles.csv (本文なし)
    # ------------------------------------------------------------
    public_cols = [c for c in df.columns if c not in ("text", "text_source", "day")]
    public_df = df[public_cols]
    public_df.to_csv(os.path.join(PUBLIC_DATA_DIR, "raw_articles.csv"), index=False, encoding="utf-8-sig")
    log(f"raw_articles.csv 保存: {len(public_df)}件")

    # ------------------------------------------------------------
    # event_summary.csv
    # ------------------------------------------------------------
    events = df[df["event_id"].notna()].copy()
    rows = []
    for eid, grp in events.groupby("event_id"):
        # 公開日順、同じ公開日時の記事はURL順に固定する。代表記事(先頭)・固有名詞の並び・最頻タグの同点時の
        # 選ばれ方がこの順番で決まるため、固定しないと同じデータでも実行ごとに変わっていた(2026-10-01)
        grp = grp.sort_values(["date", "url"], kind="mergesort")
        count = len(grp)
        rel_mean = grp["relevance"].mean()
        severity = math.log(1 + count) * (rel_mean / 100)

        tag_counter = {}
        for tags_str in grp["tags"].dropna():
            for t in str(tags_str).split("、"):
                t = t.strip()
                if t:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
        # 最頻タグ。件数が同じタグが複数あるときは、上の順番(公開日→URL)で先に現れたタグにする
        tags_mode = max(tag_counter, key=tag_counter.get) if tag_counter else ""

        entity_set = []
        for ent_str in grp["entities"].dropna():
            for e in str(ent_str).split("、"):
                e = e.strip()
                if e and e not in entity_set:
                    entity_set.append(e)

        rows.append({
            "event_id": eid,
            "burst_start_date": grp["burst_start_date"].iloc[0],
            "event_article_count": count,
            "event_relevance_mean": round(rel_mean, 2),
            "severity_index": round(severity, 4),
            "tags_mode": tags_mode,
            "entities_union": "、".join(entity_set),
            "representative_title": grp.iloc[0]["title"],
        })

    # 並び順は「開始日→event_id」で固定する。開始日だけで並べると、同じ開始日の出来事どうしの順番が
    # 実行のたびに入れ替わり、毎日実行で意味のない差分が出るため(2026-10-01。中身・数値は変わらない)
    event_summary = pd.DataFrame(rows).sort_values(["burst_start_date", "event_id"]).reset_index(drop=True)
    event_summary.to_csv(os.path.join(PUBLIC_DATA_DIR, "event_summary.csv"), index=False, encoding="utf-8-sig")
    log(f"event_summary.csv 保存: {len(event_summary)}件")

    # ------------------------------------------------------------
    # daily_panel.csv
    # ------------------------------------------------------------
    rel30 = df[df["relevance"] >= 30].copy()
    min_day = df["day"].min()
    max_day = df["day"].max()
    all_days = pd.date_range(min_day, max_day, freq="D").date

    rel30_by_day = rel30.groupby("day")
    freq_by_day = rel30_by_day.size()
    domains_by_day = rel30_by_day["domain"].apply(lambda s: set(s.dropna()))

    burst_list = event_summary[["event_id", "burst_start_date"]].copy()
    burst_list["burst_start_date"] = pd.to_datetime(burst_list["burst_start_date"]).dt.date
    burst_list = burst_list.sort_values("burst_start_date")

    def tags_on_day(day_df):
        present = set()
        for tags_str in day_df["tags"].dropna():
            for t in str(tags_str).split("、"):
                t = t.strip()
                if t:
                    present.add(t)
        return present

    tags_by_day = df.groupby("day").apply(tags_on_day)
    severity_map = dict(zip(event_summary["event_id"], event_summary["severity_index"]))
    event_ids_by_day = df[df["event_id"].notna()].groupby("day")["event_id"].apply(set)

    panel_rows = []
    for d in all_days:
        freq = int(freq_by_day.get(d, 0))
        domain_window = set()
        for i in range(7):
            domain_window |= domains_by_day.get(d - timedelta(days=i), set())
        breadth = len(domain_window)

        active = burst_list[
            (burst_list["burst_start_date"] <= d) &
            ((d - burst_list["burst_start_date"]).apply(lambda x: x.days) <= BURST_WINDOW_DAYS)
        ]
        days_since = (d - active["burst_start_date"].max()).days if len(active) > 0 else None

        eids_today = event_ids_by_day.get(d, set())
        sev_today = max([severity_map.get(e, 0) for e in eids_today], default=0.0)
        tags_today = tags_by_day.get(d, set())

        row = {
            "date": d.strftime("%Y-%m-%d"),
            "frequency_t": freq,
            "breadth_t": breadth,
            "days_since_burst_start_t": days_since,
            "severity_index_t": round(sev_today, 4),
        }
        for tag, col in TAG_COLS.items():
            row[col] = 1 if tag in tags_today else 0
        panel_rows.append(row)

    daily_panel = pd.DataFrame(panel_rows)
    daily_panel.to_csv(os.path.join(PUBLIC_DATA_DIR, "daily_panel.csv"), index=False, encoding="utf-8-sig")
    log(f"daily_panel.csv 保存: {len(daily_panel)}件")

    # ------------------------------------------------------------
    # recent_events.json (デイリーニュースダッシュボード表示用)
    # ------------------------------------------------------------
    recent = build_recent_events(df, datetime.now(JST))
    recent_path = os.path.join(PUBLIC_DATA_DIR, "recent_events.json")
    # 生成時刻以外が前回と同じなら書き換えない(新着が無い日に、生成時刻だけの差分がコミットされるのを防ぐ)
    previous = None
    if os.path.exists(recent_path):
        with open(recent_path, encoding="utf-8") as f:
            previous = json.load(f)
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_at"}
    if previous is not None and strip(previous) == strip(recent):
        log(f"recent_events.json: 生成時刻以外に変更なし(書き換えない)。{len(recent['events'])}件")
    else:
        with open(recent_path, "w", encoding="utf-8") as f:
            json.dump(recent, f, ensure_ascii=False, indent=1)
        log(f"recent_events.json 保存: {len(recent['events'])}件(直近{RECENT_EVENTS_DAYS}日)")

    log("=== 完了 ===")


if __name__ == "__main__":
    main()
