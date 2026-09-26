"""Small, dependency-free XLSX export for finalized delivery records.

The web interface is the only correction surface. The workbook is a one-way,
owner-reviewed audit artifact; a narrow legacy reader remains for compatibility
with already-issued files, but no upload route exposes it.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import posixpath
import re
import uuid
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from django.conf import settings

from .models import Delivery

SCHEMA_VERSION = "2"
DATA_SHEET = "Delivery"
METADATA_SHEET = "Metadata"
MAX_XLSX_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_ROWS = 10_000

HEADERS = (
    "Line ID",
    "Position",
    "Vendor SKU",
    "UPC",
    "Description",
    "Pack",
    "Cases",
    "Units per case",
    "Received units",
    "Square count before",
    "Proposed delta",
    "Projected count after",
    "Square variation ID",
    "Square item name",
    "Include",
    "Match status",
    "Review note",
    "Invoice unit cost",
    "Square baseline unit cost",
    "Unit cost change",
    "Unit cost change %",
    "Cost baseline source",
    "Invoice line total",
)

_DANGEROUS_CELL_START = re.compile(r"^\s*[=+\-@]")
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class WorkbookValidationError(ValueError):
    """The uploaded workbook is unsafe, stale or not an exported delivery file."""

    def __init__(self, message: str, *, field: str | None = None, row: int | None = None):
        super().__init__(message)
        self.field = field
        self.row = row


@dataclass(frozen=True)
class WorkbookLine:
    row_number: int
    values: dict[str, object]


@dataclass(frozen=True)
class DeliveryWorkbook:
    delivery_id: uuid.UUID
    revision: int
    line_count: int
    signature: str
    lines: tuple[WorkbookLine, ...]


def safe_excel_text(value: object) -> str:
    """Neutralize text that spreadsheet programs may treat as a formula."""

    text = "" if value is None else str(value)
    if _DANGEROUS_CELL_START.match(text):
        return "'" + text
    return text


def restore_excel_text(value: object) -> str:
    """Undo only the formula-neutralizing apostrophe added by this exporter."""

    text = "" if value is None else str(value)
    if text.startswith("'") and _DANGEROUS_CELL_START.match(text[1:]):
        return text[1:]
    return text


def _line_ids(delivery: Delivery) -> list[str]:
    return [
        str(value)
        for value in delivery.lines.order_by("position", "id").values_list("id", flat=True)
    ]


def workbook_signature(*, delivery_id: object, revision: int, line_ids: list[str]) -> str:
    payload = "\n".join(
        [SCHEMA_VERSION, str(delivery_id), str(revision), str(len(line_ids)), *line_ids]
    ).encode()
    return hmac.new(
        str(settings.SECRET_KEY).encode(), payload, digestmod=hashlib.sha256
    ).hexdigest()


def export_workbook(delivery: Delivery) -> bytes:
    lines = list(delivery.lines.order_by("position", "id"))
    if len(lines) > MAX_ROWS - 4:
        raise WorkbookValidationError("This delivery has too many lines for one workbook.")
    line_ids = [str(line.id) for line in lines]
    signature = workbook_signature(
        delivery_id=delivery.id,
        revision=delivery.spreadsheet_revision,
        line_ids=line_ids,
    )

    title = f"Delivery {delivery.invoice_number or str(delivery.id)[:8]}"
    instructions = (
        "Finalized owner-reviewed snapshot. Make corrections only in Store Ops; "
        "this download cannot be uploaded back into the app."
    )
    data_rows: list[list[object]] = [
        [title],
        [instructions],
        [],
        list(HEADERS),
    ]
    for line in lines:
        data_rows.append(
            [
                str(line.id),
                line.position,
                safe_excel_text(line.vendor_sku),
                safe_excel_text(line.upc),
                safe_excel_text(line.description),
                safe_excel_text(line.pack_text),
                line.cases,
                line.units_per_case,
                line.received_units,
                line.square_count_before,
                line.proposed_delta,
                line.projected_count_after,
                safe_excel_text(line.square_catalog_variation_id),
                safe_excel_text(line.square_item_name),
                line.included,
                line.match_status,
                safe_excel_text(line.review_note),
                _money_value(line.unit_cost_cents),
                _money_value(line.square_unit_cost_cents),
                _money_value(line.unit_cost_change_cents),
                line.unit_cost_change_display,
                safe_excel_text(line.square_unit_cost_source),
                _money_value(line.line_total_cents),
            ]
        )

    metadata_rows: list[list[object]] = [
        ["schema_version", SCHEMA_VERSION],
        ["delivery_id", str(delivery.id)],
        ["spreadsheet_revision", delivery.spreadsheet_revision],
        ["line_count", len(lines)],
        ["signature", signature],
    ]

    return _build_xlsx(data_rows=data_rows, metadata_rows=metadata_rows)


def _money_value(cents: int | None) -> Decimal | None:
    return Decimal(cents) / 100 if cents is not None else None


def _build_xlsx(*, data_rows: list[list[object]], metadata_rows: list[list[object]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_relationships_xml())
        archive.writestr("docProps/app.xml", _app_properties_xml())
        archive.writestr("docProps/core.xml", _core_properties_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml())
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_relationships_xml())
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _worksheet_xml(data_rows, is_data_sheet=True),
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            _worksheet_xml(metadata_rows, is_data_sheet=False),
        )
    return output.getvalue()


def _content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _root_relationships_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _app_properties_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Store Ops</Application></Properties>"""


