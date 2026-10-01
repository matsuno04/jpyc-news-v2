"""
既存記事の収集日時(collected_at)を、非公開リポジトリ jpyc-news-data の git 履歴から復元する(1回限りの作業用)

2026-10-01 に collected_at 列を導入した際、既存記事の値は次のように埋めると決めた(運用者の判断):
- Actions の実行で追加された記事: その記事が初めて現れたコミットの日時(JST)。collected_at_source="git_commit"
  (収集・分類・コミットは1回の実行内なので、実際の取得の数分後の時刻になる)
- 初期一括取り込み(2026-07-10、最初のコミット)の記事: 取り込み前にPCで収集した日時の記録がないため空欄。
  collected_at_source="unknown"
- 公開日での代用はしない(収集日時と意味が違い、分析で混同するおそれがあるため)

collected_at が既に入っている記事は変更しない。何度実行しても結果は同じ。

使い方:
  python pipeline/backfill_collected_at.py              # FULL_DATA_PATH を書き換える
  OUTPUT_PATH=/tmp/x.csv python pipeline/backfill_collected_at.py   # 別ファイルに書き出す(ドライラン)
"""
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FULL_DATA_PATH = os.environ.get(
    "FULL_DATA_PATH", os.path.join("..", "jpyc-news-data", "raw_articles_full.csv")
)
# git履歴を読む非公開リポジトリ(FULL_DATA_PATHと別の場所でドライランする場合に指定)
DATA_REPO_PATH = os.environ.get("DATA_REPO_PATH", os.path.dirname(os.path.abspath(FULL_DATA_PATH)))
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", FULL_DATA_PATH)
FILE_IN_REPO = "raw_articles_full.csv"
JST = timezone(timedelta(hours=9))


def git(*args, binary=False):
    r = subprocess.run(["git", "-C", DATA_REPO_PATH, *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8")


def main():
    commits = [line.split("\t") for line in git("log", "--reverse", "--format=%H\t%aI", "--", FILE_IN_REPO).splitlines()]
    initial_commit = commits[0][0]
    first_seen = {}
    for h, t in commits:
        snap = pd.read_csv(io.BytesIO(git("show", f"{h}:{FILE_IN_REPO}", binary=True)), encoding="utf-8-sig", usecols=["url"])
        when = datetime.fromisoformat(t).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
        for u in snap["url"].dropna().astype(str):
            first_seen.setdefault(u, (h, when))
    print(f"履歴のコミット: {len(commits)}件(最初のコミット=初期一括取り込み {initial_commit[:7]})")

    # 既存の列は1文字も変えないよう、すべて文字列のまま読み書きする
    # (数値として読むと relevance の "5" が "5.0" に変わる。Windowsで書くと本文中の改行が CRLF に変わる)
    df = pd.read_csv(FULL_DATA_PATH, dtype=str, keep_default_na=False)
    for col in ("collected_at", "collected_at_source"):
        if col not in df.columns:
            df[col] = ""

    counts = {"already": 0, "git_commit": 0, "unknown": 0, "not_in_history": 0}
    for idx, row in df.iterrows():
        if str(row["collected_at"]).strip():
            counts["already"] += 1
            continue
        seen = first_seen.get(str(row["url"]))
        if seen is None:
            counts["not_in_history"] += 1
            df.at[idx, "collected_at_source"] = "unknown"
        elif seen[0] == initial_commit:
            counts["unknown"] += 1
            df.at[idx, "collected_at_source"] = "unknown"
        else:
            counts["git_commit"] += 1
            df.at[idx, "collected_at"] = seen[1]
            df.at[idx, "collected_at_source"] = "git_commit"

    print(f"記事 {len(df)}件: 既に値あり {counts['already']} / gitから復元 {counts['git_commit']} / "
          f"初期一括で不明 {counts['unknown']} / 履歴に無い {counts['not_in_history']}")
    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"保存: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
