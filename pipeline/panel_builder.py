"""
JPYCニュース event_summary / daily_panel / 公開用raw_articles 生成スクリプト(週次自動実行用)

- 非公開リポジトリの raw_articles_full.csv (本文付き) を入力とする
- event_summary.csv: event_id単位の集計(severity_index等)
- daily_panel.csv: 日次集計(オンチェーンパネルとdate列の型・粒度を合わせる: YYYY-MM-DD)
- raw_articles.csv: text/text_source を除いた公開版(著作権上の理由)
すべて公開リポジトリの data/ ディレクトリに保存する。
"""
import os
import sys
import math
from datetime import timedelta

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FULL_DATA_PATH = os.environ.get(
    "FULL_DATA_PATH", os.path.join("..", "jpyc-news-data", "raw_articles_full.csv")
)
PUBLIC_DATA_DIR = os.environ.get("PUBLIC_DATA_DIR", "data")

BURST_WINDOW_DAYS = 14
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
        grp = grp.sort_values("date")
        count = len(grp)
        rel_mean = grp["relevance"].mean()
        severity = math.log(1 + count) * (rel_mean / 100)

        tag_counter = {}
        for tags_str in grp["tags"].dropna():
            for t in str(tags_str).split("、"):
                t = t.strip()
                if t:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
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

    event_summary = pd.DataFrame(rows).sort_values("burst_start_date").reset_index(drop=True)
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

    log("=== 完了 ===")


if __name__ == "__main__":
    main()
