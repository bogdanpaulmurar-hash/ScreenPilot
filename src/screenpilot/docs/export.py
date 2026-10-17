"""Procedure exports: Word (.docx), HTML (single file), Markdown (+ images).

The .docx writer produces a minimal, standards-compliant WordprocessingML
package with the zipfile module (no python-docx dependency): built-in
Title/Heading/Caption styles so Word's navigation pane and table of contents
work, numbered step headings and images scaled to the page width.
PDF export lives in the UI layer (Qt's PDF writer gives Unicode fonts).
"""

from __future__ import annotations

import base64
import html
import re
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from screenpilot.docs.model import ProcedureSession

EMU_PER_PX = 9525  # at 96 dpi
MAX_IMAGE_WIDTH_PX = 624  # 6.5 in text width on A4/Letter with normal margins


def step_heading(index: int, title: str, label: str = "Step") -> str:
    return f"{label} {index + 1}" + (f": {title}" if title else "")


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text.strip().lower()).strip("-") or "procedure"


# -- Markdown -----------------------------------------------------------------
def export_markdown(session: ProcedureSession, target: Path, *, step_label: str = "Step") -> Path:
    """Write ``target`` (.md) and copy images into ``<name>_images/`` next to it."""
    p = session.procedure
    images_dir = target.with_name(target.stem + "_images")
    images_dir.mkdir(parents=True, exist_ok=True)
    out = [f"# {p.title or 'Procedure'}", ""]
    meta = " · ".join(x for x in (p.author, p.created.replace("T", " ")) if x)
    if meta:
        out += [f"_{meta}_", ""]
    if p.introduction:
        out += [p.introduction, ""]
    for i, step in enumerate(session.steps):
        name = f"step-{i + 1:02d}.png"
        shutil.copyfile(session.image_path(step), images_dir / name)
        out.append(f"## {step_heading(i, step.title, step_label)}")
        out.append("")
        if step.description:
            out += [step.description, ""]
        out += [f"![{step_heading(i, step.title, step_label)}]({images_dir.name}/{name})", ""]
    target.write_text("\n".join(out), encoding="utf-8")
    return target


# -- HTML ---------------------------------------------------------------------
_CSS = """
body{font-family:"Segoe UI",system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#1b1b1b;line-height:1.55}
h1{font-size:28px;margin-bottom:4px}.meta{color:#666;margin-bottom:28px}
.step{margin:28px 0;padding-top:12px;border-top:1px solid #e5e5e5}
.step h2{font-size:19px;display:flex;align-items:center;gap:10px}
.num{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:30px;border-radius:15px;
background:#0067c0;color:#fff;font-size:15px}
.step img{max-width:100%;border:1px solid #ddd;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.desc{white-space:pre-wrap;margin:6px 0 12px}
@media print{.step{page-break-inside:avoid}}
"""


def export_html(session: ProcedureSession, target: Path, *, step_label: str = "Step", lang: str = "en") -> Path:
    p = session.procedure
    parts = [
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">',
        f"<title>{html.escape(p.title or 'Procedure')}</title><style>{_CSS}</style></head><body>",
        f"<h1>{html.escape(p.title or 'Procedure')}</h1>",
        f'<div class="meta">{html.escape(" · ".join(x for x in (p.author, p.created.replace("T", " ")) if x))}</div>',
    ]
    if p.introduction:
        parts.append(f'<p class="desc">{html.escape(p.introduction)}</p>')
    for i, step in enumerate(session.steps):
        data = base64.b64encode(session.image_path(step).read_bytes()).decode("ascii")
        title = html.escape(step.title or f"{step_label} {i + 1}")
        parts.append(f'<section class="step"><h2><span class="num">{i + 1}</span>{title}</h2>')
        if step.description:
            parts.append(f'<p class="desc">{html.escape(step.description)}</p>')
        parts.append(f'<img alt="{title}" src="data:image/png;base64,{data}"></section>')
    parts.append("</body></html>")
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


# -- Word (.docx) -------------------------------------------------------------
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Segoe UI" w:hAnsi="Segoe UI" w:cs="Segoe UI" w:eastAsia="Segoe UI"/>
<w:sz w:val="21"/><w:lang w:val="{lang}"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:after="80"/></w:pPr><w:rPr><w:b/><w:sz w:val="48"/><w:color w:val="1B1B1B"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:rPr><w:color w:val="666666"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="30"/><w:color w:val="0067C0"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:rPr><w:i/><w:sz w:val="18"/><w:color w:val="666666"/></w:rPr></w:style>
</w:styles>"""


def _para(text: str, style: str | None = None, *, keep_next: bool = False) -> str:
    ppr = ""
    if style or keep_next:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + ("<w:keepNext/>" if keep_next else "") + "</w:pPr>"
    runs = []
    for i, line in enumerate(text.split("\n")):
        if i:
            runs.append("<w:r><w:br/></w:r>")
        runs.append(f'<w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r>')
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _image_para(rel_id: str, index: int, name: str, width_px: int, height_px: int) -> str:
    scale = min(1.0, MAX_IMAGE_WIDTH_PX / max(1, width_px))
    cx, cy = int(width_px * scale * EMU_PER_PX), int(height_px * scale * EMU_PER_PX)
    return (
        "<w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr><w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{index}" name="{escape(name)}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{index}" name="{escape(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:ln w="6350"><a:solidFill><a:srgbClr val="D0D0D0"/></a:solidFill></a:ln></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def export_docx(session: ProcedureSession, target: Path, *, step_label: str = "Step", lang: str = "en-US") -> Path:
    p = session.procedure
    body = [_para(p.title or "Procedure", "Title")]
    meta = " · ".join(x for x in (p.author, p.created.replace("T", " ")) if x)
    if meta:
        body.append(_para(meta, "Subtitle"))
    if p.introduction:
        body.append(_para(p.introduction))
    rels = [
        '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]
    media: list[tuple[str, Path]] = []
    for i, step in enumerate(session.steps):
        rel_id = f"rIdImg{i + 1}"
        name = f"image{i + 1}.png"
        media.append((name, session.image_path(step)))
        rels.append(
            f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{name}"/>'
        )
        body.append(_para(step_heading(i, step.title, step_label), "Heading1"))
        if step.description:
            body.append(_para(step.description, keep_next=True))
        body.append(_image_para(rel_id, i + 1, name, step.width or 800, step.height or 600))
    body.append(
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels) + "</Relationships>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(p.title or 'Procedure')}</dc:title><dc:creator>{escape(p.author)}</dc:creator>"
        "<cp:keywords>ScreenPilot</cp:keywords></cp:coreProperties>"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("docProps/core.xml", core)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _STYLES.replace("{lang}", lang))
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, path in media:
            zf.write(path, f"word/media/{name}")
    return target


def default_name(session: ProcedureSession) -> str:
    return _slug(session.procedure.title or "procedure")
