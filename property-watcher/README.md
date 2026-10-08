# 新着収益物件ウォッチャー（楽待・健美家）

楽待・健美家の検索結果ページを定期的に巡回し、**前回までに見ていない物件だけ**を
Discord / Slack / LINE / メールに通知するツールです。GitHub Actions 上で動くので、PCを起動しておく必要はありません。

## 仕組み

1. `config.yaml` に書いた検索結果URLを取得
2. 物件詳細ページへのリンク（楽待 `…/{ID}/show.html`、健美家 `…/re_{ID}/`）から物件IDを抽出
3. 物件名・価格・利回り・所在地を一覧から読み取り（読めない項目は空欄）
4. `state/seen.json` に無いIDだけを新着として通知し、既読に追加

初回実行時は「今掲載されている物件」を既読登録するだけで通知しません（大量通知を防ぐため）。

## セットアップ

### 1. 検索条件を設定
楽待・健美家で希望条件（エリア・種別・価格・利回りなど）を指定して検索し、
**並び順を「新着順」にした**結果ページのURLを `config.yaml` の `searches` に貼り付けます。
何件でも追加できます。サイト側で絞り込めない条件は `filters` で指定できます。

### 2. 通知先を設定（GitHub の Settings → Secrets and variables → Actions）
使うものだけ登録してください（複数可）。

| 通知先 | 登録するSecret |
|---|---|
| Discord | `DISCORD_WEBHOOK_URL`（チャンネル設定 → 連携サービス → ウェブフック） |
| Slack | `SLACK_WEBHOOK_URL`（Incoming Webhook） |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_TO`（LINE Messaging API。LINE Notify は2025年3月で終了） |
| メール（Gmail） | `SMTP_USER`（Gmailアドレス）, `SMTP_PASSWORD`（アプリパスワード）。自分宛てに届きます。別の宛先は `MAIL_TO`、Gmail以外は `SMTP_HOST` / `SMTP_PORT` も登録 |

### 3. 動作確認
Actions タブ →「新着収益物件チェック」→ Run workflow。
`dry_run` にチェックを入れると、通知せずに取得結果をログに表示します。
取得件数が0件ならURLかサイトの構造変更を疑ってください。

以後は毎時17分（JST 7時〜23時）に自動実行されます。頻度は `.github/workflows/property-watcher.yml` の `cron` で変更できます。

## ローカルで動かす

```bash
cd property-watcher
pip install -r requirements.txt
python watcher.py --dry-run            # 取得結果を表示するだけ
DISCORD_WEBHOOK_URL=... python watcher.py
```

## 注意

- 各サイトの利用規約を確認のうえ、個人の情報収集の範囲でご利用ください。アクセス間隔は検索ごとに3秒空けています。頻度を上げすぎないでください。
- サイトのHTML構造が変わると価格などが読めなくなることがあります。物件IDはURLから取っているので、新着検知自体は比較的壊れにくい作りです。
