import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vision


class BuildUserMessageTests(unittest.TestCase):
    def test_text_only_messages_keep_string_content(self):
        expected = {"role": "user", "content": "hello"}

        self.assertEqual(vision.build_user_message("hello", []), (expected, expected))

    def test_supported_images_use_detected_mime_and_history_keeps_only_count(self):
        samples = {
            "photo.png": (b"\x89PNG\r\n\x1a\nbody", "image/png"),
            "photo.jpg": (b"\xff\xd8\xff\xe0body", "image/jpeg"),
            "photo.webp": (b"RIFF\x04\x00\x00\x00WEBPbody", "image/webp"),
            "photo.gif": (b"GIF89abody", "image/gif"),
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, (data, _) in samples.items():
                path = Path(directory, name)
                path.write_bytes(data)
                paths.append(path)

            wire, stored = vision.build_user_message("describe", paths)

        self.assertEqual(wire["role"], "user")
        self.assertEqual(wire["content"][0], {"type": "text", "text": "describe"})
        for part, (_, (data, mime)) in zip(wire["content"][1:], samples.items()):
            self.assertEqual(part["type"], "image_url")
            self.assertEqual(
                part["image_url"]["url"],
                f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            )
        self.assertEqual(stored, {"role": "user", "content": "describe\n\n[Attached images: 4]"})
        for name in samples:
            self.assertNotIn(name, stored["content"])

    def test_rejects_more_than_four_images(self):
        with self.assertRaises(ValueError):
            vision.build_user_message("hello", ["missing.png"] * 5)

    def test_total_limit_is_checked_before_any_base64_encoding(self):
        data = b"\x89PNG\r\n\x1a\nbody"
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory, name) for name in ("one.png", "two.png")]
            for path in paths:
                path.write_bytes(data)

            with (
                patch.object(vision, "MAX_TOTAL_IMAGE_BYTES", 2 * len(data) - 1, create=True),
                patch.object(vision.base64, "b64encode") as encode,
            ):
                with self.assertRaises(ValueError):
                    vision.build_user_message("hello", paths)
                encode.assert_not_called()
            with patch.object(vision, "MAX_TOTAL_IMAGE_BYTES", 2 * len(data), create=True):
                wire, _ = vision.build_user_message("hello", paths)

        self.assertEqual(len(wire["content"]), 3)

    def test_rejects_missing_directories_oversize_and_unknown_magic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = root / "unknown.png"
            unknown.write_bytes(b"not an image")
            oversized = root / "large.png"
            oversized.write_bytes(b"\x89PNG\r\n\x1a\nbody")

            for path in (root / "missing.png", root, unknown):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    vision.build_user_message("hello", [path])
            with patch.object(vision, "MAX_IMAGE_BYTES", 8):
                with self.assertRaises(ValueError):
                    vision.build_user_message("hello", [oversized])

    def test_extension_cannot_override_magic_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "actually-png.jpg")
            data = b"\x89PNG\r\n\x1a\nbody"
            path.write_bytes(data)

            wire, _ = vision.build_user_message("hello", [path])

        self.assertTrue(wire["content"][1]["image_url"]["url"].startswith("data:image/png;"))


if __name__ == "__main__":
    unittest.main()
