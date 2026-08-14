#!/usr/bin/env python3
"""Probe the running vLLM server from the PBS submission host."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


# denebola is an internal cluster host; do not route these requests through a
# proxy inherited from the login environment.
DIRECT_OPENER = build_opener(ProxyHandler({}))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def request_json(
    url: str,
    *,
    api_key: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 600,
) -> Any:
    headers: dict[str, str] = {}
    data = None
    method = "GET"
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        method = "POST"

    request = Request(url, data=data, headers=headers, method=method)
    with DIRECT_OPENER.open(request, timeout=timeout) as response:
        payload = response.read()
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def health_check(url: str) -> None:
    request = Request(url, method="GET")
    with DIRECT_OPENER.open(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"health endpoint returned HTTP {response.status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path.home() / "local" / "pdf-vlm",
        help="Data root containing state/vllm-server.json (default: %(default)s)",
    )
    parser.add_argument(
        "--prompt",
        default="日本語で一文だけ、現在正常に応答できていることを説明してください。",
        help="Text prompt used for the authenticated chat probe",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    state_path = data_root / "state" / "vllm-server.json"
    state = read_json(state_path)
    if state.get("status") != "ready":
        raise RuntimeError(
            f"vLLM state is not ready: {state.get('status')!r} ({state_path})"
        )

    base_url = str(state["base_url"]).rstrip("/")
    health_url = str(state["health_url"])
    model_name = str(state["served_model_name"])
    api_key_path = Path(str(state["api_key_file"])).expanduser()
    api_key = api_key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError(f"API key file is empty: {api_key_path}")

    print(f"State: {state_path}")
    print(f"PBS job: {state.get('pbs_job_id')}")
    print(f"Base URL: {base_url}")
    print(f"Model: {model_name}")

    health_check(health_url)
    print("Health: OK")

    models = request_json(f"{base_url}/models", api_key=api_key, timeout=30)
    if not isinstance(models, dict):
        raise RuntimeError(f"unexpected /models response: {models!r}")
    model_ids = [item.get("id") for item in models.get("data", [])]
    if model_name not in model_ids:
        raise RuntimeError(
            f"served model {model_name!r} is absent from /models: {model_ids}"
        )
    print(f"Authentication and model listing: OK ({', '.join(model_ids)})")

    result = request_json(
        f"{base_url}/chat/completions",
        api_key=api_key,
        body={
            "model": model_name,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.5,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected chat response: {result!r}")
    message = result["choices"][0]["message"]
    answer = message.get("content") or message.get("reasoning_content") or ""
    if not answer.strip():
        raise RuntimeError(f"chat response is empty: {result}")

    print("=== Response ===")
    print(answer.strip())
    print("Usage: " + json.dumps(result.get("usage", {}), sort_keys=True))
    print("Authenticated vLLM API probe: OK")
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, ValueError, KeyError, RuntimeError, HTTPError, URLError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
