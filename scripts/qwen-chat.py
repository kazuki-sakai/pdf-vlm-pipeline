#!/usr/bin/env python3
"""Interactive terminal client for the PBS-hosted Qwen vLLM server."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import mimetypes
import shlex
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


DIRECT_OPENER = build_opener(ProxyHandler({}))
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
DEFAULT_MAX_TEXT_BYTES = 48 * 1024
DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def request_json(
    url: str,
    *,
    api_key: str,
    body: dict[str, Any],
    timeout: float = 600,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with DIRECT_OPENER.open(request, timeout=timeout) as response:
            value = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected API response: {value!r}")
    return value


def classify_attachment(path: Path) -> tuple[str, str]:
    mime_type = mimetypes.guess_type(path.name)[0] or ""
    if mime_type in IMAGE_MIME_TYPES:
        return "image", mime_type
    return "text", mime_type or "text/plain"


def parse_attachment_path(argument: str) -> Path:
    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise RuntimeError(f"invalid attachment path: {exc}") from exc
    if len(parts) != 1:
        raise RuntimeError("usage: /attach PATH")
    path = Path(parts[0]).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"attachment is not a readable file: {path}")
    return path


def image_content(path: Path, mime_type: str, max_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_bytes:
        raise RuntimeError(
            f"image is too large ({size} bytes; limit {max_bytes}): {path}"
        )
    data_url = (
        f"data:{mime_type};base64,"
        + base64.b64encode(path.read_bytes()).decode("ascii")
    )
    return {"type": "image_url", "image_url": {"url": data_url}}


def text_content(path: Path, remaining_bytes: int) -> tuple[str, int]:
    size = path.stat().st_size
    if size > remaining_bytes:
        raise RuntimeError(
            f"text attachments exceed the per-message limit "
            f"({size} bytes needed, {remaining_bytes} available): {path}"
        )
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise RuntimeError(f"unsupported binary attachment: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"text attachment is not UTF-8: {path}") from exc
    return f"--- 添付文書: {path.name} ---\n{text}\n--- 添付文書ここまで ---", size


def strip_images(messages: list[dict[str, Any]]) -> None:
    """Remove an older image before introducing a new one to the request."""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        message["content"] = [
            item
            for item in content
            if not (isinstance(item, dict) and item.get("type") == "image_url")
        ]


def make_user_content(
    prompt: str,
    attachments: list[Path],
    *,
    max_text_bytes: int,
    max_image_bytes: int,
) -> tuple[str | list[dict[str, Any]], bool]:
    if not attachments:
        return prompt, False

    content: list[dict[str, Any]] = []
    text_bytes = 0
    has_image = False
    for path in attachments:
        kind, mime_type = classify_attachment(path)
        if kind == "image":
            if has_image:
                raise RuntimeError("only one image can be attached to each message")
            content.append(image_content(path, mime_type, max_image_bytes))
            has_image = True
        else:
            attachment_text, size = text_content(path, max_text_bytes - text_bytes)
            content.append({"type": "text", "text": attachment_text})
            text_bytes += size
    content.append({"type": "text", "text": prompt})
    return content, has_image


def print_help() -> None:
    print(
        """Commands:
  /attach PATH  Attach one image or a UTF-8 text/Markdown file to the next message
  /files        Show attachments queued for the next message
  /detach       Remove all queued attachments
  /clear        Clear conversation history and queued attachments
  /status       Show the server and conversation status
  /help         Show this help
  /quit         Exit the client

