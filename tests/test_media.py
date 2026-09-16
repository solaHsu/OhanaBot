"""图片附件取回与编码的测试（media.py）。

运行：python -m unittest discover -s tests -v
"""

import base64
import io
import tempfile
import unittest
from pathlib import Path

from bot.engine import media
from bot.engine.onebot.parser import ImageRef

try:
    from PIL import Image
except ImportError:
    Image = None

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class MediaTests(unittest.TestCase):
    def _png_path(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pic.png"
        path.write_bytes(PNG_BYTES)
        return path

    def test_local_file_to_data_url(self):
        ref = ImageRef(url="", file=str(self._png_path()))
        urls = media.collect_image_data_urls([ref])
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(urls[0].split(",", 1)[1]), PNG_BYTES)

    def test_file_url_scheme(self):
        ref = ImageRef(url="", file=f"file://{self._png_path()}")
        self.assertEqual(len(media.collect_image_data_urls([ref])), 1)

    def test_unknown_format_skipped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.bin"
        path.write_bytes(b"not an image")
        self.assertEqual(media.collect_image_data_urls([ImageRef(file=str(path))]), [])

    def test_oversize_skipped(self):
        original = media.MAX_IMAGE_BYTES
        media.MAX_IMAGE_BYTES = 8
        self.addCleanup(setattr, media, "MAX_IMAGE_BYTES", original)
        self.assertEqual(media.collect_image_data_urls([ImageRef(file=str(self._png_path()))]), [])

    def test_limit_applied(self):
        refs = [ImageRef(file=str(self._png_path())) for _ in range(3)]
        self.assertEqual(len(media.collect_image_data_urls(refs, limit=1)), 1)

    def test_missing_file_skipped(self):
        self.assertEqual(media.collect_image_data_urls([ImageRef(file="no-such-file.png")]), [])

    def test_webp_magic(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pic.webp"
        path.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        urls = media.collect_image_data_urls([ImageRef(file=str(path))])
        self.assertTrue(urls[0].startswith("data:image/webp;base64,"))

    @unittest.skipUnless(Image is not None, "需要 Pillow")
    def test_large_image_compressed_to_jpeg(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "big.png"
        Image.new("RGB", (1600, 800), (200, 30, 30)).save(path)
        urls = media.collect_image_data_urls([ImageRef(file=str(path))])
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("data:image/jpeg;base64,"))
        payload = base64.b64decode(urls[0].split(",", 1)[1])
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(max(image.size), 768)
            self.assertEqual(image.size, (768, 384))

    @unittest.skipUnless(Image is not None, "需要 Pillow")
    def test_small_image_untouched(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "small.png"
        Image.new("RGB", (100, 50), (10, 10, 10)).save(path)
        urls = media.collect_image_data_urls([ImageRef(file=str(path))])
        self.assertTrue(urls[0].startswith("data:image/png;base64,"))

    @unittest.skipUnless(Image is not None, "需要 Pillow")
    def test_compress_disabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "big.png"
        Image.new("RGB", (1600, 800), (200, 30, 30)).save(path)
        urls = media.collect_image_data_urls([ImageRef(file=str(path))], max_dimension=0)
        self.assertTrue(urls[0].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
