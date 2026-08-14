# pdf-vlm-pipeline

PDFをPaddleOCR-VLでMarkdown・JSON・画像へ変換し、VLMとの対話に利用するためのパイプラインです。

現在の実装範囲は、OpenPBS上で`inbox/`内の未処理PDFを変換するOCRバッチです。

## OCRバッチ

既定の配置は次のとおりです。

```text
~/local/pdf-vlm/
├── inbox/
├── artifacts/<sha256>/
├── failures/<sha256>/
├── state/
└── cache/
```

PBSジョブはリポジトリのルートから投入します。

```bash
qsub scripts/pbs/ocr-batch.pbs
```

既定値を変更する場合は`qsub -v`で渡します。

```bash
qsub -v \
PDF_VLM_DATA_ROOT=/path/to/data,PDF_VLM_OCR_SIF=/path/to/paddleocr.sif \
scripts/pbs/ocr-batch.pbs
```

処理済み判定にはPDF内容のSHA-256を使用します。同じ内容のPDFはファイル名が異なっても再処理しません。失敗したPDFは隔離され、他のPDFの処理を妨げません。再試行する場合は次のように指定します。

```bash
qsub -v \
PDF_VLM_DATA_ROOT=/path/to/data,PDF_VLM_RETRY_FAILED=1 \
scripts/pbs/ocr-batch.pbs
```

検証条件の更新後など、隔離済みの出力をOCRなしで再検証して復旧する場合は次のように指定します。

```bash
qsub -v PDF_VLM_RECOVER_QUARANTINED=1 scripts/pbs/ocr-batch.pbs
```

## vLLMコンテナ

Qwen VLM用の推論サーバにはvLLM 0.19.1を使用します。公式コンテナをSIFへ変換するジョブは、リポジトリのルートから投入します。

```bash
qsub scripts/pbs/vllm-pull.pbs
```

既定の出力先は次のとおりです。

```text
~/local/containers/vllm-openai-0.19.1.sif
```

SIF作成後、モデルをロードせずにGPU・CUDA・vLLMの基本動作を確認します。

```bash
qsub scripts/pbs/vllm-gpu-probe.pbs
```

基本動作の確認後、Qwen3.6-35B-A3B-FP8を共有ホームへダウンロードします。

```bash
qsub scripts/pbs/qwen-download.pbs
```

既定の保存先は次のとおりです。中断した場合は同じジョブを再投入すると、ダウンロード済みファイルを利用して再開します。

```text
~/local/pdf-vlm/models/Qwen3.6-35B-A3B-FP8
```

ダウンロード後、RTX 3090を2枚使ってvLLMを一時起動し、テキスト入力と画像入力を検査します。検査後、サーバーは自動終了します。

```bash
qsub scripts/pbs/qwen-server-probe.pbs
```

OpenPBSがUUID形式で指定したGPUは、割当cgroup内のローカル番号へ変換してからvLLMへ渡します。

## 48時間vLLMサーバー

検査完了後、本番用サーバーを投入します。このジョブは最初に`inbox/`のOCRバッチを実行し、その終了後に同じGPU割当のままvLLMを起動します。未処理PDFがなければPaddleOCR-VLモデルはロードされません。

```bash
qsub scripts/pbs/qwen-server.pbs
```

OCRを明示的に省略する場合だけ、次のように指定します。

```bash
qsub -v PDF_VLM_RUN_OCR=0 scripts/pbs/qwen-server.pbs
```

サーバーはRTX 3090を2枚使用して最大48時間稼働します。同じデータ領域に対する二重起動はロックで防止します。接続情報は次のファイルに保存されます。

```text
~/local/pdf-vlm/state/vllm-server.json
```

APIキーは初回起動時に生成され、次の所有者専用ファイルに保存されます。

```text
~/local/pdf-vlm/secrets/vllm-api-key
```

`arcturus`から状態・認証・モデル一覧・日本語応答をまとめて確認します。

```bash
python3 scripts/qwen-api-probe.py
```

## 対話セッション

通常はセッションランチャーを使用します。認証済みサーバーが稼働中ならそのジョブを再利用し、なければ統合PBSジョブを1件だけ投入します。OCRとvLLMの準備完了後、ターミナルCLIまたはWeb UIを対話的に選択します。

```bash
python3 scripts/pdf-vlm-session.py
```

選択を省略して直接起動することもできます。

```bash
python3 scripts/pdf-vlm-session.py --interface terminal
python3 scripts/pdf-vlm-session.py --interface web
```

Markdown添付の既定上限は48 KiBです。必要に応じて上限を変更できますが、モデルの16,384トークンのコンテキスト上限は超えられません。

```bash
python3 scripts/pdf-vlm-session.py --interface web --max-text-bytes 65536
python3 scripts/pdf-vlm-session.py --interface terminal --max-text-bytes 65536
```

Web UIは`arcturus`のループバックアドレスだけで待ち受けます。Web UIを選択して起動したまま、ローカルPCの別ターミナルでSSHトンネルを作成します。

```bash
ssh -N -L 18766:127.0.0.1:18766 arcturus
```

その後、ローカルPCのブラウザで`http://127.0.0.1:18766`を開きます。Web UIの`▤`ボタンから、共有ホームにある完成済みartifactの結合Markdown・画像を直接添付できます。結合Markdownが上限を超える場合は、OCRのページ別Markdown、またはページ別Markdownと同じページの画像1枚をまとめて添付できます。`＋`ボタンはローカルPC上のファイルを添付します。Web UIを`Ctrl-C`で終了してもPBS上のvLLMサーバーは停止しません。

処理済み資料の「論文全体を階層的に要約」は、ページごとのMarkdownと代表画像1枚を独立に研究ノートへ圧縮し、必要に応じて中間統合してから全体要約を生成します。結果は`~/local/pdf-vlm/summaries/<sha256>/`へ保存され、同じモデル・設定での再実行時に再利用されます。完了後は圧縮済み統合ノートと最終要約が新しい会話履歴になり、全文をコンテキストへ保持せずに追加質問できます。

サーバーの準備だけを行い、対話CLIを起動しない場合は次のように指定します。

```bash
python3 scripts/pdf-vlm-session.py --no-chat
```

すでに稼働しているサーバーへ直接接続する場合は、対話CLI単体も使用できます。

```bash
python3 scripts/qwen-chat.py
```

対話中に画像またはMarkdownなどのUTF-8文書を次の発言へ添付できます。

```text
you> /attach ~/local/pdf-vlm/artifacts/<sha256>/merged/original.md
you> この論文の新規性を簡潔に説明してください。
```

画像を添付する場合も同じ`/attach`を使います。本番サーバーの設定に合わせ、1回の発言へ添付できる画像は1枚です。`/help`で全コマンドを表示できます。
