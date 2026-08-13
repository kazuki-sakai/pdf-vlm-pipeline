import importlib.util
import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


MODULE_PATH = (
    Path(__file__).parents[1] / "src" / "pdf_vlm_pipeline" / "ocr_batch.py"
)
SPEC = importlib.util.spec_from_file_location("ocr_batch", MODULE_PATH)
assert SPEC and SPEC.loader
ocr_batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ocr_batch
SPEC.loader.exec_module(ocr_batch)


class OcrBatchTests(unittest.TestCase):
    @staticmethod
    def make_valid_artifact(artifact: Path, digest: str) -> None:
        page = artifact / "raw" / "page-0001"
        merged = artifact / "merged"
        page.mkdir(parents=True)
        merged.mkdir(parents=True)
        (artifact / "original.pdf").write_bytes(b"%PDF-test")
        (artifact / "manifest.json").write_text(
            json.dumps({"source_sha256": digest, "page_count": 1}),
            encoding="utf-8",
        )
        (page / "page.md").write_text("page\n", encoding="utf-8")
        (page / "page_res.json").write_text("{}", encoding="utf-8")
        (merged / "document.md").write_text("document\n", encoding="utf-8")
        (merged / "document_res.json").write_text("{}", encoding="utf-8")

    def test_discover_skips_complete_quarantined_and_duplicate(self) -> None:
        with TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            inbox = tmp_path / "inbox"
            artifacts = tmp_path / "artifacts"
            failures = tmp_path / "failures"
            for directory in (inbox, artifacts, failures):
                directory.mkdir()

            complete_pdf = inbox / "complete.pdf"
            complete_pdf.write_bytes(b"%PDF-complete")
            complete_hash = ocr_batch.sha256_file(complete_pdf)
            (artifacts / complete_hash).mkdir()
            (artifacts / complete_hash / ".complete").touch()

            failed_pdf = inbox / "failed.pdf"
            failed_pdf.write_bytes(b"%PDF-failed")
            failed_hash = ocr_batch.sha256_file(failed_pdf)
            (failures / failed_hash).mkdir()
            (failures / failed_hash / "latest.json").write_text(
                "{}", encoding="utf-8"
            )

            pending_pdf = inbox / "pending.PDF"
            pending_pdf.write_bytes(b"%PDF-pending")
            (inbox / "pending-copy.pdf").write_bytes(b"%PDF-pending")

            summary = ocr_batch.BatchSummary()
            work = ocr_batch.discover_work(
                inbox=inbox,
                artifact_root=artifacts,
                failure_root=failures,
                min_age_seconds=0,
                retry_failed=False,
                summary=summary,
            )

            self.assertEqual([item.source.name for item in work], ["pending-copy.pdf"])
            self.assertEqual(summary.discovered, 4)
            self.assertEqual(summary.already_complete, 1)
            self.assertEqual(summary.quarantined, 1)
            self.assertEqual(summary.duplicate, 1)
            self.assertEqual(summary.pending, 1)

    def test_validate_artifact_with_page_scoped_images(self) -> None:
        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            page = artifact / "raw" / "page-0001"
            merged = artifact / "merged"
            (page / "imgs").mkdir(parents=True)
            (merged / "imgs").mkdir(parents=True)

            original = artifact / "original.pdf"
            original.write_bytes(b"%PDF-test")
            digest = ocr_batch.sha256_file(original)
            (artifact / "manifest.json").write_text(
                json.dumps({"source_sha256": digest, "page_count": 1}),
                encoding="utf-8",
            )

            (page / "page.md").write_text("![](imgs/raw.jpg)\n", encoding="utf-8")
            (page / "page_res.json").write_text("{}", encoding="utf-8")
            (page / "imgs" / "raw.jpg").write_bytes(b"raw-image")
            (merged / "document.md").write_text(
                '<img src="imgs/merged.jpg">\n', encoding="utf-8"
            )
            (merged / "document_res.json").write_text("{}", encoding="utf-8")
            (merged / "imgs" / "merged.jpg").write_bytes(b"merged-image")
            (merged / "imgs" / "decorative.jpg").write_bytes(b"header-image")

            counts = ocr_batch.validate_artifact(artifact)

            self.assertEqual(counts["raw_markdown_files"], 1)
            self.assertEqual(counts["image_references"], 2)
            self.assertEqual(counts["image_files"], 3)
            self.assertEqual(counts["unreferenced_image_files"], 1)

    def test_recover_quarantined_artifact_without_ocr(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_root = root / "artifacts"
            failure_root = root / "failures"
            artifact_root.mkdir()

            digest = hashlib.sha256(b"%PDF-test").hexdigest()
            attempt = failure_root / digest / "attempt-test"
            self.make_valid_artifact(attempt, digest)
            (attempt / "failure.json").write_text("{}", encoding="utf-8")

            summary = ocr_batch.BatchSummary()
            ocr_batch.recover_quarantined_artifacts(
                artifact_root=artifact_root,
                failure_root=failure_root,
                summary=summary,
            )

            recovered = artifact_root / digest
            self.assertEqual(summary.recovered, 1)
            self.assertTrue((recovered / ".complete").is_file())
            self.assertTrue((recovered / "previous-failure.json").is_file())
            self.assertFalse(attempt.exists())


if __name__ == "__main__":
    unittest.main()
