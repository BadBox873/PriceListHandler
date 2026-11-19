"""Fail-closed OOXML reader and surgical cell patcher. Never reserialize a sheet."""
from __future__ import annotations

import copy
import hashlib
import os
import posixpath
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET

from .domain import Product, UserError, decimal_value, quantity

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
TAG = "{" + NS + "}"
HEADERS = ["Номенклатура", "Характеристика", "Код", "Штрих-код", "Спец. цена", "Остаток", "Заказано"]
CELL = re.compile(rb'<c(?=[\s>])[^>]*?(?:/\s*>|>.*?</c\s*>)', re.DOTALL)
ADDR = re.compile(rb'\br\s*=\s*([\x22\x27])([A-Z]+[1-9][0-9]*)\1')
TYPE = re.compile(rb'\s+t\s*=\s*([\x22\x27])[^\x22\x27]*\1')
MAX_PART = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def xml(data: bytes):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise UserError("XML с DTD или сущностями не поддерживается.")
    try:
        return ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as exc:
        raise UserError("Повреждённый или небезопасный XML в XLSX.") from exc


def read_package(path: Path) -> tuple[dict[str, bytes], list, bytes]:
    try:
        with ZipFile(path) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(infos) > 2000 or len(set(names)) != len(names):
                raise UserError("Слишком много или дублирующиеся части XLSX.")
            if sum(i.file_size for i in infos) > MAX_TOTAL:
                raise UserError("Распакованный XLSX превышает 256 МиБ.")
            for i in infos:
                p = PurePosixPath(i.filename)
                if p.is_absolute() or ".." in p.parts or "\\" in i.filename or ":" in i.filename or i.flag_bits & 1:
                    raise UserError("Небезопасный путь или шифрование в XLSX.")
                if i.file_size > MAX_PART:
                    raise UserError("Часть XLSX превышает 64 МиБ.")
            parts = {i.filename: z.read(i) for i in infos}
            for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"):
                if required not in parts:
                    raise UserError(f"В XLSX отсутствует обязательная часть: {required}")
            for name, data in parts.items():
                if name.endswith((".xml", ".rels", ".vml")):
                    tree = xml(data)
                    if name.endswith(".rels"):
                        base = "" if name == "_rels/.rels" else posixpath.dirname(posixpath.dirname(name))
                        ids = set()
                        for rel in tree:
                            if rel.get("Id") in ids:
                                raise UserError(f"Повторяющийся идентификатор связи: {name}")
                            ids.add(rel.get("Id"))
                            if rel.get("TargetMode") == "External":
                                continue  # Preserved, never fetched or executed.
                            target = rel.get("Target", "")
                            dest = posixpath.normpath(posixpath.join(base, target)) if not target.startswith("/") else target[1:]
                            if dest not in parts:
                                raise UserError(f"Повреждённая связь XLSX: {name} → {target}")
            return parts, infos, z.comment
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise UserError(f"Не удалось прочитать XLSX: {exc}") from exc


@dataclass
class Workbook:
    path: Path
    sha256: str
    parts: dict[str, bytes]
    infos: list
    comment: bytes
    sheet: str
    sheet_name: str
    products: list[Product]
    cells: dict[str, bytes]
    by_code: dict[str, Product] = field(init=False)

    def __post_init__(self):
        self.by_code = {p.code: p for p in self.products}

    @property
    def imported(self) -> dict[str, str]:
        return {p.code: (p.original if p.original_type in ("n", "") else f"Тип {p.original_type}: {p.original}")
                for p in self.products if p.original is not None}

    def errors(self, items: dict[str, str]) -> dict[str, str]:
        errors = {}
        for code, value in items.items():
            if code not in self.by_code:
                errors[code] = "Товар отсутствует в привязанном прайсе"
                continue
            p = self.by_code[code]
            err = p.error(value, imported=True)
            if err:
                errors[code] = err
        return errors


