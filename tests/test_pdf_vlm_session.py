import importlib.util
import json
import sys
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "pdf-vlm-session.py"
SPEC = importlib.util.spec_from_file_location("pdf_vlm_session", MODULE_PATH)
assert SPEC and SPEC.loader
session = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = session
SPEC.loader.exec_module(session)


class FakeResponse(BytesIO):
    def __init__(self, payload: bytes = b"", status: int = 200) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class PdfVlmSessionTests(unittest.TestCase):
    def test_probe_ready_server_with_authentication(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_path = root / "key"
            state_path = root / "state.json"
            key_path.write_text("test-secret", encoding="utf-8")
            base_url = "http://denebola:18765/v1"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "health_url": "http://denebola:18765/health",
                        "base_url": base_url,
                        "api_key_file": str(key_path),
                        "served_model_name": "qwen-research",
                    }
                ),
                encoding="utf-8",
            )
            models = json.dumps({"data": [{"id": "qwen-research"}]}).encode()
            with patch.object(
                session.DIRECT_OPENER,
                "open",
                side_effect=[FakeResponse(), FakeResponse(models)],
            ):
                state, reason = session.probe_server(state_path)

            self.assertEqual(reason, "ready")
            self.assertIsNotNone(state)
            self.assertEqual(state["base_url"], base_url)

    def test_probe_rejects_wrong_api_key(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_path = root / "key"
            state_path = root / "state.json"
            key_path.write_text("wrong", encoding="utf-8")
            state_path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "health_url": "http://denebola:18765/health",
                        "base_url": "http://denebola:18765/v1",
                        "api_key_file": str(key_path),
                        "served_model_name": "qwen-research",
                    }
                ),
                encoding="utf-8",
            )
            unauthorized = HTTPError(
                "http://denebola:18765/v1/models",
                401,
                "Unauthorized",
                hdrs=None,
                fp=BytesIO(b"unauthorized"),
            )
            with patch.object(
                session.DIRECT_OPENER,
                "open",
                side_effect=[FakeResponse(), unauthorized],
            ):
                state, reason = session.probe_server(state_path)

            self.assertIsNone(state)
            self.assertIn("401", reason)

    def test_server_log_path_matches_pbs_script_sanitizing(self) -> None:
        path = session.server_log_path(Path("/data"), "42.server/name")
        self.assertEqual(path, Path("/data/state/vllm-server-42.server_name.log"))

    def test_submit_server_records_returned_job_id(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "project"
            data_root = root / "data"
            server_script = project_root / "scripts" / "pbs" / "qwen-server.pbs"
            submission_path = data_root / "state" / "vllm-submission.json"
            server_script.parent.mkdir(parents=True)
            server_script.write_text("#!/bin/bash\n", encoding="utf-8")
            completed = CompletedProcess(
                args=[],
                returncode=0,
                stdout="321.arcturus.example\n",
                stderr="",
            )

            with patch.object(session.subprocess, "run", return_value=completed) as run:
                job_id = session.submit_server(
                    project_root=project_root,
                    data_root=data_root,
                    submission_path=submission_path,
                )

            self.assertEqual(job_id, "321.arcturus.example")
            self.assertEqual(
                session.read_json(submission_path)["pbs_job_id"],
                "321.arcturus.example",
            )
            command = run.call_args.args[0]
            self.assertEqual(command[0], "qsub")
            self.assertIn(f"PDF_VLM_DATA_ROOT={data_root}", command)

    def test_select_interface_accepts_web_choice(self) -> None:
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = True
        with (
            patch.object(session.sys, "stdin", fake_stdin),
            patch("builtins.input", return_value="2"),
            redirect_stdout(StringIO()),
        ):
            selected = session.select_interface("ask", no_chat=False)

        self.assertEqual(selected, "web")

    def test_no_chat_overrides_interface(self) -> None:
        self.assertEqual(
            session.select_interface("web", no_chat=True),
            "none",
        )


if __name__ == "__main__":
    unittest.main()
