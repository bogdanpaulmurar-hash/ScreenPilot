import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.dom import minidom

from screenpilot.capture import CaptureService, Rect
from screenpilot.docs import ProcedureSession, export_docx, export_html, export_image, export_markdown
from screenpilot.docs import export as export_mod
from screenpilot.samples import DemoBackend


def make_session(root: Path) -> ProcedureSession:
    backend = DemoBackend(Rect(0, 0, 1280, 800))
    service = CaptureService(backend)
    session = ProcedureSession.create(root, title="Aprobare comandă vânzare", author="Bogdan")
    erp = backend.list_windows()[0].bounds
    session.add(service.capture_region(Rect(erp.x, erp.y, erp.width, 120)).image, title="Deschide comanda", description="Caută comanda 10482.")
    session.add(service.capture_window().image, title="Verifică datele", source="ERP Client")
    session.add(service.capture_region(Rect(erp.right - 230, erp.bottom - 60, 230, 60)).image, title="Apasă Approve", description="Linia 1\nLinia 2 cu „ghilimele” & <tag>")
    return session


class ProcedureSessionTests(unittest.TestCase):
    def test_add_move_update_remove_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(Path(tmp))
            self.assertEqual(len(session.steps), 3)
            session.move(2, 0)
            self.assertEqual(session.steps[0].title, "Apasă Approve")
            session.update(1, description="Nou")
            removed = session.steps[2].image
            session.remove(2)
            self.assertFalse((session.folder / removed).exists())
            again = ProcedureSession.load(session.folder)
            self.assertEqual([s.title for s in again.steps], ["Apasă Approve", "Deschide comanda"])
            self.assertEqual(again.steps[1].description, "Nou")
            self.assertEqual(ProcedureSession.unfinished(Path(tmp)), [session.folder])
            session.discard()
            self.assertEqual(ProcedureSession.unfinished(Path(tmp)), [])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.session = make_session(self.root / "sessions")

    def tearDown(self):
        self._tmp.cleanup()

    def test_markdown(self):
        md = export_markdown(self.session, self.root / "out" / "proc.md", step_label="Pasul")
        text = md.read_text(encoding="utf-8")
        self.assertIn("# Aprobare comandă vânzare", text)
        self.assertIn("## Pasul 3: Apasă Approve", text)
        self.assertIn("![Pasul 1: Deschide comanda](proc_images/step-01.png)", text)
        self.assertEqual(len(list((self.root / "out" / "proc_images").glob("*.png"))), 3)

    def test_html_is_self_contained_and_escaped(self):
        page = export_html(self.session, self.root / "proc.html", lang="ro").read_text(encoding="utf-8")
        self.assertEqual(page.count("data:image/png;base64,"), 3)
        self.assertIn("&lt;tag&gt;", page)
        self.assertNotIn("<tag>", page)

    def test_docx_structure_is_valid(self):
        target = export_docx(self.session, self.root / "proc.docx", step_label="Pasul", lang="ro-RO")
        with zipfile.ZipFile(target) as zf:
            names = set(zf.namelist())
            for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml", "word/_rels/document.xml.rels"):
                self.assertIn(part, names)
                minidom.parseString(zf.read(part))  # well-formed XML
            self.assertEqual(len([n for n in names if n.startswith("word/media/")]), 3)
            doc = zf.read("word/document.xml").decode("utf-8")
        self.assertIn("Pasul 2: Verifică datele", doc)
        self.assertIn("&lt;tag&gt;", doc)
        self.assertEqual(len(re.findall(r'w:val="Heading1"', doc)), 3)

    @unittest.skipUnless(shutil.which("soffice"), "LibreOffice not installed")
    def test_docx_opens_in_libreoffice(self):
        target = export_docx(self.session, self.root / "proc.docx")
        out = self.root / "pdf"
        proc = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(target)],
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((out / "proc.pdf").exists())


class SingleImageExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.session = make_session(self.tmp / "s")

    def _sizes(self):
        from PIL import Image

        out = []
        for step in self.session.steps:
            with Image.open(self.session.image_path(step)) as im:
                out.append(im.size)
        return out

    def test_steps_are_stacked_top_to_bottom(self):
        from PIL import Image

        target = export_image(self.session, self.tmp / "all.png", step_label="Pasul")
        sizes = self._sizes()
        m, gap, head = export_mod.IMAGE_MARGIN, export_mod.IMAGE_GAP, export_mod.IMAGE_HEADER
        with Image.open(target) as page:
            self.assertEqual(page.width, max(w for w, _ in sizes) + 2 * m)
            self.assertEqual(page.height, 2 * m + sum(h + head for _, h in sizes) + gap * (len(sizes) - 1))
            # the first pixel of every step image sits where expected, in order
            y = m
            page = page.convert("RGB")
            for step, (w, h) in zip(self.session.steps, sizes):
                y += head
                with Image.open(self.session.image_path(step)) as im:
                    self.assertEqual(page.getpixel((m + w // 2, y + h // 2)), im.convert("RGB").getpixel((w // 2, h // 2)))
                y += h + gap

    def test_without_headers(self):
        from PIL import Image

        target = export_image(self.session, self.tmp / "plain.png", headers=False)
        sizes = self._sizes()
        m, gap = export_mod.IMAGE_MARGIN, export_mod.IMAGE_GAP
        with Image.open(target) as page:
            self.assertEqual(page.height, 2 * m + sum(h for _, h in sizes) + gap * (len(sizes) - 1))

    def test_very_tall_result_is_scaled_down(self):
        from PIL import Image

        old = export_mod.IMAGE_MAX_SIDE
        export_mod.IMAGE_MAX_SIDE = 500
        self.addCleanup(setattr, export_mod, "IMAGE_MAX_SIDE", old)
        with Image.open(export_image(self.session, self.tmp / "small.png")) as page:
            self.assertLessEqual(max(page.size), 500)

    def test_empty_procedure_is_refused(self):
        empty = ProcedureSession.create(self.tmp / "e")
        with self.assertRaises(ValueError):
            export_image(empty, self.tmp / "none.png")


if __name__ == "__main__":
    unittest.main()
