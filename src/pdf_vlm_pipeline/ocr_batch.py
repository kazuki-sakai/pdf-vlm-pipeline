#!/usr/bin/env python3
"""Convert pending PDFs with PaddleOCR-VL and store validated artifacts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import unquote


PIPELINE_NAME = "PaddleOCR-VL-1.6"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE
)


class BatchConfigurationError(RuntimeError):
    """The batch environment is unusable."""


class DocumentChangedError(RuntimeError):
    """The source document changed while it was being processed."""


@dataclass(frozen=True)
class WorkItem:
    source: Path
    sha256: str
    size: int
    mtime_ns: int


@dataclass
class BatchSummary:
    discovered: int = 0
    pending: int = 0
    completed: int = 0
    already_complete: int = 0
    duplicate: int = 0
    quarantined: int = 0
    recovered: int = 0
    deferred: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "pending": self.pending,
            "completed": self.completed,
            "already_complete": self.already_complete,
            "duplicate": self.duplicate,
            "quarantined": self.quarantined,
            "recovered": self.recovered,
            "deferred": self.deferred,
            "failed": self.failed,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def pdf_candidates(inbox: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in inbox.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: path.name.casefold(),
    )


def looks_like_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def discover_work(
    *,
    inbox: Path,
    artifact_root: Path,
    failure_root: Path,
    min_age_seconds: float,
    retry_failed: bool,
    summary: BatchSummary,
) -> list[WorkItem]:
    now = time.time()
    work: list[WorkItem] = []
    seen_pending_hashes: set[str] = set()

    for source in pdf_candidates(inbox):
        summary.discovered += 1
        stat = source.stat()
        age = now - stat.st_mtime
        if age < min_age_seconds:
            print(
                f"DEFER unstable: {source.name} "
                f"(age={age:.1f}s, required={min_age_seconds:.1f}s)",
                flush=True,
            )
            summary.deferred += 1
            continue

        digest = sha256_file(source)
        artifact_dir = artifact_root / digest
        failure_marker = failure_root / digest / "latest.json"

        if (artifact_dir / ".complete").is_file():
            print(f"SKIP complete: {source.name} ({digest[:12]})", flush=True)
            summary.already_complete += 1
            continue

        if failure_marker.is_file() and not retry_failed:
            print(
                f"SKIP quarantined: {source.name} ({digest[:12]}); "
                "use --retry-failed to retry",
                flush=True,
            )
            summary.quarantined += 1
            continue

        if not looks_like_pdf(source):
            failure = {
                "schema_version": 1,
                "source_filename": source.name,
                "source_path": str(source),
                "source_sha256": digest,
                "failed_at": utc_now().isoformat(),
                "exception_type": "InvalidPDFHeader",
                "message": "file does not begin with %PDF-",
                "pbs_job_id": os.environ.get("PBS_JOBID"),
            }
            atomic_write_json(failure_marker, failure)
            print(f"QUARANTINED invalid PDF header: {source.name}", flush=True)
            summary.quarantined += 1
            continue

        if digest in seen_pending_hashes:
            print(
                f"SKIP duplicate pending content: {source.name} ({digest[:12]})",
                flush=True,
            )
            summary.duplicate += 1
            continue

        seen_pending_hashes.add(digest)

        work.append(
            WorkItem(
                source=source,
                sha256=digest,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )

    summary.pending = len(work)
    return work


def markdown_references(markdown_path: Path) -> list[str]:
    text = markdown_path.read_text(encoding="utf-8")
    references = MARKDOWN_IMAGE_RE.findall(text)
    references.extend(HTML_IMAGE_RE.findall(text))
    return references


def validate_artifact(artifact: Path) -> dict[str, int]:
    errors: list[str] = []
    manifest_path = artifact / "manifest.json"
    original_pdf = artifact / "original.pdf"
    raw_dir = artifact / "raw"
    merged_dir = artifact / "merged"

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - validation should aggregate errors
        errors.append(f"manifest.json cannot be parsed: {exc}")
        manifest = {}

    if not original_pdf.is_file():
        errors.append("original.pdf is missing")
    elif manifest.get("source_sha256") != sha256_file(original_pdf):
        errors.append("original.pdf SHA-256 does not match manifest")

    page_count = manifest.get("page_count")
    page_dirs = sorted(path for path in raw_dir.glob("page-*") if path.is_dir())
    raw_markdown = sorted(raw_dir.glob("page-*/*.md"))
    raw_json = sorted(raw_dir.glob("page-*/*_res.json"))
    merged_markdown = sorted(merged_dir.glob("*.md"))
    merged_json = sorted(merged_dir.glob("*_res.json"))

    if isinstance(page_count, int):
        if len(page_dirs) != page_count:
            errors.append(f"raw page directory count is {len(page_dirs)}, expected {page_count}")
        if len(raw_markdown) != page_count:
            errors.append(f"raw Markdown count is {len(raw_markdown)}, expected {page_count}")
        if len(raw_json) != page_count:
            errors.append(f"raw JSON count is {len(raw_json)}, expected {page_count}")
    else:
        errors.append("manifest page_count is not an integer")

    if len(merged_markdown) != 1:
        errors.append(f"merged Markdown count is {len(merged_markdown)}, expected 1")
    if len(merged_json) != 1:
        errors.append(f"merged JSON count is {len(merged_json)}, expected 1")

    for json_path in [*raw_json, *merged_json]:
        try:
            json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"invalid JSON {json_path.relative_to(artifact)}: {exc}")

    reference_count = 0
    referenced_images: set[Path] = set()
    for markdown_path in [*raw_markdown, *merged_markdown]:
        for raw_reference in markdown_references(markdown_path):
            reference = raw_reference.strip().split(maxsplit=1)[0]
            reference = unquote(reference.strip("<>"))
            if reference.startswith(("http://", "https://", "data:")):
                errors.append(
                    f"external image reference in {markdown_path.relative_to(artifact)}: "
                    f"{reference}"
                )
                continue

            reference_count += 1
            target = (markdown_path.parent / reference).resolve()
            try:
                target.relative_to(artifact.resolve())
            except ValueError:
                errors.append(
                    f"image reference escapes artifact: "
                    f"{markdown_path.relative_to(artifact)}: {reference}"
                )
                continue
            referenced_images.add(target)
            if not target.is_file():
                errors.append(
                    f"missing image: {markdown_path.relative_to(artifact)}: {reference}"
                )
            elif target.stat().st_size == 0:
                errors.append(
                    f"empty image: {markdown_path.relative_to(artifact)}: {reference}"
                )

    all_images = {
        path.resolve()
        for path in artifact.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    }
    # PaddleOCR-VL can save decorative/header crops that it deliberately omits
    # from Markdown.  They are useful source material and do not make the
    # artifact invalid.  Missing files referenced by Markdown remain errors.
    unreferenced = all_images - referenced_images
    if unreferenced:
        relative_paths = sorted(
            str(path.relative_to(artifact.resolve())) for path in unreferenced
        )
        print(
            "WARNING: generated image(s) not referenced by Markdown: "
            + ", ".join(relative_paths),
            flush=True,
        )

    if errors:
        raise RuntimeError("artifact validation failed:\n - " + "\n - ".join(errors))

    return {
        "raw_markdown_files": len(raw_markdown),
        "raw_json_files": len(raw_json),
        "merged_markdown_files": len(merged_markdown),
        "merged_json_files": len(merged_json),
        "image_references": reference_count,
        "image_files": len(all_images),
        "unreferenced_image_files": len(unreferenced),
    }


def recover_quarantined_artifacts(
    *, artifact_root: Path, failure_root: Path, summary: BatchSummary
) -> None:
    """Promote complete attempts that pass the current artifact validator."""

    for document_failure_root in sorted(failure_root.iterdir()):
        if not document_failure_root.is_dir():
            continue

        digest = document_failure_root.name
        artifact_dir = artifact_root / digest
        if artifact_dir.exists():
            continue

        attempts = sorted(
            (
                path
                for path in document_failure_root.glob("attempt-*")
                if path.is_dir()
            ),
            reverse=True,
        )
        for attempt_dir in attempts:
            try:
                validation = validate_artifact(attempt_dir)
            except Exception as exc:  # noqa: BLE001 - try an older attempt
                print(
                    f"RECOVERY SKIP {attempt_dir}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            manifest_path = attempt_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("source_sha256") != digest:
                print(
                    f"RECOVERY SKIP {attempt_dir}: directory hash does not match manifest",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            manifest["validation"] = validation
            manifest["recovered_at"] = utc_now().isoformat()
            manifest["recovered_from"] = str(attempt_dir)
            atomic_write_json(manifest_path, manifest)

            failure_record = attempt_dir / "failure.json"
            if failure_record.exists():
                os.replace(failure_record, attempt_dir / "previous-failure.json")

            (attempt_dir / ".complete").touch()
            os.replace(attempt_dir, artifact_dir)
            summary.recovered += 1
            print(f"RECOVERED artifact: {artifact_dir}", flush=True)
            break


def source_is_unchanged(item: WorkItem) -> bool:
    stat = item.source.stat()
    return stat.st_size == item.size and stat.st_mtime_ns == item.mtime_ns


def attempt_id() -> str:
    job_id = os.environ.get("PBS_JOBID", f"pid-{os.getpid()}").split(".", 1)[0]
    return f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{job_id}"


def quarantine_failure(
    *, item: WorkItem, work_dir: Path, failure_root: Path, exc: BaseException
) -> Path:
    document_failure_root = failure_root / item.sha256
    attempt_dir = document_failure_root / f"attempt-{attempt_id()}"
    attempt_dir.parent.mkdir(parents=True, exist_ok=True)

    failure = {
        "schema_version": 1,
        "source_filename": item.source.name,
        "source_path": str(item.source),
        "source_sha256": item.sha256,
        "failed_at": utc_now().isoformat(),
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
    }
    atomic_write_json(work_dir / "failure.json", failure)
    os.replace(work_dir, attempt_dir)
    atomic_write_json(document_failure_root / "latest.json", failure)
    return attempt_dir


def process_document(
    *,
    pipeline: Any,
    item: WorkItem,
    artifact_root: Path,
    failure_root: Path,
    paddlex_version: str,
    paddle_version: str,
) -> Path:
    job_id = os.environ.get("PBS_JOBID", f"pid-{os.getpid()}").split(".", 1)[0]
    work_dir = artifact_root / f".work-{item.sha256}-{job_id}"
    artifact_dir = artifact_root / item.sha256

    if work_dir.exists():
        raise BatchConfigurationError(f"work directory already exists: {work_dir}")
    if artifact_dir.exists():
        raise BatchConfigurationError(
            f"incomplete artifact directory already exists: {artifact_dir}"
        )

    work_dir.mkdir(parents=True)
    (work_dir / "raw").mkdir()
    (work_dir / "merged").mkdir()
    started_at = utc_now()
    started = time.monotonic()

    try:
        if not source_is_unchanged(item):
            raise DocumentChangedError("source metadata changed before processing")

        original_pdf = work_dir / "original.pdf"
        shutil.copy2(item.source, original_pdf)
        if sha256_file(original_pdf) != item.sha256 or not source_is_unchanged(item):
            raise DocumentChangedError("source changed while it was copied")

        print(f"PROCESS {item.source.name} ({item.sha256[:12]})", flush=True)
        pages = list(pipeline.predict(input=str(original_pdf)))
        if not pages:
            raise RuntimeError("PaddleOCR-VL returned no pages")

        for page_number, result in enumerate(pages, start=1):
            page_dir = work_dir / "raw" / f"page-{page_number:04d}"
            page_dir.mkdir()
            result.save_to_json(save_path=str(page_dir))
            result.save_to_markdown(save_path=str(page_dir))
            print(
                f"  saved page {page_number}/{len(pages)}: {item.source.name}",
                flush=True,
            )

        merged_results = list(
            pipeline.restructure_pages(
                pages,
                merge_tables=True,
                relevel_titles=True,
                concatenate_pages=True,
            )
        )
        if len(merged_results) != 1:
            raise RuntimeError(
                f"expected one merged result, received {len(merged_results)}"
            )
        merged_results[0].save_to_json(save_path=str(work_dir / "merged"))
        merged_results[0].save_to_markdown(save_path=str(work_dir / "merged"))

        manifest = {
            "schema_version": 1,
            "source_filename": item.source.name,
            "source_path": str(item.source),
            "source_sha256": item.sha256,
            "source_size": item.size,
            "pipeline": PIPELINE_NAME,
            "paddlex_version": paddlex_version,
            "paddlepaddle_version": paddle_version,
            "page_count": len(pages),
            "merged_result_count": len(merged_results),
            "started_at": started_at.isoformat(),
            "completed_at": utc_now().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "pbs_job_id": os.environ.get("PBS_JOBID"),
            "host": socket.gethostname(),
        }
        atomic_write_json(work_dir / "manifest.json", manifest)
        validation = validate_artifact(work_dir)
        manifest["validation"] = validation
        atomic_write_json(work_dir / "manifest.json", manifest)
        (work_dir / ".complete").touch()
        os.replace(work_dir, artifact_dir)
        print(
            f"COMPLETE {item.source.name}: {len(pages)} pages, "
            f"{manifest['elapsed_seconds']:.1f}s",
            flush=True,
        )
        return artifact_dir
    except Exception as exc:
        if isinstance(exc, DocumentChangedError):
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        failure_dir = quarantine_failure(
            item=item, work_dir=work_dir, failure_root=failure_root, exc=exc
        )
        print(f"QUARANTINED {item.source.name}: {failure_dir}", file=sys.stderr)
        raise


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchConfigurationError(
                f"another OCR batch owns the lock: {path}"
            ) from exc
        stream.seek(0)
        stream.truncate()
        stream.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "pbs_job_id": os.environ.get("PBS_JOBID"),
                    "started_at": utc_now().isoformat(),
                    "host": socket.gethostname(),
                }
            )
            + "\n"
        )
        stream.flush()
        yield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Root containing inbox/, artifacts/, failures/, and state/",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=float,
        default=30.0,
        help="Ignore PDFs modified more recently than this (default: 30)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry unchanged documents previously quarantined in failures/",
    )
    parser.add_argument(
        "--recover-quarantined",
        action="store_true",
        help="Promote quarantined attempts that pass the current validator",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    inbox = data_root / "inbox"
    artifact_root = data_root / "artifacts"
    failure_root = data_root / "failures"
    state_root = data_root / "state"
    summary = BatchSummary()

    for directory in (inbox, artifact_root, failure_root, state_root):
        directory.mkdir(parents=True, exist_ok=True)

    with exclusive_lock(state_root / "ocr.lock"):
        if args.recover_quarantined:
            recover_quarantined_artifacts(
                artifact_root=artifact_root,
                failure_root=failure_root,
                summary=summary,
            )

        work = discover_work(
            inbox=inbox,
            artifact_root=artifact_root,
            failure_root=failure_root,
            min_age_seconds=args.min_age_seconds,
            retry_failed=args.retry_failed,
            summary=summary,
        )

        if not work:
            print("No pending PDFs. PaddleOCR-VL will not be loaded.")
            print("SUMMARY " + json.dumps(summary.as_dict(), sort_keys=True))
            atomic_write_json(state_root / "last-ocr-summary.json", summary.as_dict())
            return 0

        print(f"Loading {PIPELINE_NAME} once for {len(work)} pending PDF(s)...")
        import paddle
        import paddlex
        from paddlex import create_pipeline

        pipeline = create_pipeline(pipeline=PIPELINE_NAME, device="gpu:0")

        for item in work:
            try:
                process_document(
                    pipeline=pipeline,
                    item=item,
                    artifact_root=artifact_root,
                    failure_root=failure_root,
                    paddlex_version=getattr(paddlex, "__version__", "unknown"),
                    paddle_version=paddle.__version__,
                )
                summary.completed += 1
            except DocumentChangedError as exc:
                print(f"DEFER changed: {item.source.name}: {exc}", file=sys.stderr)
                summary.deferred += 1
            except Exception as exc:  # noqa: BLE001 - continue with other documents
                print(f"FAILED {item.source.name}: {exc}", file=sys.stderr)
                summary.failed += 1

        summary_data = summary.as_dict()
        summary_data["completed_at"] = utc_now().isoformat()
        summary_data["pbs_job_id"] = os.environ.get("PBS_JOBID")
        atomic_write_json(state_root / "last-ocr-summary.json", summary_data)
        print("SUMMARY " + json.dumps(summary_data, sort_keys=True))

    # Per-document failures are quarantined and do not block a later VLM stage.
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except BatchConfigurationError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