def load_workbook(path: str | Path) -> Workbook:
    path = Path(path).resolve()
    parts, infos, comment = read_package(path)
    book = xml(parts["xl/workbook.xml"])
    sheets = book.findall(f"{TAG}sheets/{TAG}sheet")
    if len(sheets) != 1:
        raise UserError("Профиль ХИМБЭСТ требует ровно один лист. Нужна проверка нового формата.")
    if any(n.lower().endswith("vbaproject.bin") for n in parts):
        raise UserError("Книги с макросами не поддерживаются.")
    rid = sheets[0].get("{" + REL + "}id")
    relationships = xml(parts["xl/_rels/workbook.xml.rels"])
    targets = [r.get("Target") for r in relationships if r.get("Id") == rid and r.get("Type", "").endswith("/worksheet")]
    if len(targets) != 1:
        raise UserError("Неоднозначная связь рабочего листа.")
    target = targets[0]
    sheet = posixpath.normpath(posixpath.join("xl", target)) if not target.startswith("/") else target[1:]
    if sheet not in parts:
        raise UserError("Не найден XML рабочего листа.")
    strings = []
    if "xl/sharedStrings.xml" in parts:
        for si in xml(parts["xl/sharedStrings.xml"]).findall(TAG + "si"):
            strings.append("".join(t.text or "" for t in si.iter(TAG + "t")))

    def value(c):
        t = c.get("t", "n")
        v = c.find(TAG + "v")
        if t == "inlineStr":
            return "".join(n.text or "" for n in c.iter(TAG + "t"))
        raw = None if v is None else v.text
        if raw is None:
            return None
        if t == "s":
            try:
                index = int(raw)
                if index < 0:
                    raise ValueError
                return strings[index]
            except (ValueError, IndexError) as exc:
                raise UserError("Повреждённая таблица строк XLSX.") from exc
        return raw

    raw_sheet = parts[sheet]
    root = xml(raw_sheet)
    if root.tag != TAG + "worksheet" or b"<c " not in raw_sheet:
        raise UserError("Неизвестное XML-представление листа. Экспорт заблокирован.")
    cell_nodes = root.findall(f"{TAG}sheetData/{TAG}row/{TAG}c")
    raw_cells = {}
    for m in CELL.finditer(raw_sheet):
        addr = ADDR.search(m.group())
        if not addr or addr[2].decode() in raw_cells:
            raise UserError("Повторяющиеся или некорректные адреса ячеек.")
        raw_cells[addr[2].decode()] = m.group()
    if len(raw_cells) != len(cell_nodes):
        raise UserError("Нестандартные XML-ячейки. Безопасность точечного экспорта не доказана.")
    all_nodes = {c.get("r"): c for c in cell_nodes}
    if len(all_nodes) != len(cell_nodes) or set(all_nodes) != set(raw_cells):
        raise UserError("Неоднозначные адреса ячеек XLSX.")
    headers = [value(all_nodes.get(f"{col}1")) if f"{col}1" in all_nodes else None for col in "ABCDEFG"]
    if headers != HEADERS:
        raise UserError("Не совпадают заголовки A1:G1 ХИМБЭСТ. Переставленные колонки не поддерживаются.")
    products, codes = [], set()
    rows = root.findall(f"{TAG}sheetData/{TAG}row")
    row_ids = [r.get("r") for r in rows]
    if len(set(row_ids)) != len(row_ids):
        raise UserError("Повторяющиеся строки XLSX.")
    for row in rows:
        rn = row.get("r", "")
        if not rn.isdigit() or int(rn) < 1:
            raise UserError("Некорректный номер строки.")
        cells = {c.get("r"): c for c in row.findall(TAG + "c")}
        if any(not re.fullmatch(r"[A-Z]+" + rn, address or "") for address in cells):
            raise UserError("Адрес ячейки не совпадает с её строкой.")
        if rn == "1":
            continue
        c = cells.get("C" + rn)
        code = value(c) if c is not None else None
        if code is None or not code.strip() or not code.strip("0 "):
            continue
        if c.get("t", "n") not in ("s", "inlineStr", "str") or c.find(TAG + "f") is not None:
            raise UserError(f"Строка {rn}: код должен быть текстом с сохранением ведущих нулей.")
        if not re.fullmatch(r"[\w.-]+ *", code, re.UNICODE):
            raise UserError(f"Строка {rn}: неподдерживаемый код товара.")
        if code.rstrip() in codes:
            raise UserError(f"Повтор кода {code}. Безопасное сопоставление невозможно.")
        codes.add(code.rstrip())
        def get(col):
            node = cells.get(col + rn)
            return value(node) if node is not None else None
        name = get("A")
        if not name or name.strip().casefold() == "итого":
            raise UserError(f"Строка {rn}: код без корректного названия товара.")
        g = cells.get("G" + rn)
        if g is None or set(g.attrib) - {"r", "s", "t"} or any(n.tag not in (TAG + "v", TAG + "is") for n in g):
            raise UserError(f"G{rn}: отсутствующая ячейка, формула или неизвестное содержимое. Нужна проверка формата.")
        if len(g.findall(TAG + "v")) > 1 or len(g.findall(TAG + "is")) > 1:
            raise UserError(f"G{rn}: неоднозначное значение.")
        for col in "EF":
            node = cells.get(col + rn)
            if node is not None and node.find(TAG + "f") is not None:
                raise UserError(f"{col}{rn}: расчётные цена/остаток не поддерживаются без проверки поставщика.")
        price, stock = decimal_value(get("E")), decimal_value(get("F"))
        if (price is not None and price < 0) or (stock is not None and stock < 0):
            raise UserError(f"Строка {rn}: отрицательная цена или остаток.")
        products.append(Product(int(rn), code, name, get("B") or "", get("D") or "", price, stock, get("G"), g.get("t", "n")))
    if not products:
        raise UserError("В прайсе не найдены товарные строки.")
    return Workbook(path, digest(path.read_bytes()), parts, infos, comment, sheet, sheets[0].get("name", ""), products, raw_cells)


