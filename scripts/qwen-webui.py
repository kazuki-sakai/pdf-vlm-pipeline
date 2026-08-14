#!/usr/bin/env python3
"""Loopback-only Web UI proxy for the PBS-hosted Qwen vLLM server."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


DIRECT_OPENER = build_opener(ProxyHandler({}))
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_SUFFIX_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
PAGE_DIRECTORY_RE = re.compile(r"^page-(\d{4})$")
MAX_REQUEST_BYTES = 30 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
DEFAULT_MAX_TEXT_ATTACHMENT_BYTES = 48 * 1024


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def load_server_configuration(data_root: Path) -> dict[str, Any]:
    state_path = data_root / "state" / "vllm-server.json"
    state = read_json(state_path)
    if state.get("status") != "ready":
        raise RuntimeError(
            f"vLLM state is not ready: {state.get('status')!r} ({state_path})"
        )
    api_key_path = Path(str(state["api_key_file"])).expanduser()
    api_key = api_key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError(f"API key file is empty: {api_key_path}")
    return {
        "base_url": str(state["base_url"]).rstrip("/"),
        "health_url": str(state["health_url"]),
        "model": str(state["served_model_name"]),
        "pbs_job_id": state.get("pbs_job_id"),
        "api_key": api_key,
    }


def list_artifacts(data_root: Path) -> list[dict[str, Any]]:
    artifact_root = data_root / "artifacts"
    if not artifact_root.is_dir():
        return []

    artifacts: list[dict[str, Any]] = []
    for artifact in artifact_root.iterdir():
        if (
            not artifact.is_dir()
            or not DIGEST_RE.fullmatch(artifact.name)
            or not (artifact / ".complete").is_file()
        ):
            continue
        merged = artifact / "merged"
        raw = artifact / "raw"
        markdown_files = sorted(path for path in merged.glob("*.md") if path.is_file())
        image_files = sorted(
            path
            for path in merged.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIX_TO_MIME
        )
        pages: list[dict[str, Any]] = []
        for page_directory in sorted(path for path in raw.glob("page-*") if path.is_dir()):
            page_match = PAGE_DIRECTORY_RE.fullmatch(page_directory.name)
            if page_match is None:
                continue
            page_markdown = sorted(
                path for path in page_directory.glob("*.md") if path.is_file()
            )
            page_images = sorted(
                path
                for path in page_directory.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIX_TO_MIME
            )
            pages.append(
                {
                    "number": int(page_match.group(1)),
                    "markdown": [
                        {
                            "path": str(path.relative_to(artifact)),
                            "name": path.name,
                            "size": path.stat().st_size,
                        }
                        for path in page_markdown
                    ],
                    "images": [
                        {
                            "path": str(path.relative_to(artifact)),
                            "name": path.name,
                            "size": path.stat().st_size,
                        }
                        for path in page_images
                    ],
                }
            )
        try:
            manifest = read_json(artifact / "manifest.json")
        except (OSError, ValueError, RuntimeError):
            manifest = {}
        title = str(manifest.get("source_filename") or artifact.name)
        artifacts.append(
            {
                "id": artifact.name,
                "title": title,
                "page_count": manifest.get("page_count"),
                "completed_at": manifest.get("completed_at"),
                "markdown": [
                    {
                        "path": str(path.relative_to(artifact)),
                        "name": path.name,
                        "size": path.stat().st_size,
                    }
                    for path in markdown_files
                ],
                "images": [
                    {
                        "path": str(path.relative_to(artifact)),
                        "name": path.name,
                        "size": path.stat().st_size,
                    }
                    for path in image_files
                ],
                "pages": pages,
            }
        )
    return sorted(artifacts, key=lambda item: item["title"].casefold())


def load_artifact_attachment(
    data_root: Path,
    document_id: str,
    relative_path: str,
    max_text_attachment_bytes: int = DEFAULT_MAX_TEXT_ATTACHMENT_BYTES,
) -> dict[str, Any]:
    if not DIGEST_RE.fullmatch(document_id):
        raise RuntimeError("invalid artifact ID")
    artifact = data_root / "artifacts" / document_id
    if not (artifact / ".complete").is_file():
        raise RuntimeError("artifact is not complete or does not exist")

    merged_root = (artifact / "merged").resolve()
    raw_root = (artifact / "raw").resolve()
    target = (artifact / relative_path).resolve()
    page_number: int | None = None
    try:
        target.relative_to(merged_root)
    except ValueError:
        try:
            raw_relative = target.relative_to(raw_root)
        except ValueError as exc:
            raise RuntimeError(
                "attachment must be inside the artifact merged or raw page directory"
            ) from exc
        if not raw_relative.parts:
            raise RuntimeError("raw attachment must belong to a page directory")
        page_match = PAGE_DIRECTORY_RE.fullmatch(raw_relative.parts[0])
        if page_match is None:
            raise RuntimeError("raw attachment must belong to a page directory")
        page_number = int(page_match.group(1))
    if not target.is_file():
        raise RuntimeError("artifact attachment does not exist")

    try:
        manifest = read_json(artifact / "manifest.json")
    except (OSError, ValueError, RuntimeError):
        manifest = {}
    source_name = str(manifest.get("source_filename") or document_id[:12])
    page_label = f" · page {page_number}" if page_number is not None else ""
    display_name = f"{source_name}{page_label} · {target.name}"
    size = target.stat().st_size

    if target.suffix.lower() == ".md":
        if size > max_text_attachment_bytes:
            raise RuntimeError(
                f"Markdown exceeds the {max_text_attachment_bytes}-byte attachment limit"
            )
        raw = target.read_bytes()
        if b"\x00" in raw:
            raise RuntimeError("Markdown contains binary data")
        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Markdown is not UTF-8") from exc
        return {"kind": "text", "name": display_name, "size": size, "data": data}

    mime_type = IMAGE_SUFFIX_TO_MIME.get(target.suffix.lower())
    if mime_type is None:
        raise RuntimeError("only merged Markdown and images may be attached")
    if size > MAX_IMAGE_BYTES:
        raise RuntimeError(f"image exceeds the {MAX_IMAGE_BYTES}-byte attachment limit")
    encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    return {
        "kind": "image",
        "name": display_name,
        "size": size,
        "data": f"data:{mime_type};base64,{encoded}",
    }


def validate_data_url(url: str) -> str:
    prefix, separator, encoded = url.partition(",")
    if not separator or not prefix.startswith("data:") or not prefix.endswith(";base64"):
        raise RuntimeError("image attachment must be a base64 data URL")
    mime_type = prefix[5:-7]
    if mime_type not in IMAGE_MIME_TYPES:
        raise RuntimeError(f"unsupported image MIME type: {mime_type}")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("image attachment contains invalid base64") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"image attachment exceeds {MAX_IMAGE_BYTES} bytes"
        )
    return url


def validate_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RuntimeError("messages must be a non-empty list")
    if len(value) > 100:
        raise RuntimeError("conversation contains too many messages")

    messages: list[dict[str, Any]] = []
    image_count = 0
    text_characters = 0
    for raw_message in value:
        if not isinstance(raw_message, dict):
            raise RuntimeError("each message must be an object")
        role = raw_message.get("role")
        if role not in {"user", "assistant"}:
            raise RuntimeError(f"unsupported message role: {role!r}")
        raw_content = raw_message.get("content")
        if isinstance(raw_content, str):
            text_characters += len(raw_content)
            content: str | list[dict[str, Any]] = raw_content
        elif isinstance(raw_content, list) and raw_content:
            parts: list[dict[str, Any]] = []
            for raw_part in raw_content:
                if not isinstance(raw_part, dict):
                    raise RuntimeError("message content part must be an object")
                part_type = raw_part.get("type")
                if part_type == "text":
                    text = raw_part.get("text")
                    if not isinstance(text, str):
                        raise RuntimeError("text content must be a string")
                    text_characters += len(text)
                    parts.append({"type": "text", "text": text})
                elif part_type == "image_url":
                    image = raw_part.get("image_url")
                    if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                        raise RuntimeError("image_url content is malformed")
                    image_count += 1
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": validate_data_url(image["url"])},
                        }
                    )
                else:
                    raise RuntimeError(f"unsupported content type: {part_type!r}")
            content = parts
        else:
            raise RuntimeError("message content must be text or a non-empty list")
        messages.append({"role": role, "content": content})

    if image_count > 1:
        raise RuntimeError("the server permits only one active image per conversation")
    if text_characters > MAX_TEXT_CHARS:
        raise RuntimeError(
            f"conversation text exceeds {MAX_TEXT_CHARS} characters; clear the history"
        )
    return messages


def request_chat(
    *, base_url: str, api_key: str, model: str, messages: list[dict[str, Any]], max_tokens: int
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": 1.5,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with DIRECT_OPENER.open(request, timeout=600) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"vLLM returned HTTP {exc.code}: {detail}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected vLLM response: {result!r}")
    return result


class WebUIHandler(BaseHTTPRequestHandler):
    assets_root: Path
    data_root: Path
    configuration: dict[str, Any]
    max_tokens: int
    max_text_attachment_bytes: int

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, "application/json; charset=utf-8", payload)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        request_path = parsed.path
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if request_path == "/api/status":
            self.send_json(
                HTTPStatus.OK,
                {
                    "model": self.configuration["model"],
                    "pbs_job_id": self.configuration["pbs_job_id"],
                    "max_image_bytes": MAX_IMAGE_BYTES,
                    "max_text_attachment_bytes": self.max_text_attachment_bytes,
                    "max_tokens": self.max_tokens,
                },
            )
            return
        if request_path == "/api/artifacts":
            try:
                artifacts = list_artifacts(self.data_root)
                self.send_json(HTTPStatus.OK, {"artifacts": artifacts})
            except OSError as exc:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"artifact library unavailable: {exc}"},
                )
            return
        if request_path == "/api/attachment":
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                document_id = query.get("document", [""])[0]
                relative_path = query.get("path", [""])[0]
                attachment = load_artifact_attachment(
                    self.data_root,
                    document_id,
                    relative_path,
                    self.max_text_attachment_bytes,
                )
                self.send_json(HTTPStatus.OK, attachment)
            except (OSError, ValueError, RuntimeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        asset = assets.get(request_path)
        if asset is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        filename, content_type = asset
        try:
            payload = (self.assets_root / filename).read_bytes()
        except OSError as exc:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"asset unavailable: {exc}"},
            )
            return
        self.send_bytes(HTTPStatus.OK, content_type, payload)

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                raise RuntimeError("Content-Type must be application/json")
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise RuntimeError(
                    f"request size must be between 1 and {MAX_REQUEST_BYTES} bytes"
                )
            raw = self.rfile.read(content_length)
            request_body = json.loads(raw.decode("utf-8"))
            if not isinstance(request_body, dict):
                raise RuntimeError("request body must be an object")
            messages = validate_messages(request_body.get("messages"))
            result = request_chat(
                base_url=self.configuration["base_url"],
                api_key=self.configuration["api_key"],
                model=self.configuration["model"],
                messages=messages,
                max_tokens=self.max_tokens,
            )
            message = result["choices"][0]["message"]
            if not isinstance(message, dict):
                raise RuntimeError(f"unexpected chat message: {message!r}")
            answer = message.get("content") or message.get("reasoning_content") or ""
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("vLLM returned an empty response")
            self.send_json(
                HTTPStatus.OK,
                {"answer": answer.strip(), "usage": result.get("usage", {})},
            )
        except (OSError, ValueError, KeyError, RuntimeError, URLError) as exc:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        print(f"WebUI {self.address_string()}: {format % args}", flush=True)


class WebUIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path.home() / "local" / "pdf-vlm",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument(
        "--max-text-bytes",
        type=int,
        default=DEFAULT_MAX_TEXT_ATTACHMENT_BYTES,
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.host not in LOOPBACK_HOSTS:
        raise RuntimeError(
            "WebUI may only bind to 127.0.0.1/localhost; use an SSH tunnel"
        )
    if not 1 <= args.port <= 65535:
        raise RuntimeError("--port must be between 1 and 65535")
    if args.max_tokens <= 0:
        raise RuntimeError("--max-tokens must be positive")
    if args.max_text_bytes <= 0:
        raise RuntimeError("--max-text-bytes must be positive")

    data_root = args.data_root.expanduser().resolve()
    configuration = load_server_configuration(data_root)
    assets_root = Path(__file__).resolve().parents[1] / "web" / "qwen-webui"
    if not assets_root.is_dir():
        raise RuntimeError(f"WebUI assets are missing: {assets_root}")
    WebUIHandler.assets_root = assets_root
    WebUIHandler.data_root = data_root
    WebUIHandler.configuration = configuration
    WebUIHandler.max_tokens = args.max_tokens
    WebUIHandler.max_text_attachment_bytes = args.max_text_bytes

    server = WebUIServer((args.host, args.port), WebUIHandler)
    print(f"Web UI ready: http://{args.host}:{args.port}")
    print("From your local PC, keep a separate terminal running:")
    print(f"  ssh -N -L {args.port}:127.0.0.1:{args.port} arcturus")
    print(f"Then open http://127.0.0.1:{args.port} in your browser.")
    print("Press Ctrl-C here to stop only the Web UI; the PBS vLLM job continues.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping Web UI.")
    finally:
        server.server_close()
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
