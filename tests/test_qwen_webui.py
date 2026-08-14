import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "qwen-webui.py"
SPEC = importlib.util.spec_from_file_location("qwen_webui", MODULE_PATH)
assert SPEC and SPEC.loader
webui = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = webui
SPEC.loader.exec_module(webui)


def data_url(payload: bytes = b"image") -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class QwenWebUITests(unittest.TestCase):
    def test_load_configuration_keeps_api_key_server_side(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            secret_root = root / "secrets"
            state_root.mkdir()
            secret_root.mkdir()
            key_path = secret_root / "vllm-api-key"
            key_path.write_text("secret-value", encoding="utf-8")
            (state_root / "vllm-server.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "base_url": "http://denebola:18765/v1",
                        "health_url": "http://denebola:18765/health",
                        "served_model_name": "qwen-research",
                        "pbs_job_id": "320.arcturus",
                        "api_key_file": str(key_path),
                    }
                ),
                encoding="utf-8",
            )

            configuration = webui.load_server_configuration(root)

            self.assertEqual(configuration["api_key"], "secret-value")
            self.assertEqual(configuration["model"], "qwen-research")

    def test_validate_messages_accepts_one_image_and_text(self) -> None:
        messages = webui.validate_messages(
            [
                {
                    "role": "user",
                    "ignored": "not forwarded",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url()}},
                        {"type": "text", "text": "Explain this chart."},
                    ],
                }
            ]
        )

        self.assertEqual(set(messages[0]), {"role", "content"})
        self.assertEqual(messages[0]["content"][0]["type"], "image_url")

    def test_validate_messages_rejects_two_active_images(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "only one active image"):
            webui.validate_messages(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url(b"1")}},
                            {"type": "image_url", "image_url": {"url": data_url(b"2")}},
                        ],
                    }
                ]
            )

    def test_validate_data_url_rejects_unsupported_mime_type(self) -> None:
        encoded = base64.b64encode(b"svg").decode("ascii")
        with self.assertRaisesRegex(RuntimeError, "unsupported image MIME"):
            webui.validate_data_url(f"data:image/svg+xml;base64,{encoded}")


if __name__ == "__main__":
    unittest.main()