def patch_cell(raw: bytes, desired: str | None) -> bytes:
    # Preserve the opening tag byte-for-byte except the value's type attribute.
    end = raw.index(b">")
    opening = raw[:end].rstrip()
    if opening.endswith(b"/"):
        opening = opening[:-1].rstrip()
    opening = TYPE.sub(b"", opening)
    if desired is None:
        return opening + b"/>"
    n = quantity(desired)
    return opening + b' t="n"><v>' + str(n).encode("ascii") + b"</v></c>"


def changes_for(book: Workbook, items: dict[str, str]) -> dict[str, str | None]:
    if not items:
        raise UserError("Заказ пуст. Добавь хотя бы один товар.")
    errors = book.errors(items)
    if errors:
        raise UserError("Экспорт заблокирован:\n" + "\n".join(f"{c}: {e}" for c, e in errors.items()))
    changes = {}
    for p in book.products:
        desired = items.get(p.code)
        if desired is not None:
            desired = str(int(decimal_value(desired)))
        same = desired == p.original and (desired is None or p.original_type == "n")
        if p.original is not None and p.original_type == "n" and desired is not None:
            same = decimal_value(p.original) == decimal_value(desired)
        if not same:
            changes[f"G{p.row}"] = desired
    return changes


def patch_sheet(book: Workbook, changes: dict[str, str | None]) -> bytes:
    remaining = set(changes)
    def replace(m):
        raw = m.group()
        addr = ADDR.search(raw)[2].decode()
        if addr not in changes:
            return raw
        remaining.remove(addr)
        return patch_cell(raw, changes[addr])
    result = CELL.sub(replace, book.parts[book.sheet])
    if remaining:
        raise UserError("Не все целевые ячейки найдены в XML.")
    return result


