"""
2026-10-01 の既存データ修正(1回限りの作業用)。運用者の決定に基づく:

1. 採番の不具合で混ざった記事を分け直す(当時のHaikuの判定=新しい出来事を生かし、修正後の採番で新しいevent_idを付ける。
   Haikuによる出来事の再判定はしない)
   - EVT-20260821-01 → HashPort×KDDI×ローソンの記事を EVT-20260821-03 へ
   - EVT-20260828-01 → 被災地の防災インフラ(iolite)の記事を EVT-20260828-02 へ
   - EVT-20260831-01 → JPYC流通量の減少(8/31)の記事を EVT-20260831-02 へ。
     9/07の続報「JPYCの流通量減少止まらず」も EVT-20260831-02 へ移す(Haikuが0831-01を選んだのは、
     採番の不具合でそこに流通量の記事が入っていたためで、不具合の影響として扱う)
   - EVT-20251114-01 → アステリアの決算記事を EVT-20251114-03 へ
2. Yahoo!ニュースの画像ページ・2ページ目以降のURLの行を直す
   - A. 同じ記事の元URLの行もある重複: 続き付きの行を削除
   - B. 続き付きの行だけで元URLが取れる: URLを元URLにし、本文を取り直して、タグ・固有名詞・relevance・要約を
        同じプロンプトで付け直す(text_source="refetched")。event_idは変えない。ただし付け直したrelevanceが30未満なら
        定義どおりevent_idを外す。今event_idが無い行は、30以上になっても付けない
   - C. 画像ページだけで元URLが削除済み(404): 本文が写真の説明なので削除
   - D. 2ページ目以降だけで元URLが削除済み(404): 本文は本物(記事の途中から)なのでそのまま残す
   - E. ?source=rss: URLだけ元URLにする

使い方(本物のデータは --apply のときだけ書き換える):
  python pipeline/repair_20261001.py --plan plan.json     # 変更点を計算して計画ファイルと一覧を出す(ドライラン)
  python pipeline/repair_20261001.py --apply plan.json    # 計画ファイルどおりに適用する(Haikuは呼ばない)
"""
import argparse
import json
import os
import re
import sys

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))

FULL_DATA_PATH = os.environ.get(
    "FULL_DATA_PATH", os.path.join("..", "02_news-data", "raw_articles_full.csv")
)
YAHOO = "https://news.yahoo.co.jp/articles/"

# (元のevent_id, 移す記事の見出しに含まれる文字列, 新しいevent_id, 新しい出来事の開始日)
SPLITS = [
    ("EVT-20260821-01", "HashPort、KDDI", "EVT-20260821-03", "2026-08-21"),
    ("EVT-20260828-01", "防災インフラ", "EVT-20260828-02", "2026-08-28"),
    ("EVT-20260831-01", "流通量が約3カ月半ぶり", "EVT-20260831-02", "2026-08-31"),
    ("EVT-20260831-01", "流通量減少止まらず", "EVT-20260831-02", "2026-08-31"),
    ("EVT-20251114-01", "アステリア、前年比増収増益", "EVT-20251114-03", "2025-11-14"),
]


def load():
    # 既存の列を1文字も変えないよう、すべて文字列のまま読み書きする
    return pd.read_csv(FULL_DATA_PATH, dtype=str, keep_default_na=False)


