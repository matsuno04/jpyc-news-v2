# jpyc-news-v2

日本円ステーブルコイン「JPYC」に関するニュース記事を収集・分類・集計するパイプライン(卒業論文の研究データ)。

- ダッシュボード: https://matsuno04.github.io/jpyc-news-v2/
- 本文込みのマスターデータは非公開リポジトリ `jpyc-news-data` に置く(著作権の都合で本文は公開しない)

## 処理の流れ(`.github/workflows/update.yml`)

毎日 21:10 JST に GitHub Actions で実行する。

| 手順 | スクリプト | 内容 |
|---|---|---|
| 1. 収集 | `pipeline/collector.py` | Googleニュースを `"JPYC" OR "ジェイピーワイシー"` で検索(月曜は直近14日、それ以外は直近3日)。Googleのリンクを元記事URLに変換し、本文を取得して `classification_status=pending` で追記する |
| 2. 分類 | `pipeline/classifier.py` | pending の記事を Haiku で分類(tags / entities / relevance / summary / event_id) |
| 3. 集計 | `pipeline/panel_builder.py` | 公開用の `data/` を生成 |

手動実行(workflow_dispatch)では、収集で見直す日数 `lookback_days` を指定できる(空欄なら自動)。取りこぼした期間を取り戻すときに使う。

## データ

### 非公開リポジトリ `jpyc-news-data`
| ファイル | 内容 |
|---|---|
| `raw_articles_full.csv` | 全記事(本文込み)。分類結果と収集日時を含む |
| `url_map.csv` | Googleニュースのリンクと元記事URLの対応表。変換済みのリンクは再変換しない |
| `fetch_failures.csv` | 本文取得に失敗した記事と失敗回数。3回失敗したら以後は再試行しない |

### 公開リポジトリ `data/`
| ファイル | 内容 |
|---|---|
| `raw_articles.csv` | 全記事(本文を除く) |
| `event_summary.csv` | 出来事(event_id)ごとの集計。並び順は開始日→event_id |
| `daily_panel.csv` | 日次パネル(jpyc-panel のオンチェーンデータと date 列の型・粒度をそろえている) |
| `recent_events.json` | 直近30日に収集された記事を含む出来事の一覧(デイリーニュースダッシュボード表示用、本文なし) |

### 主な列
- `relevance`: JPYCがその記事の**主題としてどれだけ中心的か**(0〜100)。ニュースの重要度ではない
- `event_id`: 同じ出来事を報じた記事のまとまり。直近14日以内に始まった出来事を候補として示し、同じ出来事か新しい出来事かを Haiku が判定する。relevance 30未満の記事には付かない
- `collected_at`: 記事を収集(保存)した日時(JST)。`collected_at_source` はその値の出どころ
  - `runtime`: 収集時に記録した値
  - `git_commit`: 2026-10-01 の導入時に、非公開リポジトリの git 履歴(その記事が初めて現れたコミットの日時)から復元した値。実際の取得の数分後の時刻
  - `unknown`: 初期一括取り込み(2026-07-10)の記事。取り込み前の収集日時の記録がないため空欄

## 研究データとしての約束

分類の定義(タグの種類、relevance の定義、Haiku へのプロンプト、event_id の判定方法)は変更しない。変更が必要な場合は全件の再分類を伴うため、別途判断する。

## ローカルでの動作確認(研究データを書き換えない)

各スクリプトは読み書きするデータの場所を環境変数で切り替えられる。データを一時フォルダにコピーして、そこを指して実行する。

```bash
TMP=$(mktemp -d); mkdir -p $TMP/data-repo $TMP/public
cp ../jpyc-news-data/raw_articles_full.csv $TMP/data-repo/
export FULL_DATA_PATH=$TMP/data-repo/raw_articles_full.csv PUBLIC_DATA_DIR=$TMP/public

LOOKBACK_DAYS=3 python pipeline/collector.py     # 収集(ネットワークは使う。対応表・失敗記録も一時フォルダに作られる)
DRY_RUN_NO_API=1 python pipeline/classifier.py   # Haikuを呼ばず、分類対象と渡す候補だけを表示(保存もしない)
python pipeline/panel_builder.py                 # 公開用データを一時フォルダに生成
```

収集日時の復元スクリプトも、`OUTPUT_PATH` を指定すると別ファイルに書き出せる。

```bash
DATA_REPO_PATH=../jpyc-news-data OUTPUT_PATH=$TMP/backfilled.csv python pipeline/backfill_collected_at.py
```

## 運用履歴

| 日付 | 内容 |
|---|---|
| 2026-07-10 | 本パイプラインを構築。過去分1,225件を一括取り込み。毎週月曜 9:00 JST の週次実行を開始 |
| 2026-07-26 | EVT-20260713-01 に、ローソンの決済実証(7/13の実行で作成)とゴルフ練習場の決済導入(7/20の実行で作成)が混ざっていたのを手で分割(ゴルフ=-01、ローソン=-04)。当時は Haiku の判定ミスと記録したが、**原因は event_id の番号の不具合**(下記 2026-10-01)と判明した |
| 2026-09-21〜28 | googlenewsdecoder 0.2.1 で戻り値の成功フラグが `status` → `success` に変わり、URL変換が全件失敗扱いになって新規記事が0件のまま「成功」で終わっていた |
| 2026-10-01 | 上記を修正(両方の形式に対応、バージョンを `>=0.1.7,<0.3` に固定、全件失敗なら異常終了)。`lookback_days=21` で再実行し、9/10〜10/1 の74件を回収 |
| 2026-10-01 | **毎日実行(21:10 JST)に切り替え。** 検索期間を毎日3日・月曜14日に変更。URL変換の対応表と本文取得の失敗記録を導入。`collected_at` / `collected_at_source` 列を追加し、既存記事は git 履歴から復元(復元446件、初期一括の1,225件は unknown)。`recent_events.json` の出力を追加 |
| 2026-10-01 | **event_id の番号の不具合を修正。** 通し番号を実行のたびに01から数え直していたため、前の実行で作られた出来事と同じ番号が、後の実行で別の出来事に振られることがあった。保存済みデータの同じ日付の最大番号の続きから振るように変更(判定方法は変更なし) |
| 2026-10-01 | `event_summary.csv` の並び順を「開始日→event_id」で固定(以前は同じ開始日の出来事の順番が実行ごとに入れ替わっていた。中身は変更なし) |