def _core_properties_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>Store Ops</dc:creator><cp:lastModifiedBy>Store Ops</cp:lastModifiedBy></cp:coreProperties>"""


def _workbook_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">
  <sheets>
    <sheet name="{DATA_SHEET}" sheetId="1" r:id="rId1"/>
    <sheet name="{METADATA_SHEET}" sheetId="2" state="veryHidden" r:id="rId2"/>
  </sheets>
</workbook>"""


def _workbook_relationships_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _styles_xml() -> str:
    # Styles: 0 default, 1 title, 2 header, 3 identifiers, 4 body,
    # 5 quantities, 6 derived fields, 7 currency.
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="2"><numFmt numFmtId="164" formatCode="0.000"/><numFmt numFmtId="165" formatCode="$#,##0.00;[Red]-$#,##0.00"/></numFmts>
  <fonts count="3">
    <font><sz val="10"/><name val="Arial"/></font>
    <font><b/><sz val="14"/><name val="Arial"/><color rgb="FF172033"/></font>
    <font><b/><sz val="10"/><name val="Arial"/><color rgb="FFFFFFFF"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF233876"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF2F4F7"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left style="thin"><color rgb="FFD9DEE8"/></left><right style="thin"><color rgb="FFD9DEE8"/></right><top style="thin"><color rgb="FFD9DEE8"/></top><bottom style="thin"><color rgb="FFD9DEE8"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="8">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
    <xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
    <xf numFmtId="164" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>
    <xf numFmtId="165" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _worksheet_xml(rows: list[list[object]], *, is_data_sheet: bool) -> str:
    row_xml: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row, start=1):
            if value is None:
                continue
            style = _cell_style(row_index, column_index, is_data_sheet=is_data_sheet)
            cells.append(_cell_xml(row_index, column_index, value, style=style))
        height = ' ht="30" customHeight="1"' if is_data_sheet and row_index == 4 else ""
        row_xml.append(f'<row r="{row_index}"{height}>{"".join(cells)}</row>')

    extras = ""
    if is_data_sheet:
        last_row = max(len(rows), 4)
        extras = f"""
  <autoFilter ref="A4:W{last_row}"/>"""
        sheet_views = """<sheetViews><sheetView workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="C5" sqref="C5"/></sheetView></sheetViews>"""
        columns = """<cols>
    <col min="1" max="1" width="4" hidden="1" customWidth="1"/>
    <col min="2" max="2" width="9" customWidth="1"/>
    <col min="3" max="4" width="17" customWidth="1"/>
    <col min="5" max="5" width="38" customWidth="1"/>
    <col min="6" max="6" width="14" customWidth="1"/>
    <col min="7" max="9" width="14" customWidth="1"/>
    <col min="10" max="12" width="19" customWidth="1"/>
    <col min="13" max="13" width="28" customWidth="1"/>
    <col min="14" max="14" width="32" customWidth="1"/>
    <col min="15" max="16" width="15" customWidth="1"/>
    <col min="17" max="17" width="38" customWidth="1"/>
    <col min="18" max="20" width="20" customWidth="1"/>
    <col min="21" max="21" width="17" customWidth="1"/>
    <col min="22" max="22" width="30" customWidth="1"/>
    <col min="23" max="23" width="20" customWidth="1"/>
  </cols>"""
    else:
        sheet_views = '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        columns = '<cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="2" width="72" customWidth="1"/></cols>'

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{_MAIN_NS}">
  {sheet_views}
  {columns}
  <sheetData>{"".join(row_xml)}</sheetData>{extras}
