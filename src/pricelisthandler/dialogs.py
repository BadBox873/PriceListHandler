from __future__ import annotations

import copy
from decimal import Decimal

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFormLayout,QHBoxLayout,
    QInputDialog,QLabel,QLineEdit,QMessageBox,QPlainTextEdit,QTableWidgetItem,QVBoxLayout)

from .domain import UserError, decimal_value, money, quantity, totals
from .operations import apply_bulk, apply_transfer, bulk_preview, parse_bulk, transfer_preview, transfer_rows
from .widgets import ProductPicker, button, cell, confirm, error_box, fill_tooltips, open_path, table


class BulkDialog(QDialog):
    def __init__(self, store, oid, parent=None):
        super().__init__(parent)
        self.store,self.oid,self.rendering = store,oid,False
        self.setWindowTitle('Массовая вставка — проверка и подтверждение')
        self.resize(1380,850)
        self.book = store.book(store.get(oid)['state']['price'])
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Вставь список. Примеры: 00000016700\t20; Буренка мазь: 20; Буренка мазь — 20 шт.\nНеоднозначные названия, дубли и похожие совпадения подтверждаются вручную.'))
        self.text = QPlainTextEdit(store.get(oid)['state'].get('bulk_text',''))
        self.text.setMaximumHeight(125)
        self.text.setToolTip('Исходный список из Excel, письма или сообщения. Сохраняется в этом черновике. После изменения нажми «Разобрать список».')
        layout.addWidget(self.text)
        bar = QHBoxLayout()
        bar.addWidget(button('Разобрать список','Построить предпросмотр, ничего не добавляя в заказ.',self.parse))
        bar.addWidget(button('Выбрать товар','Полноценный поиск по прайсу и ручное подтверждение выделенной строки.',self.choose))
        bar.addWidget(button('Суммировать дубли','Явно разрешить суммирование строк одного кода. Количество будет проверено по остатку.',self.sum_duplicates))
        bar.addWidget(button('Исключить / вернуть','Исключить выбранную строку из добавления. Так можно выбрать одно количество из дублей.',self.toggle_skip))
        self.mode = QComboBox()
        self.mode.addItems(['Заменить все','Прибавить все'])
        self.mode.setToolTip('Политика для товаров, уже находящихся в заказе. Для исключения меняй способ в отдельной строке.')
        self.mode.activated.connect(self.set_all_mode)
        bar.addWidget(self.mode)
        layout.addLayout(bar)
        self.grid = table(['Исходная строка','Товар / код','Остаток','Количество','Было','Станет','Действие','Статус'],
                          'Предпросмотр. Двойной щелчок по количеству — исправить. Выдели строку для ручного сопоставления.')
        self.grid.setColumnWidth(0,300)
        self.grid.setColumnWidth(1,300)
        self.grid.cellChanged.connect(self.quantity_changed)
        layout.addWidget(self.grid)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        bottom = QHBoxLayout()
        bottom.addWidget(button('Добавить проверенные строки','Покажет итоговое подтверждение. Проблемные строки останутся; обработанные повторно не добавятся.',self.apply))
        bottom.addStretch()
        bottom.addWidget(button('Закрыть и сохранить','Сохранить незавершённую вставку в этом черновике.',self.accept))
        layout.addLayout(bottom)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.save_text)
        self.text.textChanged.connect(lambda:self.timer.start())
        self.refresh()
        fill_tooltips(self)

    def state(self):
        return self.store.get(self.oid)['state']

    def save_text(self):
        state = copy.deepcopy(self.state())
        state['bulk_text'] = self.text.toPlainText()
        self.store.change(self.oid,state)

    def done(self,result):
        try:
            self.timer.stop()
            self.save_text()
        except Exception as exc:
            error_box(self,exc)
            return
        super().done(result)

    def parse(self):
        try:
            state = copy.deepcopy(self.state())
            if state.get('bulk') and not confirm(self,'Заново разобрать список?','Текущая разметка будет заменена. Уже добавленные количества остаются в заказе. Новые строки будут считаться новой операцией.'):
                return
            state['bulk_text'] = self.text.toPlainText()
            state['bulk'] = parse_bulk(state['bulk_text'],self.book,self.store.aliases())
            self.store.change(self.oid,state)
            self.refresh()
        except Exception as exc:
            error_box(self,exc)

    def refresh(self):
        self.rendering = True
        state = self.state()
        rows = state['bulk']
        proposed,accepted,errors = bulk_preview(self.book,state['items'],rows)
        self.grid.setRowCount(len(rows))
        for i,row in enumerate(rows):
            p = self.book.by_code.get(row['code'])
            status = 'Уже добавлено' if row.get('processed') else 'Исключено' if row.get('skip') else errors.get(row['id'],'Проверено')
            if status == 'Проверено' and p is not None and p.price is None:
                status = 'Цена не указана — общая сумма неполная'
            values = [row['raw'],f'{p.name}\n{p.code}' if p else 'Товар не выбран',
                      str(p.stock) if p and p.stock is not None else 'Неизвестен',row['qty'],
                      state['items'].get(row['code'],'—'),proposed.get(row['code'],'—'),'',status]
            for j,v in enumerate(values):
                item = cell(v,row['id'] if j==0 else None)
                if j==3 and not row.get('processed'):
                    item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEditable)
                    item.setToolTip('Положительное целое количество. Изменение сохраняется сразу.')
                self.grid.setItem(i,j,item)
            mode = QComboBox()
            mode.addItems(['Заменить','Прибавить'])
            mode.setCurrentIndex(1 if row.get('mode')=='add' else 0)
            mode.setEnabled(not row.get('processed'))
            mode.setToolTip('Индивидуальное исключение из общего способа: заменить количество либо прибавить к текущему.')
            mode.activated.connect(lambda index,rid=row['id']:self.update_row(rid,mode='add' if index else 'replace'))
            self.grid.setCellWidget(i,6,mode)
        self.summary.setText(f'Готово к добавлению: {len(accepted)} строк. Требуют исправления: {len(errors)}. Обработано: {sum(bool(r.get("processed")) for r in rows)}.')
        self.rendering = False

    def selected_id(self):
        i = self.grid.currentRow()
        return self.grid.item(i,0).data(Qt.ItemDataRole.UserRole) if i>=0 else None

    def update_row(self,rid,**fields):
        try:
            state = copy.deepcopy(self.state())
            for row in state['bulk']:
                if row['id']==rid and not row.get('processed'):
                    row.update(fields)
            self.store.change(self.oid,state)
            self.refresh()
        except Exception as exc:
            error_box(self,exc)

    def quantity_changed(self,i,j):
        if self.rendering or j!=3:
            return
        rid = self.grid.item(i,0).data(Qt.ItemDataRole.UserRole)
        self.update_row(rid,qty=self.grid.item(i,j).text().strip())

    def choose(self):
        rid = self.selected_id()
        row = next((r for r in self.state()['bulk'] if r['id']==rid),None)
        if not row or row.get('processed'):
            return
        dialog = ProductPicker(self.book,row.get('identifier',''),row.get('candidates',[]),self)
        if dialog.exec():
            self.update_row(rid,code=dialog.selected.code,confirmed=True,reason='')
            self.store.save_alias(row.get('identifier') or row['raw'],dialog.selected.code)

    def sum_duplicates(self):
        rid = self.selected_id()
        state = copy.deepcopy(self.state())
        selected = next((r for r in state['bulk'] if r['id']==rid),None)
        if not selected or not selected['code']:
            return
        code = selected['code']
        rows = [r for r in state['bulk'] if r['code']==code and not r.get('processed') and not r.get('skip')]
        if len(rows)<2:
            return
        if confirm(self,'Суммировать дубли?',f'Сложить количества {len(rows)} строк с кодом {code}? Итог появится в колонке «Станет».'):
            for r in rows:
                r['duplicate']='sum'
            self.store.change(self.oid,state)
            self.refresh()

    def toggle_skip(self):
        rid = self.selected_id()
        row = next((r for r in self.state()['bulk'] if r['id']==rid),None)
        if row:
            self.update_row(rid,skip=not row.get('skip'))

    def set_all_mode(self,index):
        state = copy.deepcopy(self.state())
        for r in state['bulk']:
            if not r.get('processed'):
                r['mode'] = 'add' if index else 'replace'
        self.store.change(self.oid,state)
        self.refresh()

    def apply(self):
        try:
            self.save_text()
            state = self.state()
            proposed,accepted,errors = bulk_preview(self.book,state['items'],state['bulk'])
            lines = [f'{c}: {state["items"].get(c,"0")} → {v}' for c,v in proposed.items()]
            price_warning = '\nЕсть товары без цены: общая сумма будет неполной.' if any(self.book.by_code[c].price is None for c in proposed) else ''
            if not accepted:
                raise UserError('Нет проверенных строк. Исправь ошибки или подтверди соответствия.')
            box = QMessageBox(QMessageBox.Icon.Question,'Подтвердить массовое добавление',
                f'Добавить {len(accepted)} проверенных строк ({len(proposed)} товаров)?\nПроблемных строк остаётся: {len(errors)}.'+price_warning,parent=self)
            box.setDetailedText('\n'.join(lines))
            ok = box.addButton('Добавить',QMessageBox.ButtonRole.AcceptRole)
            box.addButton('Отмена',QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is ok:
                apply_bulk(self.store,self.oid)
                self.refresh()
        except Exception as exc:
            error_box(self,exc)


class TransferDialog(QDialog):
    def __init__(self,store,oid,new_book,parent=None):
        super().__init__(parent)
        self.store,self.oid,self.new = store,oid,new_book
        state = store.get(oid)['state']
        self.old = store.book(state['price'])
        self.rows = transfer_rows(self.old,new_book,state['items'])
        self.rendering = False
        self.setWindowTitle('Перенос на обновлённый прайс')
        self.resize(1330,720)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Количество сохраняется. Проверь изменения названий, цен и остатков. Пропавшие коды сопоставь вручную.\nНовый прайс: '+new_book.path.name))
        self.grid = table(['Старый товар','Новый товар','Количество','Старая цена','Новая цена','Новый остаток','Проверка'],
                          'Перенос атомарный. Старый прайс и история сохраняются; Ctrl+Z отменяет весь перенос.')
        self.grid.setColumnWidth(0,310)
        self.grid.setColumnWidth(1,310)
        self.grid.cellChanged.connect(self.changed)
        layout.addWidget(self.grid)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        bar = QHBoxLayout()
        bar.addWidget(button('Выбрать соответствие','Выбрать новый товар по всему прайсу и подтвердить изменённый код.',self.choose))
        bar.addWidget(button('Удалить / вернуть позицию','Исключить позицию из нового заказа или вернуть к проверке.',self.remove))
        bar.addStretch()
        self.apply_button = button('Подтвердить перенос','Применить новые цены и остатки одной операцией. До исправления ошибок перенос недоступен.',self.apply)
        bar.addWidget(self.apply_button)
        bar.addWidget(button('Отмена','Отменить перенос; исходный черновик сохранится.',self.reject))
        layout.addLayout(bar)
        self.refresh()
        fill_tooltips(self)

    def refresh(self):
        self.rendering = True
        items,errors = transfer_preview(self.old,self.new,self.rows)
        self.grid.setRowCount(len(self.rows))
        for i,r in enumerate(self.rows):
            old,new = self.old.by_code[r['old_code']], self.new.by_code.get(r['code'])
            warning = []
            if new:
                if old.name!=new.name or old.detail!=new.detail:
                    warning.append('Изменились название / характеристика')
                if old.price!=new.price:
                    warning.append('Изменилась цена')
                if new.price is None:
                    warning.append('Цена не указана; сумма неполная')
            status = 'Удалена из переноса' if r.get('removed') else errors.get(i) or '; '.join(warning) or 'Проверено по коду'
            values = [f'{old.name} [{old.code}]',f'{new.name} [{new.code}]' if new else 'Товар не найден',r['qty'],
                      money(old.price),money(new.price) if new else '—',str(new.stock) if new and new.stock is not None else 'Неизвестен',status]
            for j,v in enumerate(values):
                item = cell(v)
                if j==2 and not r.get('removed'):
                    item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEditable)
                self.grid.setItem(i,j,item)
        self.summary.setText(f'Найдено по коду: {sum(r["old_code"]==r["code"] for r in self.rows)}; проблем: {len(errors)}; останется товаров: {len(items)}.')
        self.apply_button.setEnabled(not errors and bool(items))
        self.rendering = False

    def changed(self,i,j):
        if not self.rendering and j==2:
            self.rows[i]['qty'] = self.grid.item(i,j).text().strip()
            self.refresh()

    def choose(self):
        i = self.grid.currentRow()
        if i<0:
            return
        row = self.rows[i]
        dialog = ProductPicker(self.new,self.old.by_code[row['old_code']].name,row['candidates'],self)
        if dialog.exec():
            row.update(code=dialog.selected.code,confirmed=True,removed=False)
            self.refresh()

    def remove(self):
        i = self.grid.currentRow()
        if i>=0:
            self.rows[i]['removed'] = not self.rows[i].get('removed')
            self.refresh()

    def apply(self):
        try:
            if confirm(self,'Применить перенос?','Будут использованы новые цены и остатки. Весь перенос можно отменить Ctrl+Z. Неподтверждённая массовая вставка старого прайса будет очищена.'):
                apply_transfer(self.store,self.oid,self.new,self.rows)
                self.accept()
        except Exception as exc:
            error_box(self,exc)


