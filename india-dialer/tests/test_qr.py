"""The local QR encoder (static/js/qr.js), checked by an independent decoder.

The JavaScript runs in Node; OpenCV's QR reader decodes every symbol. Each
case is tried with all eight masks, and again with modules deliberately
damaged, which only decodes if the Reed-Solomon error correction is right.
Skips when Node or OpenCV is missing (test-only dependency:
uv pip install opencv-python-headless)."""
import json
import os
import random
import shutil
import subprocess
import unittest

from _base import APP

try:
    import cv2
    import numpy as np
except ImportError:                  # pragma: no cover
    cv2 = None

QR_JS = os.path.join(APP, "static", "js", "qr.js")
CASES = [
    # text, ecl, version
    ("tel:+919876543210", "M", 2),
    ("tel:+918041161000", "L", 1),
    ("tel:18004255758", "H", 3),
    ("http://192.168.1.23:8780/m?k=3f9c2a71b6d04e58", "M", 4),
    ("https://wa.me/919820204373?text=Hi%20Dale%2C%20it%27s%20Pawan", "Q", 6),
    ("https://pxlkraft.com/india?utm_source=call&utm_medium=whatsapp&utm_campaign=ballooning-trial", "M", 7),
    ("Drawing ballooning trial: 5 drawings, PDF or DWG. " * 3, "L", 8),
    ("x" * 200, "L", 9),
    ("y" * 230, "L", 10),
]


def js_matrices(jobs):
    script = (
        "import { qrMatrix } from " + json.dumps("file://" + QR_JS) + ";\n"
        "const jobs = " + json.dumps(jobs) + ";\n"
        "console.log(JSON.stringify(jobs.map(j => { const q = qrMatrix(j.text, { ecl: j.ecl, version: j.version, mask: j.mask });"
        " return { version: q.version, mask: q.mask, size: q.size, rows: q.modules.map(r => r.map(v => v ? 1 : 0).join('')) }; })));\n")
    done = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def decode(rows, scale=None, border=4):
    """OpenCV's detector misses some valid symbols at some pixel sizes (it
    misses segno's own mask-7 version-6 symbol at 6x too), so try a few."""
    if scale is None:
        for s in (4, 5, 6, 7, 8, 3):
            text = decode(rows, s, border)
            if text:
                return text
        return ""
    n = len(rows)
    img = np.full((n + 2 * border, n + 2 * border), 255, np.uint8)
    for y, row in enumerate(rows):
        for x, c in enumerate(row):
            if c == "1":
                img[y + border, x + border] = 0
    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LocalQr(unittest.TestCase):
    @unittest.skipUnless(cv2, "opencv not installed")
    def test_every_case_and_mask_decodes_to_the_same_text(self):
        jobs = [dict(text=t, ecl=e, version=v, mask=m) for t, e, v in CASES for m in range(8)]
        for job, qr in zip(jobs, js_matrices(jobs)):
            self.assertEqual((qr["version"], qr["mask"]), (job["version"], job["mask"]))
            self.assertEqual(decode(qr["rows"]), job["text"], (job["text"][:30], job["ecl"], job["version"], job["mask"]))

    @unittest.skipUnless(cv2, "opencv not installed")
    def test_error_correction_recovers_damage(self):
        # Level H recovers ~30% of codewords; flip a few isolated modules well inside that.
        jobs = [dict(text="tel:+919876543210", ecl="H", version=3, mask=2)]
        rows = [list(r) for r in js_matrices(jobs)[0]["rows"]]
        rng = random.Random(7)
        size = len(rows)
        flipped = 0
        while flipped < 14:
            x, y = rng.randrange(9, size - 9), rng.randrange(9, size - 9)
            if x == 6 or y == 6:
                continue
            rows[y][x] = "0" if rows[y][x] == "1" else "1"
            flipped += 1
        self.assertEqual(decode(["".join(r) for r in rows]), "tel:+919876543210")

    def test_a_forced_version_that_is_too_small_is_refused(self):
        with self.assertRaises(AssertionError) as caught:
            js_matrices([dict(text="tel:18004255758", ecl="H", version=2)])
        self.assertIn("does not fit", str(caught.exception))

    def test_automatic_version_is_the_smallest_that_fits(self):
        got = js_matrices([dict(text="tel:+919876543210", ecl="M"), dict(text="z" * 100, ecl="M"), dict(text="tel:+91", ecl="L")])
        self.assertEqual([g["version"] for g in got], [2, 6, 1])
        self.assertEqual([g["size"] for g in got], [25, 41, 21])
        self.assertTrue(all(0 <= g["mask"] < 8 for g in got))

    @unittest.skipUnless(cv2, "opencv not installed")
    def test_automatic_mask_symbols_decode(self):
        jobs = [dict(text=t, ecl=e) for t, e, _ in CASES]
        for job, qr in zip(jobs, js_matrices(jobs)):
            self.assertEqual(decode(qr["rows"]), job["text"])


if __name__ == "__main__":
    unittest.main()