def yahoo_parts(url):
    m = re.match(r"^https?://news\.yahoo\.co\.jp/articles/([0-9a-f]+)(.*)$", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def make_plan(df):
    from collector import get_text, _UA
    plan = {"delete": [], "split": [], "refetch": [], "url_only": [], "keep": []}
    parts = df["url"].map(yahoo_parts)
    df = df.assign(_aid=[p[0] for p in parts], _sfx=[p[1] or "" for p in parts])
    groups = df[df._aid.notna()].groupby("_aid").size()
    multi = set(groups[groups > 1].index)

    for _, r in df[(df._sfx != "") & df._aid.notna()].sort_values(["date", "url"]).iterrows():
        row = {"article_id": r.article_id, "url": r.url, "title": r.title, "date": r.date, "event_id": r.event_id}
        if r._aid in multi:
            plan["delete"].append({**row, "reason": "A: 同じ記事の元URLの行もある重複"})
            continue
        base = YAHOO + r._aid
        if r._sfx.startswith("?source"):
            plan["url_only"].append({**row, "new_url": base, "reason": "E: 追跡パラメータを除く"})
            continue
        status = requests.get(base, headers={"User-Agent": _UA}, timeout=15).status_code
        if status == 404:
            if r._sfx.startswith("/images/"):
                plan["delete"].append({**row, "reason": "C: 画像ページだけで、元URLが削除済み(404)"})
            else:
                plan["keep"].append({**row, "reason": "D: 2ページ目以降だけで、元URLが削除済み(404)。本文は本物なので残す"})
            continue
        if status != 200:
            raise SystemExit(f"元URLの状態が想定外です(HTTP {status}): {base}  時間をおいて再実行してください")
        text, _ = get_text(base)
        if not text:
            raise SystemExit(f"元URLから本文を取得できませんでした: {base}")
        plan["refetch"].append({**row, "new_url": base, "new_text": text, "old_text_len": len(r.text),
                                "old": {k: r[k] for k in ("tags", "entities", "relevance", "summary", "is_commentary")},
                                "reason": "B: 元URLで本文を取り直して付け直す"})

    # 付け直し(Haikuは出来事の判定には使わない。候補は渡さず、出来事の答えは捨てる)
    if plan["refetch"]:
        import classifier
        for p in plan["refetch"]:
            tags, entities, relevance, summary, _ = classifier.call_haiku(p["title"], p["new_text"], [])
            p["new"] = {"tags": "、".join(tags), "entities": "、".join(entities), "relevance": float(relevance),
                        "summary": summary, "is_commentary": 1.0 if "解説・論説" in tags else 0.0}
            if relevance < 30 and p["event_id"]:
                p["event_change"] = "relevanceが30未満になったため、定義どおりevent_idを外す"
            elif relevance >= 30 and not p["event_id"]:
                p["event_change"] = "relevanceが30以上になったが、出来事の再判定はしないためevent_idは付けない"

    deleted = {p["url"] for p in plan["delete"]}
    for old, pat, new, start in SPLITS:
        g = df[(df.event_id == old) & df.title.str.contains(pat, regex=False) & ~df.url.isin(deleted)]
        for _, r in g.sort_values(["date", "url"]).iterrows():
            plan["split"].append({"article_id": r.article_id, "url": r.url, "title": r.title, "date": r.date,
                                  "from": old, "to": new, "burst_start_date": start})
    taken = set(df.event_id)
    for p in plan["split"]:
        assert p["to"] not in taken, f"{p['to']} は既に使われています"
    return plan


def apply_plan(df, plan):
    idx = {u: i for i, u in enumerate(df["url"])}
    for p in plan["split"]:
        i = idx[p["url"]]
        assert df.at[i, "event_id"] == p["from"], p
        df.at[i, "event_id"] = p["to"]
        df.at[i, "burst_start_date"] = p["burst_start_date"]
    for p in plan["url_only"]:
        df.at[idx[p["url"]], "url"] = p["new_url"]
    for p in plan["refetch"]:
        i = idx[p["url"]]
        df.at[i, "url"] = p["new_url"]
        df.at[i, "text"] = p["new_text"]
        df.at[i, "text_source"] = "refetched"
        for k, v in p["new"].items():
            df.at[i, k] = str(float(v)) if k in ("relevance", "is_commentary") else v
        if p["new"]["relevance"] < 30:
            df.at[i, "event_id"] = ""
            df.at[i, "burst_start_date"] = ""
    drop = {idx[p["url"]] for p in plan["delete"]}
    return df.drop(index=sorted(drop)).reset_index(drop=True)


def write_report(plan, path):
    L = ["# 2026-10-01 既存データ修正の変更点(ドライラン)", ""]
    L += [f"- 分け直し: {len(plan['split'])}件 / 削除: {len(plan['delete'])}件 / 本文の取り直し・付け直し: {len(plan['refetch'])}件"
          f" / URLだけ修正: {len(plan['url_only'])}件 / 残す: {len(plan['keep'])}件", ""]
    L += ["## 分け直し", "", "| 公開日 | 見出し | 元のevent_id | 新しいevent_id |", "|---|---|---|---|"]
    L += [f"| {p['date'][:16]} | {p['title'][:60]} | {p['from']} | {p['to']} |" for p in plan["split"]]
    L += ["", "## 本文の取り直し・付け直し(B)", ""]
    for p in plan["refetch"]:
        o, n = p["old"], p["new"]
        L += [f"### {p['title'][:60]}", "",
              f"- URL: `{p['url']}` → `{p['new_url']}`",
              f"- 本文: {p['old_text_len']}字 → {len(p['new_text'])}字(先頭: {p['new_text'][:60]}…)",
              f"- relevance: {o['relevance']} → {n['relevance']} / タグ: {o['tags']} → {n['tags']}",
              f"- 固有名詞: {o['entities']} → {n['entities']}",
              f"- event_id: {p['event_id'] or '(なし)'}" + (f" ※{p['event_change']}" if p.get("event_change") else "(変えない)"),
              f"- 要約(新): {n['summary']}", ""]
    for key, title in (("delete", "削除"), ("url_only", "URLだけ修正(E)"), ("keep", "残す(D)")):
        L += [f"## {title}", "", "| 区分 | 公開日 | 見出し | URL | event_id |", "|---|---|---|---|---|"]
        L += [f"| {p['reason'][:2]} | {p['date'][:10]} | {p['title'][:50]} | …{p['url'][-30:]} | {p['event_id'] or ''} |" for p in plan[key]]
        L.append("")
    open(path, "w", encoding="utf-8").write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan")
    g.add_argument("--apply")
    a = ap.parse_args()
    df = load()
    if a.plan:
        plan = make_plan(df)
        json.dump(plan, open(a.plan, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        report = os.path.splitext(a.plan)[0] + ".md"
        write_report(plan, report)
        print({k: len(v) for k, v in plan.items()}, "→", a.plan, report)
    else:
        plan = json.load(open(a.apply, encoding="utf-8"))
        before = len(df)
        df = apply_plan(df, plan)
        df.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig", lineterminator="\n")
        print(f"適用しました: 記事 {before} → {len(df)}件 / {FULL_DATA_PATH}")


if __name__ == "__main__":
    main()
