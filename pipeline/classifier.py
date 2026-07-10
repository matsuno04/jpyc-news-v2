"""
JPYCニュース Haiku分類スクリプト(週次自動実行用)

- classification_status == 'failed' の記事を先にリトライ対象に戻す
- classification_status == 'pending' の記事を日付昇順でHaikuに投げ、
  tags / entities / relevance / event_id(burst) / is_commentary を付与する
- credit_balance_too_low を検知したら sys.exit(1) で即座に異常終了する
  (GitHub Actionsのジョブ失敗メール通知をトリガーするため)
- それ以外の失敗は retries=2 でリトライ後、classification_status='failed' として次に進む
"""
import os
import sys
import json
import time
from datetime import datetime

import pandas as pd
from anthropic import Anthropic

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FULL_DATA_PATH = os.environ.get(
    "FULL_DATA_PATH", os.path.join("..", "jpyc-news-data", "raw_articles_full.csv")
)
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"
MAX_CHARS = 4000
BURST_WINDOW_DAYS = 14
RETRIES = 2
SAVE_INTERVAL = 25

TAGS = {
    "活用事例":       "JPYCが実際の送金・決済・システム連携などで活用されている具体的な事例",
    "実証実験":       "PoC・実証実験としての利用・検証に関する話題",
    "取扱い・対応":   "取引所・ウォレット・サービス等がJPYCの取扱い/対応を開始したという話題",
    "提携・連携":     "他社・団体との提携、業務連携の発表",
    "発行・資金":     "JPYCの発行量、準備金、資金調達などの話題",
    "制度・規制":     "資金移動業登録、金融庁対応など制度・法規制に関する話題",
    "市場・統計":     "発行残高、取引量、流通量などの統計データに関する話題",
    "競合・市場環境": "他のステーブルコインや競合サービスとの比較、業界全体の動向",
    "解説・論説":     "事実報道ではなく、解説・分析・論説的な記事(性質タグ、他タグと併用可)",
    "リスク・懸念":   "ハッキング・障害・訴訟・批判的論調など、JPYCの信頼性に疑念を抱かせる具体的な出来事",
    "その他":         "上記いずれにも当てはまらない場合のみ",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY が設定されていません")
    return Anthropic(api_key=api_key)


client = get_client()


def build_prompt(title, text, candidates):
    tag_list = "\n".join(f"- {k}: {v}" for k, v in TAGS.items())
    body = str(text)[:MAX_CHARS]

    if candidates:
        cand_lines = "\n".join(
            f'- event_id="{c["event_id"]}" (開始日:{c["burst_start_date"]}): {c["title"][:60]}'
            for c in candidates
        )
        burst_section = f"""
【進行中のburst候補】(直近{BURST_WINDOW_DAYS}日以内に開始した報道の波)
{cand_lines}

この記事がこれらのどれかと同じ出来事を報じている場合は、その event_id をそのまま使ってください。
どれとも異なる新しい出来事であれば "NEW" としてください。
"""
    else:
        burst_section = "\n【進行中のburst候補】現在なし。新しい出来事であれば \"NEW\" としてください。\n"

    return f"""以下のJPYCニュース記事を分析し、指定するJSON形式で結果だけを出力してください。

【タグ一覧】(該当するものを複数選択可)
{tag_list}

判定基準:
- 話題が記事の「主題」または「明確な焦点」になっている場合のみタグを付ける。一度触れた程度・背景説明のみでは付けない
- 「解説・論説」は事実報道には付けない。記事の性質タグなので他のタグと併用可
- 「その他」は主要タグが一つも当てはまらないときのみ
- 「リスク・懸念」はハッキング・障害・訴訟・批判的論調など、JPYCの信頼性に疑念を抱かせる具体的な出来事が対象

【entities】記事の核となる固有名詞を2〜4個(組織名・人物名・制度名・サービス名など)。
「JPYC」「ステーブルコイン」「日本円」等の頻出語は除外する。

【relevance】JPYCがこの記事の主題としてどれだけ中心的かを0〜100のスコアで判定する。
{burst_section}
【relevanceが30未満の場合】event_id_choice は必ず null にしてください。

【タイトル】
{title}

【本文】
{body}

以下のJSON形式のみを出力してください(説明文不要):
{{"tags": ["タグ名", ...], "entities": ["固有名詞", ...], "relevance": 0-100の整数, "event_id_choice": "既存event_id または NEW または null"}}
"""


def call_haiku(title, text, candidates):
    prompt = build_prompt(title, text, candidates)
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
            tags = [t for t in data.get("tags", []) if t in TAGS]
            entities = data.get("entities", [])
            relevance = int(data.get("relevance", 0))
            event_choice = data.get("event_id_choice")
            return tags, entities, relevance, event_choice
        except Exception as e:
            msg = str(e)
            if "credit_balance_too_low" in msg:
                log("FATAL: credit_balance_too_low を検出。処理を中断します。")
                sys.exit(1)
            last_err = e
            log(f"  リトライ ({attempt + 1}/{RETRIES}): {e}")
            time.sleep(1.5)
    raise last_err


def main():
    log("読み込み開始")
    if not os.path.exists(FULL_DATA_PATH):
        log(f"データファイルが見つかりません: {FULL_DATA_PATH}")
        sys.exit(1)

    df = pd.read_csv(FULL_DATA_PATH)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["tags", "entities", "event_id", "classification_status"]:
        if col in df.columns:
            df[col] = df[col].astype(object)
    df["is_commentary"] = df["is_commentary"].astype(object) if "is_commentary" in df.columns else None
    df["relevance"] = df["relevance"].astype(object) if "relevance" in df.columns else None
    df["burst_start_date"] = df["burst_start_date"].astype(object) if "burst_start_date" in df.columns else None
    log(f"total: {len(df)}")

    empty_text_mask = df["text"].isna() | (df["text"].astype(str).str.strip() == "")
    df.loc[empty_text_mask & (df["classification_status"] == "pending"), "classification_status"] = "failed"

    retry_mask = (df["classification_status"] == "failed") & (~empty_text_mask)
    df.loc[retry_mask, "classification_status"] = "pending"
    log(f"failed からリトライ対象に戻した件数: {retry_mask.sum()}")

    df = df.sort_values("date", na_position="last").reset_index(drop=True)

    active_bursts = []
    event_seq_by_day = {}

    def register_burst(event_id, start_date, title):
        active_bursts.append({"event_id": event_id, "burst_start_date": start_date, "title": title})

    for _, row in df[df["classification_status"] == "done"].iterrows():
        eid = row.get("event_id")
        if pd.notna(eid) and eid and pd.notna(row.get("burst_start_date")):
            if eid not in [b["event_id"] for b in active_bursts]:
                register_burst(eid, pd.Timestamp(row["burst_start_date"]).date(), row.get("title", ""))

    todo_idx = df.index[df["classification_status"] == "pending"].tolist()
    log(f"分類対象: {len(todo_idx)}件")

    done_count = 0
    fail_count = 0

    for i, idx in enumerate(todo_idx, 1):
        row = df.loc[idx]
        article_date = row["date"]
        article_date_d = article_date.date() if pd.notna(article_date) else None

        if article_date_d is not None:
            candidates = [
                {"event_id": b["event_id"], "burst_start_date": b["burst_start_date"], "title": b["title"]}
                for b in active_bursts
                if 0 <= (article_date_d - b["burst_start_date"]).days <= BURST_WINDOW_DAYS
            ]
        else:
            candidates = []

        try:
            tags, entities, relevance, event_choice = call_haiku(row["title"], row["text"], candidates)
        except Exception as e:
            log(f"[{i}/{len(todo_idx)}] 失敗(記録): {str(row['title'])[:30]} | {e}")
            df.at[idx, "classification_status"] = "failed"
            fail_count += 1
            continue

        is_commentary = 1 if "解説・論説" in tags else 0

        event_id_final = None
        burst_start_date_final = None
        if relevance >= 30 and article_date_d is not None:
            if event_choice and event_choice not in ("null", "NEW"):
                match = next((b for b in active_bursts if b["event_id"] == event_choice), None)
                if match:
                    event_id_final = match["event_id"]
                    burst_start_date_final = match["burst_start_date"]
            if event_id_final is None and event_choice == "NEW":
                day_key = article_date_d.strftime("%Y%m%d")
                seq = event_seq_by_day.get(day_key, 0) + 1
                event_seq_by_day[day_key] = seq
                new_id = f"EVT-{day_key}-{seq:02d}"
                register_burst(new_id, article_date_d, row["title"])
                event_id_final = new_id
                burst_start_date_final = article_date_d

        df.at[idx, "tags"] = "、".join(tags)
        df.at[idx, "entities"] = "、".join(entities)
        df.at[idx, "relevance"] = relevance
        df.at[idx, "is_commentary"] = is_commentary
        df.at[idx, "event_id"] = event_id_final
        df.at[idx, "burst_start_date"] = burst_start_date_final
        df.at[idx, "classification_status"] = "done"
        done_count += 1

        if i % 10 == 0 or i == len(todo_idx):
            log(f"[{i}/{len(todo_idx)}] done={done_count} failed={fail_count} | {str(row['title'])[:30]} -> tags={tags} rel={relevance} event={event_id_final}")

        if i % SAVE_INTERVAL == 0:
            df.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig")
            log(f"  中間保存 ({i}/{len(todo_idx)})")

    df.to_csv(FULL_DATA_PATH, index=False, encoding="utf-8-sig")
    log(f"=== 完了: done={done_count} failed={fail_count} ===")


if __name__ == "__main__":
    main()
