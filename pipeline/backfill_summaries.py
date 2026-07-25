"""
JPYCニュース summary 追加抽出バッチ(手動実行専用)

- classification_status == 'done' かつ summary が未取得の既存記事に対し、
  Haikuで summary のみを追加抽出する(tags/entities/relevance/event_id等の再分類は行わない)
- 週次自動ワークフロー(update.yml)には組み込まない。必要な時に手動で実行する
- credit_balance_too_low を検知したら sys.exit(1) で即座に異常終了する
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime

import pandas as pd

from classifier import get_client, MAX_CHARS, CLASSIFY_MODEL, FULL_DATA_PATH

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RETRIES = 2
SAVE_INTERVAL = 25

client = get_client()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def build_summary_prompt(title, text):
    body = str(text)[:MAX_CHARS]
    return f"""以下のJPYCニュース記事の本文(text)を根拠に、日本語で2〜3文程度の要約を生成してください。

- 本文に書かれていない情報を推測・補完しない(non-fabrication厳守)
- 固有名詞・数値は本文の表記をそのまま使う

【タイトル】
{title}

【本文】
{body}

以下のJSON形式のみを出力してください(説明文不要):
{{"summary": "要約文"}}
"""


def call_haiku_summary(title, text):
    prompt = build_summary_prompt(title, text)
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            resp = client.messages.create(
                model=CLASSIFY_MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            return str(data.get("summary", "")).strip()
        except Exception as e:
            msg = str(e)
            if "credit_balance_too_low" in msg:
                log("FATAL: credit_balance_too_low を検出。処理を中断します。")
                sys.exit(1)
            last_err = e
            log(f"  リトライ ({attempt + 1}/{RETRIES}): {e}")
            time.sleep(1.5)
    raise last_err


def parse_args():
    parser = argparse.ArgumentParser(description="summary追加抽出バッチ(手動実行専用)")
    parser.add_argument("--limit", type=int, default=None, help="処理件数の上限(テスト実行用)")
    return parser.parse_args()


def main():
    args = parse_args()
    log("読み込み開始")
    if not os.path.exists(FULL_DATA_PATH):
        log(f"データファイルが見つかりません: {FULL_DATA_PATH}")
        sys.exit(1)

    df = pd.read_csv(FULL_DATA_PATH)
    df["summary"] = df["summary"].astype(object) if "summary" in df.columns else None
    log(f"total: {len(df)}")

    missing_summary = df["summary"].isna() | (df["summary"].astype(str).str.strip() == "")
    empty_text_mask = df["text"].isna() | (df["text"].astype(str).str.strip() == "")

    todo_idx = df.index[
        (df["classification_status"] == "done") & missing_summary & (~empty_text_mask)
    ].tolist()
    skipped_empty_text = (
        (df["classification_status"] == "done") & missing_summary & empty_text_mask
    ).sum()
    log(f"summary抽出対象: {len(todo_idx)}件 (本文なしでスキップ: {skipped_empty_text}件)")

    if args.limit is not None:
        todo_idx = todo_idx[: args.limit]
        log(f"--limit指定によりテスト実行: 先頭{len(todo_idx)}件のみ処理")

    done_count = 0
    fail_count = 0

    for i, idx in enumerate(todo_idx, 1):
        row = df.loc[idx]
        try:
            summary = call_haiku_summary(row["title"], row["text"])
        except Exception as e:
            log(f"[{i}/{len(todo_idx)}] 失敗(スキップ): {str(row['title'])[:30]} | {e}")
            fail_count += 1
            continue

        df.at[idx, "summary"] = summary
        done_count += 1

        if i % 10 == 0 or i == len(todo_idx):
            log(f"[{i}/{len(todo_idx)}] done={done_count} failed={fail_count} | {str(row['title'])[:30]}")

        if i % SAVE_INTERVAL == 0:
            df.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig")
            log(f"  中間保存 ({i}/{len(todo_idx)})")

    df.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig")
    log(f"=== 完了: done={done_count} failed={fail_count} ===")


if __name__ == "__main__":
    main()
