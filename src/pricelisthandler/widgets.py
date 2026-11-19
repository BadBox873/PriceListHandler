from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QAbstractTableModel, QObject, QRunnable, Qt, Signal, Slot, QTimer, QThreadPool
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import QUrl

from .domain import UserError, money, search


def button(text, tip, callback):
    b = QPushButton(text)
    b.setToolTip(tip)
    b.clicked.connect(callback)
    return b


def cell(value, data=None):
    item = QTableWidgetItem(str(value))
    item.setToolTip(str(value))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


def table(headers, tip):
    t = QTableWidget(0,len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setToolTip(tip)
    t.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QTableView.SelectionMode.SingleSelection)
    t.setAlternatingRowColors(True)
    t.setWordWrap(False)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    t.setColumnWidth(0,360)
    return t


def fill_tooltips(root):
    for w in root.findChildren(QWidget):
        if w.toolTip():
            continue
        label = w.accessibleName() or (w.text() if hasattr(w,'text') else '')
        if not isinstance(label,str):
            label = ''
        if not label and w.parentWidget():
            label = w.parentWidget().toolTip()
        w.setToolTip(label or 'Управление окном PriceListHandler. Tab — следующее поле; Escape — закрыть диалог.')


def error_box(parent, error):
    QMessageBox.warning(parent,'Не удалось выполнить действие',str(error))


def confirm(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Question,title,text,parent=parent)
    yes = box.addButton('Подтвердить',QMessageBox.ButtonRole.AcceptRole)
    box.addButton('Отмена',QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(QMessageBox.StandardButton.NoButton)
    box.exec()
    return box.clickedButton() is yes


def open_path(parent, path, folder=False):
    from pathlib import Path
    import os, subprocess
    path = Path(path)
    if not path.exists():
        raise UserError('Файл перемещён или удалён. В истории хранится только путь, отдельной копии экспорта нет.')
    if folder and os.name=='nt':
        subprocess.Popen(['explorer.exe','/select,',str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if folder else path)))


class JobSignals(QObject):
    success = Signal(object)
    failure = Signal(object)
    finished = Signal()


class Job(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.signals = JobSignals()
        self.function = function

    @Slot()
    def run(self):
        try:
            self.signals.success.emit(self.function())
        except Exception as exc:
            self.signals.failure.emit(exc)
        finally:
            self.signals.finished.emit()


class ProductModel(QAbstractTableModel):
    headers = ['Товар','Характеристика','Код','Штрихкод','Цена','Остаток','В заказе','Заказать']

    def __init__(self, callback=None, parent=None):
        super().__init__(parent)
        self.products = []
        self.items = {}
        self.callback = callback
        self.editable = callback is not None
        self.sort_column = 0
        self.sort_order = Qt.SortOrder.AscendingOrder

    def set_products(self, products, items):
        self.beginResetModel()
        self.products, self.items = list(products),dict(items)
        self.endResetModel()
        self.sort(self.sort_column,self.sort_order)

    def rowCount(self, parent=None):
        return 0 if parent and parent.isValid() else len(self.products)

    def columnCount(self, parent=None):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation==Qt.Orientation.Horizontal and role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole):
            return self.headers[section] if role==Qt.ItemDataRole.DisplayRole else self.headers[section]+' — данные исходного прайса. Меняется только количество в «Заказать».'
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row()>=len(self.products):
            return None
        p = self.products[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.EditRole):
            return [p.name,p.detail,p.code,p.barcode,money(p.price),str(p.stock) if p.stock is not None else 'Неизвестен',
                    self.items.get(p.code,''),''][index.column()]
        if role==Qt.ItemDataRole.ToolTipRole:
            return f'{p.name}\nКод: {p.code}; строка Excel: {p.row}\n{money(p.price)}; остаток: {p.stock if p.stock is not None else "неизвестен"}\nВведи положительное целое в «Заказать» и нажми Enter. Количество проверяется по остатку.'
        if role==Qt.ItemDataRole.BackgroundRole and p.code in self.items:
            return QApplication.palette().alternateBase()
        if role==Qt.ItemDataRole.TextAlignmentRole and index.column()>=4:
            return int(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self,index):
        flags = super().flags(index)
        if self.editable and index.column()==7:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self,index,value,role=Qt.ItemDataRole.EditRole):
        if role==Qt.ItemDataRole.EditRole and index.column()==7 and self.editable and str(value).strip():
            code = self.products[index.row()].code
            QTimer.singleShot(0,lambda:self.callback(code,str(value)))
            return True
        return False

    def sort(self,column,order=Qt.SortOrder.AscendingOrder):
        self.sort_column,self.sort_order = column,order
        if column==7:
            return
        def key(p):
            if column in (4,5):
                v = p.price if column==4 else p.stock
                return (v is None, v or Decimal(0),p.row)
            if column==6:
                try:
                    return (Decimal(self.items.get(p.code,'0')),p.row)
                except Exception:
                    return (Decimal(-1),p.row)
            return ([p.name.casefold(),p.detail.casefold(),p.code,p.barcode][column],p.row)
        self.layoutAboutToBeChanged.emit()
        self.products.sort(key=key,reverse=order==Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()


class ProductPicker(QDialog):
    def __init__(self, book, query='', candidates=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle('Найти и подтвердить товар')
        self.resize(1120,620)
        self.book,self.selected = book,None
        self.jobs = set()
        self.generation = 0
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Выбери точный товар. Похожие названия и сохранённые соответствия требуют подтверждения.'))
        self.query = QLineEdit(query)
        self.query.setToolTip('Поиск по всему прайсу: название, код, штрихкод, слова в любом порядке.')
        layout.addWidget(self.query)
        self.view = QTableView()
        self.model = ProductModel(parent=self)
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(True)
        self.view.setColumnWidth(0,450)
        self.view.setToolTip('Полный прайс. Двойной щелчок или кнопка подтверждения выбирает выделенный товар.')
        layout.addWidget(self.view)
        self.note = QLabel()
        layout.addWidget(self.note)
        buttons = QDialogButtonBox()
        buttons.addButton('Подтвердить товар',QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(self.choose)
        buttons.addButton('Отмена',QDialogButtonBox.ButtonRole.RejectRole).clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.find)
        self.query.textChanged.connect(lambda:self.timer.start())
        self.view.doubleClicked.connect(self.choose)
        if candidates:
            self.model.set_products([book.by_code[c] for c in candidates if c in book.by_code],{})
            self.note.setText('Предложенные соответствия. Для поиска по всему прайсу измени запрос.')
        else:
            self.find()
        fill_tooltips(self)

    def find(self):
        self.generation += 1
        generation,query = self.generation,self.query.text()
        job = Job(lambda:search(self.book.products,query))
        self.jobs.add(job)
        self.note.setText('Ищу по прайсу…')
        def found(result):
            if generation != self.generation:
                return
            products,fuzzy = result
            self.model.set_products(products,{})
            self.note.setText(f'{"Похожие, проверь вручную" if fuzzy else "Найдено"}: {len(products)}')
        job.signals.success.connect(found)
        job.signals.failure.connect(lambda e:self.note.setText(str(e)))
        job.signals.finished.connect(lambda:self.jobs.discard(job))
        QThreadPool.globalInstance().start(job)

    def choose(self,*args):
        index = self.view.currentIndex()
        if not index.isValid():
            return
        self.selected = self.model.products[index.row()]
        self.accept()