class DictionaryDialog(QDialog):
    def __init__(self,store,book=None,parent=None):
        super().__init__(parent)
        self.store,self.book = store,book
        self.setWindowTitle('Словарь соответствий ХИМБЭСТ')
        self.resize(850,600)
        layout = QVBoxLayout(self)
        self.query = QLineEdit()
        self.query.setToolTip('Поиск по альтернативному названию и коду товара.')
        layout.addWidget(self.query)
        self.grid = table(['Альтернативное название','Код товара'],'Словарь предлагает ранее подтверждённые соответствия. Каждая массовая вставка требует повторного подтверждения.')
        layout.addWidget(self.grid)
        bar = QHBoxLayout()
        bar.addWidget(button('Добавить','Создать соответствие для последующих массовых вставок.',lambda:self.edit(False)))
        bar.addWidget(button('Изменить','Изменить запись; существующие заказы остаются прежними.',lambda:self.edit(True)))
        bar.addWidget(button('Удалить','Удалить выбранное соответствие после подтверждения.',self.remove))
        bar.addStretch()
        bar.addWidget(button('Закрыть','Закрыть менеджер словаря.',self.accept))
        layout.addLayout(bar)
        self.query.textChanged.connect(self.refresh)
        self.refresh()
        fill_tooltips(self)

    def refresh(self):
        self.rows = self.store.aliases(self.query.text())
        self.grid.setRowCount(len(self.rows))
        for i,r in enumerate(self.rows):
            self.grid.setItem(i,0,cell(r['alias']))
            self.grid.setItem(i,1,cell(r['code']))

    def edit(self,existing):
        try:
            i = self.grid.currentRow()
            if existing and i<0:
                return
            row = self.rows[i] if existing else {'alias':'','code':''}
            name,ok = QInputDialog.getText(self,'Альтернативное название','Название из вставляемого списка:',text=row['alias'])
            if not ok:
                return
            if self.book:
                picker = ProductPicker(self.book,row['code'] or name,parent=self)
                if not picker.exec():
                    return
                code = picker.selected.code
            else:
                code,ok = QInputDialog.getText(self,'Код товара','Точный код с ведущими нулями:',text=row['code'])
                if not ok:
                    return
            self.store.save_alias(name,code,row['alias'] if existing else None)
            self.refresh()
        except Exception as exc:
            error_box(self,exc)

    def remove(self):
        i = self.grid.currentRow()
        if i>=0 and confirm(self,'Удалить соответствие?',self.rows[i]['alias']+' → '+self.rows[i]['code']):
            self.store.delete_alias(self.rows[i]['alias'])
            self.refresh()