</worksheet>"""


def _cell_style(row: int, column: int, *, is_data_sheet: bool) -> int:
    if not is_data_sheet:
        return 0
    if row == 1:
        return 1
    if row == 4:
        return 2
    if row < 5:
        return 0
    if column in {1, 2}:
        return 3
    if column in {10, 11, 12, 16, 21}:
        return 6
    if column in {7, 9}:
        return 5
    if column in {18, 19, 20, 23}:
        return 7
    return 4


def _cell_xml(row: int, column: int, value: object, *, style: int) -> str:
    reference = f"{_column_letters(column)}{row}"
    style_attr = f' s="{style}"' if style else ""
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attr}><v>{int(value)}</v></c>'
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return f'<c r="{reference}" t="n"{style_attr}><v>{value}</v></c>'
    text = escape(_xml_safe_text(str(value)))
    return (
        f'<c r="{reference}" t="inlineStr"{style_attr}>'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _column_letters(column: int) -> str:
    result = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        result = chr(65 + remainder) + result
    return result


def read_workbook(upload: object) -> DeliveryWorkbook:
    raw = _read_upload(upload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise WorkbookValidationError("Upload a valid .xlsx delivery workbook.") from exc

    with archive:
        _validate_archive(archive)
        shared_strings = _read_shared_strings(archive)
        sheet_paths = _sheet_paths(archive)
        try:
            data_path = sheet_paths[DATA_SHEET]
            metadata_path = sheet_paths[METADATA_SHEET]
        except KeyError as exc:
            raise WorkbookValidationError(
                "This file is missing the Delivery or Metadata worksheet."
            ) from exc
        data_rows = _read_sheet(archive, data_path, shared_strings)
        metadata_rows = _read_sheet(archive, metadata_path, shared_strings)

    metadata = _metadata_values(metadata_rows)
    required_metadata = {
        "schema_version",
        "delivery_id",
        "spreadsheet_revision",
        "line_count",
        "signature",
    }
    missing = required_metadata - metadata.keys()
    if missing:
        raise WorkbookValidationError(
            f"Workbook metadata is incomplete: missing {', '.join(sorted(missing))}."
        )
    if str(metadata["schema_version"]) != SCHEMA_VERSION:
        raise WorkbookValidationError("This workbook version is not supported.")

    try:
        delivery_id = uuid.UUID(str(metadata["delivery_id"]))
        revision = _strict_integer(metadata["spreadsheet_revision"], "spreadsheet_revision")
        line_count = _strict_integer(metadata["line_count"], "line_count")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, WorkbookValidationError):
            raise
        raise WorkbookValidationError("Workbook metadata contains invalid values.") from exc

    lines = _data_lines(data_rows)
    if len(lines) != line_count:
        raise WorkbookValidationError(
            f"Workbook declares {line_count} lines but contains {len(lines)}."
        )
    return DeliveryWorkbook(
        delivery_id=delivery_id,
        revision=revision,
        line_count=line_count,
        signature=str(metadata["signature"]),
        lines=tuple(lines),
    )


def _read_upload(upload: object) -> bytes:
    if isinstance(upload, bytes):
        raw = upload
    elif isinstance(upload, bytearray):
        raw = bytes(upload)
    elif hasattr(upload, "read"):
        raw = upload.read(MAX_XLSX_BYTES + 1)
    else:
        raise WorkbookValidationError("Upload must be an .xlsx file or file object.")
    if not raw:
        raise WorkbookValidationError("The uploaded workbook is empty.")
    if len(raw) > MAX_XLSX_BYTES:
        raise WorkbookValidationError("The workbook is larger than the 10 MB safety limit.")
    return raw


def _validate_archive(archive: zipfile.ZipFile) -> None:
    names = [info.filename for info in archive.infolist()]
    if len(names) != len(set(names)):
        raise WorkbookValidationError("The workbook contains duplicate archive entries.")
    if any(
        name.lower().endswith("vbaproject.bin") or name.lower().startswith("xl/externallinks/")
        for name in names
    ):
        raise WorkbookValidationError("Macros and external workbook links are not allowed.")
    total_size = 0
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise WorkbookValidationError("The workbook archive contains an unsafe path.")
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise WorkbookValidationError("The workbook expands beyond the safety limit.")


def _sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise WorkbookValidationError("The workbook package is incomplete.") from exc

    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
        if "Id" in rel.attrib and "Target" in rel.attrib
    }
    paths: dict[str, str] = {}
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        name = sheet.attrib.get("name")
        relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
        if not name or not relationship_id or relationship_id not in targets:
            continue
        target = targets[relationship_id].lstrip("/")
        if not target.startswith("xl/"):
            target = posixpath.normpath(posixpath.join("xl", target))
        paths[name] = target
    return paths


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except ET.ParseError as exc:
        raise WorkbookValidationError("The workbook shared strings are invalid.") from exc
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]


def _read_sheet(
    archive: zipfile.ZipFile, path: str, shared_strings: list[str]
) -> dict[int, dict[int, object]]:
    try:
        root = ET.fromstring(archive.read(path))
    except (KeyError, ET.ParseError) as exc:
        raise WorkbookValidationError("A worksheet in the workbook is invalid.") from exc

    result: dict[int, dict[int, object]] = {}
    for row in root.findall(f".//{{{_MAIN_NS}}}row"):
        row_number = int(row.attrib.get("r", "0"))
        if row_number <= 0 or row_number > MAX_ROWS:
            raise WorkbookValidationError("The workbook has an invalid or excessive row count.")
        values: dict[int, object] = {}
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            if cell.find(f"{{{_MAIN_NS}}}f") is not None:
                raise WorkbookValidationError(
                    "Formulas are not accepted in delivery workbooks.", row=row_number
                )
            reference = cell.attrib.get("r", "")
            column = _column_number(reference)
            values[column] = _cell_value(cell, shared_strings, row_number=row_number)
        result[row_number] = values
    return result


def _column_number(reference: str) -> int:
    match = re.fullmatch(r"([A-Z]+)[1-9]\d*", reference.upper())
    if not match:
        raise WorkbookValidationError("A worksheet contains an invalid cell reference.")
    result = 0
    for character in match.group(1):
        result = result * 26 + ord(character) - 64
    return result


def _cell_value(cell: ET.Element, shared_strings: list[str], *, row_number: int) -> object:
    cell_type = cell.attrib.get("t", "n")
    value_node = cell.find(f"{{{_MAIN_NS}}}v")
    raw = "" if value_node is None or value_node.text is None else value_node.text
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{_MAIN_NS}}}t"))
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError) as exc:
            raise WorkbookValidationError(
                "A worksheet has an invalid shared-string reference.", row=row_number
            ) from exc
    if cell_type == "b":
        if raw not in {"0", "1"}:
            raise WorkbookValidationError("A boolean cell is invalid.", row=row_number)
        return raw == "1"
    if cell_type in {"str", "d"}:
        return raw
    if cell_type == "e":
        raise WorkbookValidationError("Spreadsheet error cells are not accepted.", row=row_number)
    if raw == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise WorkbookValidationError("A numeric cell is invalid.", row=row_number) from exc


def _metadata_values(rows: dict[int, dict[int, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for values in rows.values():
        key = values.get(1)
        if key is None:
            continue
        key_text = str(key)
        if key_text in result:
            raise WorkbookValidationError(f"Workbook metadata key {key_text!r} is duplicated.")
        result[key_text] = values.get(2)
    return result


def _data_lines(rows: dict[int, dict[int, object]]) -> list[WorkbookLine]:
    header_row = None
    for row_number in sorted(rows):
        row = rows[row_number]
        if row.get(1) == HEADERS[0]:
            header_row = row_number
            actual_headers = tuple(row.get(index) for index in range(1, len(HEADERS) + 1))
            if actual_headers != HEADERS:
                raise WorkbookValidationError(
                    "Delivery workbook columns were changed; export a fresh copy."
                )
            break
    if header_row is None:
        raise WorkbookValidationError("The delivery line header row is missing.")

    result: list[WorkbookLine] = []
    for row_number in sorted(number for number in rows if number > header_row):
        row = rows[row_number]
        if all(row.get(index) in {None, ""} for index in range(1, len(HEADERS) + 1)):
            continue
        values = {header: row.get(index) for index, header in enumerate(HEADERS, start=1)}
        result.append(WorkbookLine(row_number=row_number, values=values))
    return result


def _strict_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise WorkbookValidationError(f"{field} must be a whole number.", field=field)
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise WorkbookValidationError(f"{field} must be a whole number.", field=field) from exc
    if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
        raise WorkbookValidationError(f"{field} must be a whole number.", field=field)
    return int(decimal_value)


def _xml_safe_text(value: str) -> str:
    def allowed(character: str) -> bool:
        codepoint = ord(character)
        return (
            character in {"\t", "\n", "\r"}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )

    return "".join(character for character in value if allowed(character))
