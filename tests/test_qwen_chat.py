import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "qwen-chat.py"
SPEC = importlib.util.spec_from_file_location("qwen_chat", MODULE_PATH)
assert SPEC and SPEC.loader
qwen_chat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qwen_chat
SPEC.loader.exec_module(qwen_chat)


class QwenChatTests(unittest.TestCase):
    def test_builds_text_and_image_content(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "paper.md"
            image = root / "figure.png"
            document.write_text("# Paper\nResult", encoding="utf-8")
            image.write_bytes(b"image-data")

            content, has_image = qwen_chat.make_user_content(
                "Explain the result.",
                [document, image],
                max_text_bytes=1024,
                max_image_bytes=1024,
            )

            self.assertTrue(has_image)
            self.assertIsInstance(content, list)
            self.assertEqual([item["type"] for item in content], [
                "text",
                "image_url",
                "text",
            ])
            self.assertIn("paper.md", content[0]["text"])
            self.assertTrue(
                content[1]["image_url"]["url"].startswith("data:image/png;base64,")
            )

    def test_rejects_more_than_one_image(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jpg"
            second = root / "second.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            with self.assertRaisesRegex(RuntimeError, "only one image"):
                qwen_chat.make_user_content(
                    "Compare.",
                    [first, second],
                    max_text_bytes=1024,
                    max_image_bytes=1024,
                )

    def test_strip_images_preserves_text(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png"}},
                    {"type": "text", "text": "What is shown?"},
                ],
            },
            {"role": "assistant", "content": "A chart."},
        ]

        qwen_chat.strip_images(messages)

        self.assertEqual(
            messages[0]["content"],
            [{"type": "text", "text": "What is shown?"}],
        )
        self.assertEqual(messages[1]["content"], "A chart.")


if __name__ == "__main__":
    unittest.main()
