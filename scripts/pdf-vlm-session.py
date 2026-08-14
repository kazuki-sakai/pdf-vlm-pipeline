#!/usr/bin/env python3
"""Ensure the PBS vLLM service is ready, then open a selected user interface."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


DIRECT_OPENER = build_opener(ProxyHandler({}))
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def authenticated_models(base_url: str, api_key: str) -> list[str]:
    request = Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with DIRECT_OPENER.open(request, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise RuntimeError(f"unexpected /models response: {value!r}")
    return [
        str(item["id"])
        for item in value["data"]
        if isinstance(item, dict) and "id" in item
    ]


def probe_server(state_path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        state = read_json(state_path)
        if state.get("status") != "ready":
            return None, f"state is {state.get('status')!r}"

        health_request = Request(str(state["health_url"]), method="GET")
        with DIRECT_OPENER.open(health_request, timeout=5) as response:
            if response.status != 200:
                return None, f"health returned HTTP {response.status}"

        api_key_path = Path(str(state["api_key_file"])).expanduser()
        api_key = api_key_path.read_text(encoding="utf-8").strip()
        if not api_key:
            return None, f"API key file is empty: {api_key_path}"
        model_name = str(state["served_model_name"])
        model_ids = authenticated_models(str(state["base_url"]), api_key)
        if model_name not in model_ids:
            return None, f"model {model_name!r} is absent from /models"
        return state, "ready"
    except FileNotFoundError:
        return None, f"state file is absent: {state_path}"
    except (OSError, ValueError, KeyError, RuntimeError, HTTPError, URLError) as exc:
        return None, str(exc)


def qstat_state(job_id: str) -> str | None:
    result = subprocess.run(
        ["qstat", "-f", job_id],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return None
    match = re.search(r"^\s*job_state\s*=\s*(\S+)", result.stdout, re.MULTILINE)
    return match.group(1) if match else "active"


def submission_job_id(path: Path) -> str | None:
    try:
        value = read_json(path)
        job_id = value.get("pbs_job_id")
        return str(job_id) if job_id else None
    except (OSError, ValueError, RuntimeError):
        return None


def state_job_id(path: Path) -> str | None:
    try:
        value = read_json(path)
        job_id = value.get("pbs_job_id")
        return str(job_id) if job_id else None
    except (OSError, ValueError, RuntimeError):
        return None


def submit_server(
    *, project_root: Path, data_root: Path, submission_path: Path
) -> str:
    server_script = project_root / "scripts" / "pbs" / "qwen-server.pbs"
    if not server_script.is_file():
        raise RuntimeError(f"server PBS script is missing: {server_script}")
    result = subprocess.run(
        [
            "qsub",
            "-v",
            f"PDF_VLM_DATA_ROOT={data_root}",
            str(server_script),
        ],
        cwd=project_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"qsub failed: {detail}")
    job_id = result.stdout.strip().splitlines()[0].strip()
    if not job_id or not JOB_ID_RE.fullmatch(job_id):
        raise RuntimeError(f"qsub returned an unexpected job ID: {job_id!r}")
    atomic_write_json(
        submission_path,
        {
            "schema_version": 1,
            "pbs_job_id": job_id,
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "project_root": str(project_root),
            "data_root": str(data_root),
        },
    )
    return job_id


def find_or_submit_job(
    *,
    project_root: Path,
    data_root: Path,
    state_path: Path,
    submission_path: Path,
    session_lock_path: Path,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Return (ready state, active/submitted job ID, was newly submitted)."""
    with exclusive_lock(session_lock_path):
        state, _ = probe_server(state_path)
        if state is not None:
            return state, str(state.get("pbs_job_id") or ""), False

        candidates = [submission_job_id(submission_path), state_job_id(state_path)]
        for job_id in candidates:
            if job_id and qstat_state(job_id) is not None:
                return None, job_id, False

        submission_path.unlink(missing_ok=True)
        job_id = submit_server(
            project_root=project_root,
            data_root=data_root,
            submission_path=submission_path,
        )
        return None, job_id, True


def server_log_path(data_root: Path, job_id: str) -> Path:
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    return data_root / "state" / f"vllm-server-{safe_job_id}.log"


