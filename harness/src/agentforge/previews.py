"""Read-only previews for workspace documents, without third-party libraries.

The agent writes documents into a sandbox workspace that has no network and no
way to install anything, so an Office file has to be read where it lies. Every
reader here stays inside the standard library -- `zipfile` and `ElementTree`,
which is all an OOXML package really is -- and every result is bounded before it
reaches the browser: a spreadsheet of a hundred thousand rows is a preview of
five hundred, and a deck of two hundred slides is still two hundred slides of
text, not of layout.
"""

from io import BytesIO
import re
import zipfile
from xml.etree import ElementTree

MAX_SHEETS = 20
MAX_SHEET_ROWS = 500
MAX_SHEET_COLUMNS = 40
MAX_SLIDES = 200
MAX_SLIDE_LINES = 40
MAX_PARAGRAPHS = 600
MAX_CELL_CHARS = 200
MAX_LINE_CHARS = 400

# A 2 MB package can inflate into something far larger; stop reading well before
# that becomes the process's problem.
MAX_XML_BYTES = 8_000_000

RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _value(element: ElementTree.Element, name: str) -> str:
    for key, attribute in element.attrib.items():
        if _local(key) == name:
            return attribute
    return ""


def _xml(archive: zipfile.ZipFile, member: str) -> ElementTree.Element | None:
    try:
        with archive.open(member) as handle:
            return ElementTree.fromstring(handle.read(MAX_XML_BYTES))
    except (KeyError, ElementTree.ParseError, OSError, zipfile.BadZipFile):
        return None


def _text_of(node: ElementTree.Element) -> str:
    return "".join(element.text or "" for element in node.iter()
                   if _local(element.tag) == "t").strip()


def _column_index(reference: str) -> int:
    letters = ""
    for character in reference:
        if character.isalpha():
            letters += character
        elif letters:
            break
    index = 0
    for character in letters.upper():
        index = index * 26 + (ord(character) - 64)
    return max(0, index - 1)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _xml(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    return [_text_of(item) for item in root if _local(item.tag) == "si"]


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Sheet names paired with their member, resolved through the relationship ids."""
    relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {}
    if relationships is not None:
        for relationship in relationships:
            identifier = relationship.get("Id") or ""
            if identifier and relationship.get("Target"):
                targets[identifier] = relationship.get("Target", "").lstrip("/")
    workbook = _xml(archive, "xl/workbook.xml")
    if workbook is None:
        return []
    sheets = []
    for element in workbook.iter():
        if _local(element.tag) != "sheet":
            continue
        target = targets.get(element.get(f"{{{RELATIONSHIPS}}}id", ""), "")
        if not target:
            continue
        sheets.append((element.get("name") or "Sheet",
                       target if target.startswith("xl/") else f"xl/{target}"))
    return sheets


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t") or ""
    if kind == "inlineStr":
        return _text_of(cell)
    value = ""
    for element in cell:
        tag = _local(element.tag)
        if tag == "is":
            return _text_of(element)
        if tag == "v":
            value = element.text or ""
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def _sheet_rows(archive: zipfile.ZipFile, target: str, shared: list[str]) -> list[list[str]]:
    root = _xml(archive, target)
    if root is None:
        return []
    rows = []
    for element in root.iter():
        if _local(element.tag) != "row":
            continue
        cells = {}
        for cell in element:
            if _local(cell.tag) != "c":
                continue
            value = _cell_value(cell, shared)
            if value:
                cells[_column_index(cell.get("r", ""))] = value[:MAX_CELL_CHARS]
        if cells:
            rows.append(cells)
        if len(rows) >= MAX_SHEET_ROWS:
            break
    width = min(max((max(cells) + 1 for cells in rows), default=0), MAX_SHEET_COLUMNS)
    return [[cells.get(index, "") for index in range(width)] for cells in rows]


def sheets_from_xlsx(data: bytes) -> dict:
    sheets = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        shared = _shared_strings(archive)
        for name, target in _sheet_targets(archive)[:MAX_SHEETS]:
            rows = _sheet_rows(archive, target, shared)
            # The first row names the columns; a single-row sheet has no header
            # to promote, so it stays a row.
            header, body = (rows[0], rows[1:]) if len(rows) > 1 else ([], rows)
            sheets.append({"name": name, "columns": header, "rows": body})
    return {"sheets": sheets}


def _slide_order(name: str) -> tuple[int, str]:
    digits = re.findall(r"\d+", name.rsplit("/", 1)[-1])
    return (int(digits[0]) if digits else 0, name)


def slides_from_pptx(data: bytes) -> dict:
    slides = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        members = sorted((name for name in archive.namelist()
                          if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=_slide_order)
        for member in members[:MAX_SLIDES]:
            root = _xml(archive, member)
            if root is None:
                continue
            title, lines = "", []
            for shape in root.iter():
                if _local(shape.tag) != "sp":
                    continue
                placeholder, paragraphs = "", []
                for element in shape.iter():
                    tag = _local(element.tag)
                    if tag == "ph":
                        placeholder = element.get("type", "") or ""
                    elif tag == "p":
                        level = 0
                        for child in element:
                            if _local(child.tag) == "pPr":
                                try:
                                    level = int(child.get("lvl", "0"))
                                except ValueError:
                                    level = 0
                        value = _text_of(element)[:MAX_LINE_CHARS]
                        if value:
                            paragraphs.append(("  " * level) + value)
                if not paragraphs:
                    continue
                if placeholder in {"title", "ctrTitle"} and not title:
                    title, paragraphs = paragraphs[0].strip(), paragraphs[1:]
                lines.extend(paragraphs)
            slides.append({"title": title[:MAX_LINE_CHARS], "body": lines[:MAX_SLIDE_LINES]})
    return {"slides": slides}


def document_from_docx(data: bytes) -> dict:
    paragraphs = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        root = _xml(archive, "word/document.xml")
        if root is None:
            return {"paragraphs": paragraphs}
        for element in root.iter():
            if _local(element.tag) != "p":
                continue
            style = ""
            for child in element:
                if _local(child.tag) != "pPr":
                    continue
                for grandchild in child:
                    if _local(grandchild.tag) == "pStyle":
                        style = _value(grandchild, "val")
            value = _text_of(element)[:MAX_LINE_CHARS]
            if value:
                paragraphs.append({"style": style, "text": value})
            if len(paragraphs) >= MAX_PARAGRAPHS:
                break
    return {"paragraphs": paragraphs}


BUILDERS = {"xlsx": ("sheets", sheets_from_xlsx), "pptx": ("slides", slides_from_pptx),
            "docx": ("document", document_from_docx)}


def build_preview(kind: str, data: bytes) -> tuple[str, dict] | None:
    """Return `(preview kind, payload)` for a format that has one, else None."""
    entry = BUILDERS.get(kind)
    if entry is None:
        return None
    name, builder = entry
    try:
        return name, builder(data)
    except (ElementTree.ParseError, KeyError, IndexError, ValueError, OSError,
            zipfile.BadZipFile) as cause:
        raise ValueError(f"{kind} could not be read as a document") from cause
