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
