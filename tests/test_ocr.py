import sys
import unittest

from PIL import Image, ImageDraw, ImageFont

from screenpilot.capture import CapturedImage, Rect
from screenpilot.ocr import OcrLine, OcrResult, OcrWord, find_sensitive, to_markdown
from screenpilot.ocr.engine import WindowsOcrEngine, parse_tesseract_tsv
from screenpilot.ocr.sensitive import cnp_ok, iban_ok, luhn_ok


def line(*items, y=0, h=20):
    """items: (text, x)"""
    return OcrLine(tuple(OcrWord(t, Rect(x, y, len(t) * 10, h)) for t, x in items))


class OcrModelTests(unittest.TestCase):
    def test_text_find_and_scaling(self):
        r = OcrResult((line(("Hello", 0), ("World", 60)), line(("Second", 0), y=30)))
        self.assertEqual(r.text, "Hello World\nSecond")
        self.assertEqual([w.text for w in r.find("wor")], ["World"])
        self.assertEqual(r.lines[0].rect, Rect(0, 0, 110, 20))
        half = r.scaled(2.0)
        self.assertEqual(half.words[1].rect, Rect(30, 0, 25, 10))
        self.assertIsNone(r.confidence)

    def test_tesseract_tsv(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t5\t40\t12\t96.5\tHello\n"
            "5\t1\t1\t1\t1\t2\t60\t5\t45\t12\t80\tWorld\n"
            "5\t1\t1\t1\t2\t1\t10\t25\t30\t12\t-1\t \n"
            "5\t1\t1\t1\t2\t2\t10\t25\t30\t12\t70\tBye\n"
        )
        r = parse_tesseract_tsv(tsv, "eng")
        self.assertEqual(r.text, "Hello World\nBye")
        self.assertEqual(r.confidence, 82.2)

    def test_markdown_paragraphs_and_table(self):
        r = OcrResult(
            (
                line(("Monthly", 0), ("report", 80), y=0),
                line(("Name", 0), ("Amount", 300), y=60),
                line(("Contoso", 0), ("12,480", 300), y=85),
                line(("Fabrikam", 0), ("3,100", 300), y=110),
            )
        )
        md = to_markdown(r)
        self.assertIn("Monthly report", md)
        self.assertIn("| Name | Amount |", md)
        self.assertIn("| Fabrikam | 3,100 |", md)


class SensitiveTests(unittest.TestCase):
    def test_validators(self):
        self.assertTrue(luhn_ok("4111111111111111"))
        self.assertFalse(luhn_ok("4111111111111112"))
        self.assertTrue(iban_ok("RO49 AAAA 1B31 0075 9384 0000"))
        self.assertFalse(iban_ok("RO49 AAAA 1B31 0075 9384 0001"))
        self.assertTrue(cnp_ok("1800101221144"))
        self.assertFalse(cnp_ok("1800101221145"))

    def test_finds_and_maps_to_word_rects(self):
        r = OcrResult(
            (
                line(("Email:", 0), ("ana.pop@contoso.ro", 70), y=0),
                line(("Card", 0), ("4111", 50), ("1111", 100), ("1111", 150), ("1111", 200), y=30),
                line(("IBAN", 0), ("RO49", 50), ("AAAA", 100), ("1B31", 150), ("0075", 200), ("9384", 250), ("0000", 300), y=60),
                line(("CNP", 0), ("1800101221144", 40), y=90),
                line(("Tel", 0), ("+40", 40), ("721", 80), ("123", 115), ("456", 150), y=120),
                line(("Server", 0), ("10.20.30.40", 70), y=150),
                line(("Order", 0), ("10482", 60), ("total", 120), ("12,480.00", 180), y=180),
            )
        )
        matches = find_sensitive(r)
        kinds = [m.kind for m in matches]
        self.assertEqual(kinds, ["email", "card", "iban", "cnp", "phone", "ip"])
        email = matches[0]
        self.assertEqual(email.rect, Rect(70, 0, 180, 20))
        card = matches[1]
        self.assertEqual(card.rect, Rect(50, 30, 190, 20))
        only_email = find_sensitive(r, kinds=("email",))
        self.assertEqual(len(only_email), 1)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsOcrIntegrationTests(unittest.TestCase):
    def test_recognizes_rendered_text(self):
        engine = WindowsOcrEngine()
        if not engine.available():
            self.skipTest("Windows OCR has no languages installed on this machine")
        img = Image.new("RGB", (900, 160), "white")
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 44)
        except OSError:
            font = ImageFont.load_default()
        d.text((20, 20), "ScreenPilot invoice 12480", fill="black", font=font)
        d.text((20, 90), "contact ana.pop@contoso.ro", fill="black", font=font)
        result = engine.recognize(CapturedImage.from_pil(img), "en")
        text = result.text.lower()
        self.assertIn("screenpilot", text)
        self.assertIn("12480", text)
        self.assertTrue(any(m.kind == "email" for m in find_sensitive(result)))
        word = result.find("ScreenPilot")[0]
        self.assertTrue(10 <= word.rect.x <= 40, word.rect)
        print("Windows OCR languages:", engine.languages())


if __name__ == "__main__":
    unittest.main()
