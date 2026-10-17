import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.dom import minidom

from screenpilot.capture import CaptureService, Rect
from screenpilot.docs import ProcedureSession, export_docx, export_html, export_markdown
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


if __name__ == "__main__":
    unittest.main()