One image may be active in a conversation. Attaching a new image replaces the
older image after the next message is sent. Text attachments are limited to
the configured byte limit; use /clear if the conversation becomes too long."""
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path.home() / "local" / "pdf-vlm",
        help="Data root containing state/vllm-server.json (default: %(default)s)",
    )
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--max-text-bytes", type=int, default=DEFAULT_MAX_TEXT_BYTES)
    parser.add_argument("--max-image-bytes", type=int, default=DEFAULT_MAX_IMAGE_BYTES)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.max_tokens <= 0:
        raise RuntimeError("--max-tokens must be positive")
    if args.max_text_bytes <= 0:
        raise RuntimeError("--max-text-bytes must be positive")
    if args.max_image_bytes <= 0:
        raise RuntimeError("--max-image-bytes must be positive")

    data_root = args.data_root.expanduser().resolve()
    state_path = data_root / "state" / "vllm-server.json"
    state = read_json(state_path)
    if state.get("status") != "ready":
        raise RuntimeError(
            f"vLLM state is not ready: {state.get('status')!r} ({state_path})"
        )

    base_url = str(state["base_url"]).rstrip("/")
    model_name = str(state["served_model_name"])
    api_key_path = Path(str(state["api_key_file"])).expanduser()
    api_key = api_key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError(f"API key file is empty: {api_key_path}")

    messages: list[dict[str, Any]] = []
    pending: list[Path] = []

    print(f"Connected configuration: {model_name} at {base_url}")
    print("Type /help for commands. Type /quit to exit.")

    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\nInterrupted. Type /quit to exit.")
            continue

        if not line:
            continue
        command, _, argument = line.partition(" ")
        if command in {"/quit", "/exit"}:
            break
        if command == "/help":
            print_help()
            continue
        if command == "/attach":
            try:
                path = parse_attachment_path(argument)
                kind, _ = classify_attachment(path)
                if kind == "image" and any(
                    classify_attachment(item)[0] == "image" for item in pending
                ):
                    raise RuntimeError("only one image can be queued per message")
                pending.append(path)
                print(f"Queued {kind}: {path}")
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
            continue
        if command == "/files":
            if pending:
                for path in pending:
                    print(f"- {classify_attachment(path)[0]}: {path}")
            else:
                print("No attachments queued.")
            continue
        if command == "/detach":
            pending.clear()
            print("Queued attachments cleared.")
            continue
        if command == "/clear":
            messages.clear()
            pending.clear()
            print("Conversation and queued attachments cleared.")
            continue
        if command == "/status":
            print(f"PBS job: {state.get('pbs_job_id')}")
            print(f"Model: {model_name}")
            print(f"Messages in history: {len(messages)}")
            print(f"Queued attachments: {len(pending)}")
            continue
        if command.startswith("/"):
            print(f"ERROR: unknown command: {command} (try /help)", file=sys.stderr)
            continue

        try:
            user_content, has_new_image = make_user_content(
                line,
                pending,
                max_text_bytes=args.max_text_bytes,
                max_image_bytes=args.max_image_bytes,
            )
            request_history = copy.deepcopy(messages)
            if has_new_image:
                strip_images(request_history)
            user_message = {"role": "user", "content": user_content}
            request_messages = [*request_history, user_message]
            result = request_json(
                f"{base_url}/chat/completions",
                api_key=api_key,
                body={
                    "model": model_name,
                    "messages": request_messages,
                    "max_tokens": args.max_tokens,
                    "temperature": 0.7,
                    "top_p": 0.8,
                    "top_k": 20,
                    "presence_penalty": 1.5,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            response_message = result["choices"][0]["message"]
            if not isinstance(response_message, dict):
                raise RuntimeError(f"unexpected chat message: {response_message!r}")
            answer = (
                response_message.get("content")
                or response_message.get("reasoning_content")
                or ""
            )
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError(f"chat response is empty: {result}")
        except (OSError, ValueError, KeyError, RuntimeError, URLError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print("The message was not added to history; queued files were retained.")
            continue

        if has_new_image:
            strip_images(messages)
        messages.extend(
            [
                user_message,
                {"role": "assistant", "content": answer.strip()},
            ]
        )
        pending.clear()
        print(f"qwen> {answer.strip()}")

    print("Bye.")
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, ValueError, KeyError, RuntimeError, HTTPError, URLError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