def verify_export(book: Workbook, output: Path, items: dict[str, str]) -> dict:
    expected = changes_for(book, items)
    parts, infos, comment = read_package(output)
    if [i.filename for i in infos] != [i.filename for i in book.infos] or comment != book.comment:
        raise UserError("Изменился состав, порядок частей или комментарий ZIP.")
    unchanged = 0
    for name, original in book.parts.items():
        if name != book.sheet:
            if parts[name] != original:
                raise UserError(f"Изменена запрещённая часть XLSX: {name}")
            unchanged += 1
    # Independent diff: strip only the explicit allowed cell spans from both sheets.
    def masked(data):
        result, targets = [], {}
        pos = 0
        for m in CELL.finditer(data):
            addr = ADDR.search(m.group())[2].decode()
            if addr in expected:
                if addr in targets:
                    raise UserError("Повтор целевой ячейки при проверке.")
                result.extend((data[pos:m.start()], ("TARGET:" + addr).encode()))
                targets[addr] = m.group()
                pos = m.end()
        result.append(data[pos:])
        return b"".join(result), targets
    old_mask, old_targets = masked(book.parts[book.sheet])
    new_mask, new_targets = masked(parts[book.sheet])
    if old_mask != new_mask or set(old_targets) != set(expected) or set(new_targets) != set(expected):
        raise UserError("OOXML-проверка: изменены данные вне разрешённых ячеек G.")
    for addr, desired in expected.items():
        old = xml(b'<root xmlns="' + NS.encode() + b'">' + old_targets[addr] + b'</root>')[0]
        new = xml(b'<root xmlns="' + NS.encode() + b'">' + new_targets[addr] + b'</root>')[0]
        if {k:v for k,v in old.attrib.items() if k != "t"} != {k:v for k,v in new.attrib.items() if k != "t"}:
            raise UserError(f"Изменены стиль или адрес {addr}.")
        if desired is None:
            if list(new) or new.get("t") is not None:
                raise UserError(f"Не очищена ячейка {addr}.")
        elif new.get("t") != "n" or len(new) != 1 or new[0].tag != TAG + "v" or new[0].text != str(quantity(desired)):
            raise UserError(f"Неверное итоговое значение {addr}.")
    reread = load_workbook(output)
    if [(p.row,p.code) for p in reread.products] != [(p.row,p.code) for p in book.products] or reread.sheet_name != book.sheet_name:
        raise UserError("Изменились товары, строки, коды или лист.")
    for p in reread.products:
        desired = items.get(p.code)
        if desired is None and p.original is not None:
            raise UserError(f"Не очищен заказ {p.code}.")
        if desired is not None and (p.original_type != "n" or decimal_value(p.original) != decimal_value(desired)):
            raise UserError(f"Неверное количество для {p.code}.")
    return {"source_sha256": book.sha256, "output_sha256": digest(output.read_bytes()), "changed_cells": sorted(expected),
            "unchanged_parts": unchanged, "products": len(reread.products), "verified": True, "compatibility_1c": "not_tested"}


def export_workbook(book: Workbook, items: dict[str, str], output: str | Path,
                    *, replace: bool = False, protected: tuple[Path, ...] = ()) -> dict:
    output = Path(output).resolve()
    if output.suffix.lower() != ".xlsx":
        raise UserError("Выбери файл с расширением .xlsx.")
    if output == book.path or any(output == p.resolve() or (p.exists() and output.exists() and os.path.samefile(output,p)) for p in protected + (book.path,)):
        raise UserError("Исходный прайс и внутреннюю копию заменять запрещено.")
    if output.exists() and not replace:
        raise UserError("Файл уже существует. Выбери копию или явно подтверди замену экспорта.")
    if not output.parent.is_dir():
        raise UserError("Папка экспорта не существует.")
    if digest(book.path.read_bytes()) != book.sha256:
        raise UserError("Неизменяемая копия прайса повреждена. Экспорт остановлен.")
    changes = changes_for(book, items)
    fd, temp = tempfile.mkstemp(prefix=".plh-", suffix=".xlsx", dir=output.parent)
    os.close(fd)
    temp = Path(temp)
    try:
        with ZipFile(temp, "w") as z:
            z.comment = book.comment
            for info in book.infos:
                data = patch_sheet(book, changes) if info.filename == book.sheet else book.parts[info.filename]
                z.writestr(copy.copy(info), data)
        with temp.open("rb+") as f:
            os.fsync(f.fileno())
        report = verify_export(book, temp, items)
        if digest(book.path.read_bytes()) != book.sha256:
            raise UserError("Исходник изменился во время экспорта.")
        if replace:
            os.replace(temp, output)
        else:
            # Hard-link creation cannot silently replace a concurrently created file.
            try:
                if os.name == 'nt':
                    os.rename(temp, output)  # Windows rename refuses an existing destination.
                else:
                    os.link(temp, output)
                    temp.unlink()
            except FileExistsError as exc:
                raise UserError("Имя уже занято другим файлом. Выбери другое имя.") from exc
            except OSError as exc:
                raise UserError("Диск не поддерживает безопасное создание файла без замены. Выбери локальный диск NTFS.") from exc
        return report
    finally:
        temp.unlink(missing_ok=True)