class ExportHistoryDialog(QDialog):
    def __init__(self,store,oid=None,parent=None):
        super().__init__(parent)
        self.setWindowTitle('История экспортов')
        self.resize(1180,560)
        self.rows = store.exports(oid)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Сохраняются дата, путь и итог. Дополнительных копий экспортированных XLSX внутри программы нет.'))
        self.grid = table(['Дата и время','Путь XLSX','Позиций','Известная сумма','Статус файла'],'Выдели строку, чтобы открыть существующий файл или показать его в папке.')
        self.grid.setColumnWidth(0,180)
        self.grid.setColumnWidth(1,560)
        self.grid.setRowCount(len(self.rows))
        from pathlib import Path
        for i,r in enumerate(self.rows):
            values = [r['date'],r['path'],r['count'],money(Decimal(r['total']))+(' (неполная)' if r['incomplete'] else ''),
                      'Доступен' if Path(r['path']).is_file() else 'Перемещён / удалён']
            for j,v in enumerate(values):
                self.grid.setItem(i,j,cell(v))
        layout.addWidget(self.grid)
        bar = QHBoxLayout()
        bar.addWidget(button('Открыть файл','Открывает файл по записанному пути; не запускается автоматически.',lambda:self.open(False)))
        bar.addWidget(button('Показать в папке','Показывает существующий экспорт в Проводнике.',lambda:self.open(True)))
        bar.addStretch()
        bar.addWidget(button('Готово','Закрыть историю экспортов.',self.accept))
        layout.addLayout(bar)
        fill_tooltips(self)

    def open(self,folder):
        try:
            i = self.grid.currentRow()
            if i>=0:
                open_path(self,self.rows[i]['path'],folder)
        except Exception as exc:
            error_box(self,exc)
