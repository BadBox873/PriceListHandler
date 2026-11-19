from __future__ import annotations

import copy
import re

from .domain import UserError, normalize, quantity, search


def parse_bulk(text, book, aliases=()):
    rows = []
    known_aliases = {normalize(r['alias']):r['code'] for r in aliases}
    for index,raw in enumerate(text.splitlines()):
        if not raw.strip():
            continue
        line = raw.strip()
        # Last field is a quantity only with a clear separator. A bare trailing
        # digit in a real product name remains ambiguous and requires review.
        m = re.fullmatch(r'(.+?)(?:\t+|\s*[;:]\s*|\s+-\s+)([0-9]+)\s*(?:шт\.?)?',line,re.I)
        explicit_units = re.fullmatch(r'(.+?)\s+([0-9]+)\s*шт\.?',line,re.I)
        plain = re.fullmatch(r'(.+?)\s+([0-9]+)',line)
        m = m or explicit_units
        ambiguous = False
        if m is None and plain:
            candidate = plain[1].strip()
            # Code + quantity is unambiguous; names with trailing digits require manual confirmation.
            m = plain
            ambiguous = candidate not in book.by_code
        identifier = m[1].strip() if m else line
        count = m[2] if m else ''
        exact = [p for p in book.products if p.code == identifier or normalize(p.name) == normalize(identifier)]
        direct, fuzzy = search(book.products,identifier)
        code = exact[0].code if len(exact)==1 else ''
        confirmed = bool(code and not ambiguous)
        reason = '' if confirmed else ('Неоднозначный разбор: проверь название и количество' if ambiguous else 'Выбери и подтверди товар вручную')
        if not count:
            reason = 'Количество не распознано — укажи его вручную'
        alias_code = known_aliases.get(normalize(identifier))
        suggested = [p.code for p in direct[:30]]
        if alias_code in book.by_code:
            suggested = [alias_code] + [c for c in suggested if c!=alias_code]
        if not code and not fuzzy and len(direct)==1:
            # A partial-name single hit is still a suggestion, not an automatic match.
            suggested = [direct[0].code]
        rows.append({'id':str(index),'raw':raw,'identifier':identifier,'qty':count,'code':code,
                     'confirmed':confirmed,'processed':False,'skip':False,'mode':'replace',
                     'duplicate':'unresolved','candidates':suggested,'reason':reason})
    return rows


def bulk_preview(book, items, rows):
    groups = {}
    errors = {}
    for row in rows:
        if row.get('processed') or row.get('skip'):
            continue
        rid = row['id']
        if not row.get('confirmed') or row.get('code') not in book.by_code:
            errors[rid] = row.get('reason') or 'Подтверди товар'
            continue
        try:
            quantity(row.get('qty',''))
        except UserError as exc:
            errors[rid] = str(exc)
            continue
        groups.setdefault(row['code'],[]).append(row)
    proposed,accepted = {},[]
    for code,group in groups.items():
        if len(group)>1 and any(r.get('duplicate')!='sum' for r in group):
            for row in group:
                errors[row['id']] = 'Дубль: явно суммируй либо исключи лишнюю строку'
            continue
        modes = {r.get('mode','replace') for r in group}
        if len(modes)>1:
            for r in group:
                errors[r['id']] = 'Для дублей выбери одинаковый способ добавления'
            continue
        amount = sum(quantity(r['qty']) for r in group)
        if code in items and modes=={'add'}:
            try:
                amount += quantity(items[code])
            except UserError:
                for r in group:
                    errors[r['id']] = 'Исправь исходное количество в заказе или выбери замену'
                continue
        error = book.by_code[code].error(amount)
        if error:
            for r in group:
                errors[r['id']] = error
            continue
        proposed[code] = str(amount)
        accepted.extend(r['id'] for r in group)
    return proposed,accepted,errors


def apply_bulk(store, oid):
    order = store.get(oid)
    state = copy.deepcopy(order['state'])
    book = store.book(state['price'])
    proposed,accepted,errors = bulk_preview(book,state['items'],state['bulk'])
    if not accepted:
        raise UserError('Нет проверенных строк для добавления.')
    state['items'].update(proposed)
    for row in state['bulk']:
        if row['id'] in accepted:
            row['processed'] = True
    # The processed markers and order quantities share one SQLite transaction and one Undo step.
    store.change(oid,state,expected=order['state'])
    return accepted,errors


def transfer_rows(old_book, new_book, items):
    rows = []
    for code,count in items.items():
        old = old_book.by_code[code]
        new = new_book.by_code.get(code)
        candidates = []
        if not new:
            same_barcode = [p for p in new_book.products if old.barcode and p.barcode==old.barcode]
            found,_ = search(new_book.products,old.name+' '+old.detail)
            candidates = list(dict.fromkeys(p.code for p in same_barcode+found[:20]))
        rows.append({'old_code':code,'code':code if new else '', 'qty':count,'removed':False,
                     'confirmed':bool(new),'candidates':candidates})
    return rows


def transfer_preview(old_book, new_book, rows):
    result,errors,seen = {},{},{}
    for index,row in enumerate(rows):
        if row.get('removed'):
            continue
        code = row.get('code','')
        if not row.get('confirmed') or code not in new_book.by_code:
            errors[index] = 'Товар пропал или изменил код — требуется ручной выбор'
            continue
        if code in seen:
            errors[index] = errors[seen[code]] = 'Два старых товара сопоставлены одному новому'
        seen[code] = index
        error = new_book.by_code[code].error(row.get('qty',''))
        if error:
            errors[index] = error
        result[code] = str(row.get('qty',''))
    return result,errors


def apply_transfer(store, oid, new_book, rows):
    order = store.get(oid)
    old = store.book(order['state']['price'])
    items,errors = transfer_preview(old,new_book,rows)
    if errors:
        raise UserError('Перенос заблокирован: исправь все проблемные строки.')
    if not items:
        raise UserError('После переноса заказ пуст. Отмени перенос или выбери товары.')
    if new_book.imported:
        raise UserError('Для переноса требуется незаполненный новый прайс: в его G уже есть заказ. Импортируй его отдельным черновиком.')
    sha = store.register_price(new_book)
    state = copy.deepcopy(order['state'])
    state.update(price=sha,items=items,bulk=[],bulk_text='')
    store.change(oid,state,expected=order['state'])