def wait_until_ready(
    *,
    state_path: Path,
    submission_path: Path,
    data_root: Path,
    job_id: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_report = 0.0
    last_pbs_state: str | None = None
    last_reason = ""

    while True:
        state, reason = probe_server(state_path)
        if state is not None:
            recorded_job = submission_job_id(submission_path)
            if recorded_job == job_id:
                submission_path.unlink(missing_ok=True)
            return state

        pbs_state = qstat_state(job_id)
        if pbs_state is None:
            log_path = server_log_path(data_root, job_id)
            raise RuntimeError(
                f"PBS job {job_id} ended before the server became ready; "
                f"last check: {reason}; server log: {log_path}"
            )

        now = time.monotonic()
        if (
            pbs_state != last_pbs_state
            or reason != last_reason
            or now - last_report >= 60
        ):
            print(f"Waiting: PBS job {job_id} state={pbs_state}; {reason}", flush=True)
            last_report = now
            last_pbs_state = pbs_state
            last_reason = reason

        if now >= deadline:
            raise RuntimeError(
                f"timed out after {timeout_seconds:.0f}s waiting for PBS job {job_id}"
            )
        time.sleep(poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path.home() / "local" / "pdf-vlm",
        help="Shared data root (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=24 * 60 * 60,
        help="Maximum wait for queued job, OCR, and vLLM startup (default: 86400)",
    )
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument(
        "--interface",
        choices=("ask", "terminal", "web", "none"),
        default="ask",
        help="User interface to open after startup (default: ask)",
    )
    parser.add_argument("--web-port", type=int, default=18766)
    parser.add_argument(
        "--no-chat",
        action="store_true",
        help="Deprecated alias for --interface none",
    )
    return parser


def select_interface(configured: str, *, no_chat: bool) -> str:
    if no_chat or configured == "none":
        return "none"
    if configured != "ask":
        return configured
    if not sys.stdin.isatty():
        return "terminal"

    print("Select an interface:")
    print("  1) Terminal CLI")
    print("  2) Web UI (SSH tunnel)")
    print("  3) Server only")
    while True:
        try:
            choice = input("Choice [1]: ").strip().lower()
        except EOFError:
            return "terminal"
        if choice in {"", "1", "terminal", "cli"}:
            return "terminal"
        if choice in {"2", "web", "webui"}:
            return "web"
        if choice in {"3", "none", "server"}:
            return "none"
        print("Please enter 1, 2, or 3.")


def run(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        raise RuntimeError("timeout and poll intervals must be positive")
    if args.max_tokens <= 0:
        raise RuntimeError("--max-tokens must be positive")
    if not 1 <= args.web_port <= 65535:
        raise RuntimeError("--web-port must be between 1 and 65535")

    project_root = Path(__file__).resolve().parents[1]
    data_root = args.data_root.expanduser().resolve()
    state_root = data_root / "state"
    state_path = state_root / "vllm-server.json"
    submission_path = state_root / "vllm-submission.json"
    session_lock_path = state_root / "vllm-session.lock"

    state, job_id, submitted = find_or_submit_job(
        project_root=project_root,
        data_root=data_root,
        state_path=state_path,
        submission_path=submission_path,
        session_lock_path=session_lock_path,
    )
    if state is None:
        assert job_id
        action = "Submitted" if submitted else "Reusing queued/running"
        print(f"{action} PBS job: {job_id}")
        state = wait_until_ready(
            state_path=state_path,
            submission_path=submission_path,
            data_root=data_root,
            job_id=job_id,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    else:
        print(f"Reusing ready PBS job: {state.get('pbs_job_id')}")

    print(f"vLLM ready: {state['base_url']} ({state['served_model_name']})")
    interface = select_interface(args.interface, no_chat=args.no_chat)
    if interface == "none":
        return 0

    if interface == "terminal":
        client_script = project_root / "scripts" / "qwen-chat.py"
        client_arguments = [
            sys.executable,
            str(client_script),
            "--data-root",
            str(data_root),
            "--max-tokens",
            str(args.max_tokens),
        ]
    else:
        client_script = project_root / "scripts" / "qwen-webui.py"
        client_arguments = [
            sys.executable,
            str(client_script),
            "--data-root",
            str(data_root),
            "--port",
            str(args.web_port),
            "--max-tokens",
            str(args.max_tokens),
        ]
    result = subprocess.run(
        client_arguments,
        cwd=project_root,
        check=False,
    )
    return result.returncode


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
