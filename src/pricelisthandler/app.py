from __future__ import annotations

import argparse
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, QLockFile, QThreadPool, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressDialog, QPushButton, QSplitter, QStackedWidget, QStatusBar, QTableView,
    QTabBar, QTabWidget, QVBoxLayout, QWidget)

from . import __version__
from .adapters import ADAPTERS
from .dialogs import BulkDialog, DictionaryDialog, ExportHistoryDialog, TransferDialog
from .domain import UserError, decimal_value, money, quantity, search, totals
from .ooxml import export_workbook, load_workbook
from .storage import Store
from .widgets import Job, ProductModel, button, cell, confirm, error_box, fill_tooltips, open_path, table

LOG = logging.getLogger('PriceListHandler')


class DiagnosticFormatter(logging.Formatter):
    def format(self,record):
        from datetime import datetime
        data={'time':datetime.now().astimezone().isoformat(timespec='milliseconds'),
              'level':record.levelname,'message':record.getMessage()[:2000]}
        if record.exc_info:
            data['exception']=self.formatException(record.exc_info)[-3000:]
        return json.dumps(data,ensure_ascii=False)


def data_directory(portable=None):
    executable = Path(sys.executable).resolve() if getattr(sys,'frozen',False) else Path(sys.argv[0]).resolve()
    portable = (executable.parent/'portable.flag').exists() if portable is None else portable
    if portable:
        return executable.parent/'Data'
    base = Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))
    return base/'PriceListHandler'


def app_icon():
    pix = QPixmap(256,256)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor('#166b58'))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(8,8,240,240,48,48)
    painter.setBrush(QColor('#f5faf7'))
    painter.drawRoundedRect(62,45,132,166,14,14)
    painter.setPen(QColor('#166b58'))
    painter.setFont(QFont('Segoe UI',68,QFont.Weight.Bold))
    painter.drawText(64,44,130,168,Qt.AlignmentFlag.AlignCenter,'P')
    painter.end()
    return QIcon(pix)


def apply_theme(app, theme):
    app.setStyle('Fusion')
    palette = QPalette()
    dark = theme=='dark' or (theme=='system' and app.styleHints().colorScheme()==Qt.ColorScheme.Dark)
    colors = {
        QPalette.ColorRole.Window:'#202523' if dark else '#f2f5f3',
        QPalette.ColorRole.WindowText:'#e7eeea' if dark else '#17251f',
        QPalette.ColorRole.Base:'#171d1a' if dark else '#ffffff',
        QPalette.ColorRole.AlternateBase:'#293d33' if dark else '#e7f3ec',
        QPalette.ColorRole.Text:'#e7eeea' if dark else '#17251f',
        QPalette.ColorRole.Button:'#303a34' if dark else '#ffffff',
        QPalette.ColorRole.ButtonText:'#e7eeea' if dark else '#17251f',
        QPalette.ColorRole.Highlight:'#23836b' if dark else '#166b58',
        QPalette.ColorRole.HighlightedText:'#ffffff',
        QPalette.ColorRole.ToolTipBase:'#303a34' if dark else '#fffbea',
        QPalette.ColorRole.ToolTipText:'#ffffff' if dark else '#17251f',
        QPalette.ColorRole.PlaceholderText:'#a1b0a7' if dark else '#66776d',
    }
    for role,color in colors.items():
        palette.setColor(role,QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.Text,QColor('#849087'))
    palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.ButtonText,QColor('#849087'))
    app.setPalette(palette)
    app.setStyleSheet('''
    QWidget { font-family: "Segoe UI", "DejaVu Sans"; font-size: 10pt; }
    QPushButton { padding: 7px 12px; border: 1px solid palette(mid); border-radius: 5px; }
    QPushButton:hover { border-color: #23836b; }
    QPushButton:default { background-color: #166b58; color: white; }
    QLineEdit, QComboBox, QDateEdit { padding: 6px; border: 1px solid palette(mid); border-radius: 4px; }
    QHeaderView::section { padding: 7px; border: 0px; border-bottom: 1px solid palette(mid); }
    QTableView { border: 1px solid palette(mid); gridline-color: palette(mid); }
    QTabBar::tab { padding: 9px 18px; }
    QToolTip { padding: 6px; border: 1px solid #81968a; }
    ''')


class MainWindow(QMainWindow):
    def __init__(self,store,*,check_updates=True,recover=True):
        super().__init__()
        self.store,self.oid,self.book,self.readonly = store,None,None,False
        self.adapter = ADAPTERS['khimbest']
        self.jobs = set()
        self.search_generation = 0
        self.list_generation = 0
        self.setWindowTitle(f'PriceListHandler {__version__} — заказы поставщикам')
        self.setWindowIcon(app_icon())
        self.resize(1450,920)
        self.setMinimumSize(960,640)
        self.setStatusBar(QStatusBar())
        root = QWidget()
        outer = QVBoxLayout(root)
        header = QHBoxLayout()
        title = QLabel('PriceListHandler')
        title.setStyleSheet('font-size: 19pt; font-weight: 600;')
        title.setToolTip('Заказы поставщикам. Экспорт изменяет только количества в разрешённых товарных ячейках «Заказано».')
        header.addWidget(title)
        header.addWidget(QLabel('Заказы поставщикам'))
        header.addStretch()
        header.addWidget(button('История экспортов','Дата, путь и сумма каждого успешно проверенного экспорта.',lambda:self.safe(lambda:ExportHistoryDialog(store,parent=self).exec())))
        header.addWidget(button('Словарь','Просмотр и редактирование вручную подтверждённых соответствий.',lambda:self.safe(lambda:DictionaryDialog(store,self.book,self).exec())))
        header.addWidget(button('Настройки','Темы, резервные копии, восстановление и сведения о версии.',self.settings))
        outer.addLayout(header)
        self.suppliers = QTabWidget()
        supplier = QWidget()
        sl = QVBoxLayout(supplier)
        sl.setContentsMargins(0,6,0,0)
        self.stack = QStackedWidget()
        sl.addWidget(self.stack)
        self.suppliers.addTab(supplier,self.adapter.name)
        self.suppliers.setTabToolTip(0,'Адаптер ХИМБЭСТ. Другие поставщики подключаются отдельными проверенными адаптерами.')
        outer.addWidget(self.suppliers)
        self.make_list()
        self.make_editor()
        self.setCentralWidget(root)
        self.statusBar().showMessage('Все изменения черновиков сохраняются автоматически.')
        self.shortcut('Ctrl+Z','Отменить изменение заказа',lambda:self.undo(-1))
        self.shortcut('Ctrl+Y','Повторить отменённое изменение',lambda:self.undo(1))
        self.shortcut('Ctrl+F','Перейти к поиску',self.focus_search)
        self.shortcut('Ctrl+N','Создать заказ',self.new_order)
        self.refresh_list()
        if recover:
            QTimer.singleShot(50,lambda:self.startup())
        if check_updates:
            QTimer.singleShot(1200,self.check_updates)
        fill_tooltips(self)

    def shortcut(self,key,tip,callback):
        action = QAction(tip,self)
        action.setShortcut(QKeySequence(key))
        action.setToolTip(tip+' — '+key)
        action.triggered.connect(callback)
        self.addAction(action)

    def safe(self,callback):
        try:
            return callback()
        except Exception as exc:
            op = uuid.uuid4().hex[:10]
            LOG.exception('operation=%s stage=ui error=%s',op,type(exc).__name__)
            error_box(self,str(exc)+f'\nКод диагностики: {op}')
            return None

    def run_job(self,title,function,success,*,modal=True,on_error=None):
        job = Job(function)
        self.jobs.add(job)
        progress = None
        if modal:
            progress = QProgressDialog(title,'',0,0,self)
            progress.setWindowTitle('PriceListHandler')
            progress.setCancelButton(None)
            progress.setWindowModality(Qt.WindowModality.ApplicationModal)
            progress.setMinimumDuration(0)
            progress.setToolTip('Операция выполняется в фоне. Дождись окончания проверки; закрывать программу сейчас не нужно.')
            progress.show()
        def done(value):
            if progress:
                progress.close()
            self.safe(lambda:success(value))
        def failed(exc):
            if progress:
                progress.close()
            if on_error:
                on_error(exc)
            else:
                op = uuid.uuid4().hex[:10]
                LOG.error('operation=%s stage=%s error=%s',op,title,type(exc).__name__)
                error_box(self,str(exc)+f'\nКод диагностики: {op}')
        job.signals.success.connect(done)
        job.signals.failure.connect(failed)
        job.signals.finished.connect(lambda:self.jobs.discard(job))
        QThreadPool.globalInstance().start(job)

    def make_list(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        bar.addWidget(button('Новый заказ','Ctrl+N. Загрузить прайс и создать отдельный именованный черновик.',self.new_order))
        self.list_query = QLineEdit()
        self.list_query.setPlaceholderText('Название заказа, товар или код внутри заказа')
        self.list_query.setToolTip('Поиск в именах заказов, названиях и кодах их товаров. Все слова должны присутствовать.')
        bar.addWidget(self.list_query,1)
        self.sort = QComboBox()
        for label,key in [('По дате изменения','modified'),('По названию','name'),('По числу позиций','count'),('По сумме','total')]:
            self.sort.addItem(label,key)
        self.sort.setToolTip('Сортировка списка черновиков, архива или корзины.')
        bar.addWidget(self.sort)
        self.descending = QCheckBox('По убыванию')
        self.descending.setChecked(True)
        self.descending.setToolTip('Выключи для сортировки по возрастанию. По умолчанию новые заказы первыми.')
        bar.addWidget(self.descending)
        layout.addLayout(bar)
        filters = QHBoxLayout()
        self.date_filter = QCheckBox('Дата изменения:')
        self.date_filter.setToolTip('Ограничить список заказов диапазоном дат изменения включительно.')
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.end_date = QDateEdit(QDate.currentDate())
        for widget in (self.start_date,self.end_date):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat('dd.MM.yyyy')
            widget.setToolTip('Граница диапазона дат изменения заказов включительно.')
        filters.addWidget(self.date_filter)
        filters.addWidget(self.start_date)
        filters.addWidget(QLabel('—'))
        filters.addWidget(self.end_date)
        filters.addStretch()
        layout.addLayout(filters)
        self.list_tabs = QTabBar()
        for title in ('Черновики','Архив','Корзина'):
            self.list_tabs.addTab(title)
        self.list_tabs.setToolTip('Черновики — рабочие заказы; архив — успешные экспорты; корзина — удалённые записи без автоматической очистки.')
        layout.addWidget(self.list_tabs)
        self.list_grid = table(['Название заказа','Изменён','Позиций','Сумма','Примечание'],
                               'Двойной щелчок открывает заказ. Архив и корзина доступны для просмотра и восстановления.')
        self.list_grid.setColumnWidth(0,450)
        self.list_grid.setColumnWidth(1,220)
        self.list_grid.setColumnWidth(3,180)
        self.list_grid.doubleClicked.connect(self.open_selected)
        layout.addWidget(self.list_grid)
        bar = QHBoxLayout()
        bar.addWidget(button('Открыть','Открыть выбранный черновик либо просмотреть архивный/удалённый заказ.',self.open_selected))
        bar.addWidget(button('Переименовать','Изменить название выбранного заказа.',self.rename_selected))
        self.restore_button = button('Восстановить','Из архива создаётся новый черновик с историей; из корзины возвращается исходная запись.',self.restore_selected)
        bar.addWidget(self.restore_button)
        self.trash_button = button('В корзину','Переместить выбранный заказ в корзину. Внешний XLSX останется на диске.',self.trash_selected)
        bar.addWidget(self.trash_button)
        self.purge_button = button('Удалить навсегда','Необратимо удалить выбранный заказ из корзины после подтверждения.',lambda:self.purge(False))
        bar.addWidget(self.purge_button)
        self.empty_button = button('Очистить корзину','Показать число заказов и оценку места, затем запросить подтверждение полного удаления.',lambda:self.purge(True))
        bar.addWidget(self.empty_button)
        bar.addStretch()
        layout.addLayout(bar)
        self.list_timer = QTimer(self)
        self.list_timer.setSingleShot(True)
        self.list_timer.setInterval(160)
        self.list_timer.timeout.connect(self.refresh_list)
        self.list_query.textChanged.connect(lambda:self.list_timer.start())
        for signal in (self.list_tabs.currentChanged,self.sort.currentIndexChanged,self.descending.toggled,
                       self.date_filter.toggled,self.start_date.dateChanged,self.end_date.dateChanged):
            signal.connect(self.refresh_list)
        self.stack.addWidget(page)

    def list_status(self):
        return ['draft','archive','trash'][self.list_tabs.currentIndex()]

    def refresh_list(self,*args):
        self.list_generation += 1
        generation = self.list_generation
        start = self.start_date.date().toString('yyyy-MM-dd') if self.date_filter.isChecked() else ''
        end = self.end_date.date().toString('yyyy-MM-dd') if self.date_filter.isChecked() else ''
        status,query,sort,descending = self.list_status(),self.list_query.text(),self.sort.currentData(),self.descending.isChecked()
        def loaded(rows):
            if generation != self.list_generation:
                return
            self.list_grid.setRowCount(len(rows))
            for i,row in enumerate(rows):
                values = [row['name'],row['modified'].replace('T',' '),row['count'],money(row['total']),
                          'Сумма неполная' if row['incomplete'] else '']
                for j,value in enumerate(values):
                    self.list_grid.setItem(i,j,cell(value,row['id'] if j==0 else None))
            trash = self.list_status()=='trash'
            self.restore_button.setEnabled(self.list_status()!='draft')
            self.trash_button.setVisible(not trash)
            self.purge_button.setVisible(trash)
            self.empty_button.setVisible(trash)
            self.statusBar().showMessage(f'Заказов: {len(rows)}. Данные: {self.store.root}')
        self.run_job('Читаю список заказов',lambda:self.store.list_orders(status,query,start,end,sort,descending),loaded,modal=False)

    def selected_order(self):
        row = self.list_grid.currentRow()
        return self.list_grid.item(row,0).data(Qt.ItemDataRole.UserRole) if row>=0 else None

    def new_order(self):
        path,_ = QFileDialog.getOpenFileName(self,'Открыть прайс ХИМБЭСТ','','Прайс Excel (*.xlsx)')
        if not path:
            return
        def loaded(book):
            if book.imported:
                box = QMessageBox(QMessageBox.Icon.Warning,'В прайсе уже заполнен заказ',
                    f'Заполненных товарных ячеек G: {len(book.imported)}. Будут импортированы все, включая ошибочные. Ошибки потребуется исправить до экспорта.',parent=self)
                import_button = box.addButton('Импортировать существующий заказ',QMessageBox.ButtonRole.AcceptRole)
                box.addButton('Отменить открытие',QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is not import_button:
                    return
            name,ok = QInputDialog.getText(self,'Новый заказ','Название черновика:',text='ХИМБЭСТ — '+date.today().strftime('%d.%m.%Y'))
            if ok:
                self.run_job('Сохраняю неизменяемую копию прайса…',lambda:self.store.create(name,book),self.open_order)
        self.run_job('Проверяю прайс и структуру XLSX…',lambda:self.adapter.load(path),loaded)

    def open_selected(self,*args):
        oid = self.selected_order()
        if oid:
            self.safe(lambda:self.open_order(oid))

    def rename_selected(self):
        oid = self.selected_order()
        if not oid:
            return
        row = self.store.get(oid)
        name,ok = QInputDialog.getText(self,'Переименовать заказ','Название:',text=row['name'])
        if ok:
            self.safe(lambda:self.store.rename(oid,name))
            self.refresh_list()

    def restore_selected(self):
        oid = self.selected_order()
        if oid:
            result = self.safe(lambda:self.store.restore(oid))
            self.refresh_list()
            if result and self.store.get(result)['status']=='draft':
                self.open_order(result)

    def trash_selected(self):
        oid = self.selected_order()
        if oid and confirm(self,'Переместить заказ в корзину?',self.store.get(oid)['name']+'\nЕго можно будет восстановить вместе с историей и исходным прайсом.'):
            self.safe(lambda:self.store.trash(oid))
            self.refresh_list()

    def purge(self,all_orders):
        oid = None if all_orders else self.selected_order()
        if oid is None and not all_orders:
            return
        count,size = self.store.trash_size(oid)
        if count and confirm(self,'Окончательное удаление',f'Заказов: {count}. Ожидаемое освобождение: около {size/1024/1024:.2f} МиБ (SQLite может сохранить выделенное место).\nУдаление необратимо. Внешние экспорты и резервные копии остаются. Удалить?'):
            self.run_job('Удаляю записи из корзины…',lambda:self.store.purge(oid),lambda _:self.refresh_list())

    def make_editor(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(button('← Заказы','Вернуться к списку. Черновик уже сохранён.',self.back_to_list))
        self.order_title = QLabel()
        self.order_title.setStyleSheet('font-size: 14pt; font-weight: 600;')
        top.addWidget(self.order_title,1)
        self.undo_button = button('Отменить','Ctrl+Z. Отменить изменение; вся история сохраняется после перезапуска.',lambda:self.undo(-1))
        self.redo_button = button('Повторить','Ctrl+Y. Повторить отменённое изменение.',lambda:self.undo(1))
        top.addWidget(self.undo_button)
        top.addWidget(self.redo_button)
        layout.addLayout(top)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setToolTip('Исходный прайс хранится внутри программы побайтово; его контрольная сумма проверяется перед экспортом.')
        layout.addWidget(self.source_label)
        querybar = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText('Название, код, штрихкод — слова в любом порядке')
        self.query.setToolTip('Ctrl+F. Поиск по всем товарам прайса; совпадения по названию, характеристике, коду и штрихкоду. После добавления поиск очищается.')
        querybar.addWidget(self.query,1)
        self.available = QCheckBox('Доступные')
        self.available.setToolTip('Только товары с известным остатком не меньше одной единицы.')
        self.priced = QCheckBox('С ценой')
        self.priced.setToolTip('Скрыть товары без цены; цена не заменяется нулём.')
        self.ordered = QCheckBox('В заказе')
        self.ordered.setToolTip('Показать только уже добавленные товары.')
        for w in (self.available,self.priced,self.ordered):
            querybar.addWidget(w)
            w.toggled.connect(self.schedule_search)
        querybar.addWidget(button('Сбросить','Очистить запрос и отключить все фильтры.',self.reset_search))
        layout.addLayout(querybar)
        self.search_note = QLabel('')
        layout.addWidget(self.search_note)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.results = QTableView()
        self.result_model = ProductModel(self.add_quantity,self)
        self.results.setModel(self.result_model)
        self.results.setAlternatingRowColors(True)
        self.results.setSortingEnabled(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setColumnWidth(0,480)
        self.results.setColumnWidth(1,150)
        self.results.setColumnWidth(2,135)
        self.results.setColumnWidth(3,145)
        self.results.setColumnWidth(4,150)
        self.results.setColumnWidth(5,115)
        self.results.setToolTip('Введи количество в последней колонке и нажми Enter. Двойной щелчок по товару переводит в поле количества.')
        self.results.doubleClicked.connect(self.edit_result)
        self.results.activated.connect(self.edit_result)
        splitter.addWidget(self.results)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0,4,0,0)
        bottom_layout.addWidget(QLabel('Текущий заказ'))
        self.order_grid = table(['Товар','Код','Цена','Остаток','Количество','Сумма','Проверка'],
                                'Количество можно исправить двойным щелчком. Удаление отдельной кнопкой очищает G итогового файла.')
        self.order_grid.setColumnWidth(0,440)
        self.order_grid.setColumnWidth(1,135)
        self.order_grid.setColumnWidth(2,150)
        self.order_grid.setColumnWidth(3,115)
        self.order_grid.setColumnWidth(5,150)
        self.order_grid.setColumnWidth(6,260)
        self.order_grid.cellChanged.connect(self.order_cell_changed)
        bottom_layout.addWidget(self.order_grid)
        self.total_label = QLabel()
        self.total_label.setStyleSheet('font-size: 12pt; font-weight: 600; color: #23836b;')
        self.total_label.setToolTip('Сумма вычисляется точными десятичными числами. Если цена неизвестна, показан только известный итог с отметкой о неполноте.')
        bottom_layout.addWidget(self.total_label)
        splitter.addWidget(bottom)
        splitter.setSizes([340,300])
        layout.addWidget(splitter,1)
        bar = QHBoxLayout()
        self.edit_button = button('Изменить количество','Изменить количество выделенной позиции текущего заказа с проверкой остатка.',self.edit_order_selected)
        self.delete_button = button('Удалить позицию','Удалить выделенный товар; при экспорте его G станет пустой, включая импортированный заказ.',self.delete_selected)
        self.bulk_button = button('Массовая вставка','Вставить список, проверить соответствия и подтвердить добавление. Незавершённая операция сохраняется.',self.bulk)
        self.transfer_button = button('Новый прайс','Явно перенести текущий заказ на обновлённый прайс с предпросмотром новых цен и остатков.',self.transfer)
        for w in (self.edit_button,self.delete_button,self.bulk_button,self.transfer_button):
            bar.addWidget(w)
        bar.addStretch()
        self.preview_button = button('Предпросмотр и экспорт','Показать исходные и итоговые G, проверить изменения и сохранить новый XLSX.',self.preview)
        self.preview_button.setDefault(True)
        bar.addWidget(self.preview_button)
        layout.addLayout(bar)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(140)
        self.search_timer.timeout.connect(self.do_search)
        self.query.textChanged.connect(self.schedule_search)
        self.stack.addWidget(page)
        self.rendering_order = False

    def focus_search(self):
        if self.stack.currentIndex()==1:
            self.query.setFocus()
            self.query.selectAll()
        else:
            self.list_query.setFocus()

    def open_order(self,oid):
        self.oid = oid
        row = self.store.get(oid)
        self.readonly = row['status']!='draft'
        self.query.clear()
        for w in (self.available,self.priced,self.ordered):
            w.setChecked(False)
        self.stack.setCurrentIndex(1)
        self.refresh_order()
        self.do_search()
        self.query.setFocus()
        self.store.set_setting('last_order',oid)

    def back_to_list(self):
        self.oid,self.book = None,None
        self.search_generation += 1
        self.stack.setCurrentIndex(0)
        self.list_tabs.setCurrentIndex(0)
        self.refresh_list()

    def refresh_order(self):
        if not self.oid:
            return
        row = self.store.get(self.oid)
        state = row['state']
        self.book = self.store.book(state['price'])
        status = {'draft':'Черновик','archive':'Архив — просмотр','trash':'Корзина — просмотр'}[row['status']]
        self.order_title.setText(row['name']+' · '+status)
        self.source_label.setText(f'{self.store.price_name(state["price"])}  ·  {len(self.book.products):,} товаров  ·  Сохранено {row["modified"].replace("T"," ")}')
        self.source_label.setToolTip('SHA-256: '+state['price']+'\nНеизменяемый исходник: '+str(self.book.path))
        self.result_model.editable = not self.readonly
        self.undo_button.setEnabled(not self.readonly and row['cursor']>0)
        self.redo_button.setEnabled(not self.readonly and row['cursor']<len(row['history'])-1)
        for w in (self.edit_button,self.delete_button,self.bulk_button,self.transfer_button):
            w.setEnabled(not self.readonly)
        self.preview_button.setText('Предпросмотр' if self.readonly else 'Предпросмотр и экспорт')
        self.rendering_order = True
        errors = self.book.errors(state['items'])
        self.order_grid.setRowCount(len(state['items']))
        for i,(code,count) in enumerate(state['items'].items()):
            p = self.book.by_code[code]
            try:
                amount = p.price*decimal_value(count) if p.price is not None else None
            except UserError:
                amount = None
            values = [p.name,code,money(p.price),str(p.stock) if p.stock is not None else 'Неизвестен',count,money(amount) if amount is not None else 'Не определена',
                      errors.get(code) or ('Цена не указана — сумма неполная' if p.price is None else '')]
            for j,value in enumerate(values):
                item = cell(value,code if j==0 else None)
                if j==4 and not self.readonly:
                    item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEditable)
                self.order_grid.setItem(i,j,item)
        total,incomplete = totals(self.book.by_code,state['items'])
        self.total_label.setText(f'Позиций: {len(state["items"])}    Известная сумма: {money(total)}'+('    Общая сумма неполная' if incomplete else '')+(f'    Ошибок: {len(errors)}' if errors else ''))
        self.rendering_order = False
        self.statusBar().showMessage('Автосохранено. Ctrl+Z — отменить; Ctrl+Y — повторить.')

    def reset_search(self):
        self.query.clear()
        for w in (self.available,self.priced,self.ordered):
            w.setChecked(False)
        self.schedule_search()

    def schedule_search(self,*args):
        self.search_generation += 1
        self.search_timer.start()

    def do_search(self):
        if not self.oid or not self.book:
            return
        self.search_generation += 1
        generation = self.search_generation
        items = self.store.get(self.oid)['state']['items']
        query,available,priced,ordered = self.query.text(),self.available.isChecked(),self.priced.isChecked(),self.ordered.isChecked()
        book = self.book
        def found(result):
            if generation!=self.search_generation or not self.oid:
                return
            products,fuzzy = result
            self.result_model.set_products(products,items)
            self.search_note.setText(f'{"Прямых совпадений нет. Похожие товары — выбери вручную" if fuzzy else "Найдено товаров"}: {len(products)}')
        self.run_job('Поиск',lambda:search(book.products,query,available=available,priced=priced,ordered=set(items) if ordered else None),found,modal=False)

    def edit_result(self,index):
        if not self.readonly and index.isValid():
            self.results.setFocus()
            target = self.result_model.index(index.row(),7)
            self.results.setCurrentIndex(target)
            self.results.edit(target)

    def add_quantity(self,code,value):
        def work():
            if self.readonly or not self.oid:
                return
            n = quantity(value)
            row = self.store.get(self.oid)
            state = copy.deepcopy(row['state'])
            p = self.book.by_code[code]
            if code in state['items']:
                box = QMessageBox(QMessageBox.Icon.Question,'Товар уже в заказе',f'{p.name}\nУже заказано: {state["items"][code]}. Введено: {n}.',parent=self)
                replace = box.addButton('Заменить',QMessageBox.ButtonRole.AcceptRole)
                add = box.addButton('Прибавить',QMessageBox.ButtonRole.ActionRole)
                box.addButton('Отмена',QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is add:
                    n += quantity(state['items'][code])
                elif box.clickedButton() is not replace:
                    return
            err = p.error(n)
            if err:
                raise UserError(err)
            if p.price is None:
                QMessageBox.warning(self,'Цена не указана',p.name+'\nЗаказ разрешён. Общая сумма будет отмечена как неполная; цена в XLSX не изменится.')
            state['items'][code] = str(n)
            self.store.change(self.oid,state,expected=row['state'])
            self.refresh_order()
            self.query.clear()
            self.do_search()
            self.query.setFocus()
        self.safe(work)

    def selected_code(self):
        i = self.order_grid.currentRow()
        return self.order_grid.item(i,0).data(Qt.ItemDataRole.UserRole) if i>=0 else None

    def set_quantity(self,code,value):
        if self.readonly:
            return
        row = self.store.get(self.oid)
        n = quantity(value)
        error = self.book.by_code[code].error(n)
        if error:
            raise UserError(error)
        state = copy.deepcopy(row['state'])
        state['items'][code] = str(n)
        self.store.change(self.oid,state,expected=row['state'])
        self.refresh_order()
        self.do_search()

    def order_cell_changed(self,i,j):
        if self.rendering_order or j!=4:
            return
        code = self.order_grid.item(i,0).data(Qt.ItemDataRole.UserRole)
        self.safe(lambda:self.set_quantity(code,self.order_grid.item(i,j).text()))
        self.refresh_order()

    def edit_order_selected(self):
        code = self.selected_code()
        if code:
            self.prompt_quantity(code)

    def prompt_quantity(self,code):
        current = self.store.get(self.oid)['state']['items'].get(code,'')
        value,ok = QInputDialog.getText(self,'Количество',self.book.by_code[code].name,text=current)
        if ok:
            self.safe(lambda:self.set_quantity(code,value))

    def delete_code(self,code):
        if self.readonly:
            return
        state = copy.deepcopy(self.store.get(self.oid)['state'])
        state['items'].pop(code,None)
        self.store.change(self.oid,state)
        self.refresh_order()
        self.do_search()

    def delete_selected(self):
        code = self.selected_code()
        if code:
            self.safe(lambda:self.delete_code(code))

    def undo(self,step):
        if self.oid and not self.readonly and self.stack.currentIndex()==1 and QApplication.activeModalWidget() is None:
            self.safe(lambda:self.store.undo(self.oid,step))
            self.refresh_order()
            self.do_search()

    def bulk(self):
        if self.oid and not self.readonly:
            self.safe(lambda:BulkDialog(self.store,self.oid,self).exec())
            self.refresh_order()
            self.do_search()

    def transfer(self):
        if self.readonly:
            return
        path,_ = QFileDialog.getOpenFileName(self,'Обновлённый прайс ХИМБЭСТ','','Прайс Excel (*.xlsx)')
        if not path:
            return
        def loaded(book):
            if book.imported:
                raise UserError('В новом прайсе уже есть заполненные G. Импортируй его отдельным черновиком или выбери чистый прайс для переноса.')
            dialog = TransferDialog(self.store,self.oid,book,self)
            if dialog.exec():
                self.refresh_order()
                self.do_search()
        self.run_job('Проверяю обновлённый прайс…',lambda:self.adapter.load(path),loaded)

    def preview(self):
        if not self.oid:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Предпросмотр: исходное G → итоговое G')
        dialog.resize(1320,750)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Меняются только значения товарных ячеек «Заказано». Очищаемые исходные количества показаны отдельными строками.'))
        grid = table(['Товар','Код','Ячейка','Цена','Остаток','Было G','Станет G','Сумма','Проверка'],
                     'Проверка исходного и итогового количества. Исправления доступны кнопками внизу и сразу сохраняются в черновике.')
        grid.setColumnWidth(0,350)
        grid.setColumnWidth(1,130)
        layout.addWidget(grid)
        summary = QLabel()
        summary.setWordWrap(True)
        layout.addWidget(summary)
        bar = QHBoxLayout()
        def selected():
            i = grid.currentRow()
            return grid.item(i,0).data(Qt.ItemDataRole.UserRole) if i>=0 else None
        def edit():
            code = selected()
            if code:
                self.prompt_quantity(code)
                refresh()
        def remove():
            code = selected()
            if code:
                self.delete_code(code)
                refresh()
        edit_b = button('Изменить количество','Исправить итоговое количество выбранной позиции с проверкой остатка.',edit)
        remove_b = button('Удалить позицию','Очистить итоговую G выбранной позиции.',remove)
        for w in (edit_b,remove_b):
            w.setEnabled(not self.readonly)
            bar.addWidget(w)
        bar.addStretch()
        save_b = button('Сохранить новый XLSX','Записать новый файл, проверить все части OOXML и только после успеха архивировать заказ.',lambda:self.choose_export(dialog))
        save_b.setDefault(True)
        bar.addWidget(save_b)
        bar.addWidget(button('Закрыть','Вернуться к заказу. Все исправления уже сохранены.',dialog.reject))
        layout.addLayout(bar)
        def refresh():
            state = self.store.get(self.oid)['state']
            errors = self.book.errors(state['items'])
            products = [p for p in self.book.products if p.code in state['items'] or p.original is not None]
            grid.setRowCount(len(products))
            for i,p in enumerate(products):
                final = state['items'].get(p.code)
                try:
                    amount = p.price*decimal_value(final) if p.price is not None and final is not None else None
                except UserError:
                    amount = None
                status = errors.get(p.code) or ('Будет очищена' if final is None else 'Изменится' if final!=p.original else 'Без изменения')
                values = [p.name,p.code,'G'+str(p.row),money(p.price),str(p.stock) if p.stock is not None else 'Неизвестен',
                          p.original if p.original is not None else 'Пусто',final if final is not None else 'Пусто',money(amount) if amount is not None else '—',status]
                for j,v in enumerate(values):
                    item = cell(v,p.code if j==0 else None)
                    if final!=p.original:
                        item.setBackground(QApplication.palette().alternateBase())
                    grid.setItem(i,j,item)
            total,incomplete = totals(self.book.by_code,state['items'])
            summary.setText(f'Позиций в заказе: {len(state["items"])}. Известная сумма: {money(total)}.'+(' Общая сумма неполная.' if incomplete else '')+
                            f' Ошибок: {len(errors)}.\nСовместимость с реальным обработчиком 1С поставщика не подтверждена; автоматическая проверка контролирует сохранность XLSX.')
            save_b.setEnabled(not self.readonly and bool(state['items']) and not errors)
        refresh()
        fill_tooltips(dialog)
        dialog.exec()

    def choose_export(self,preview_dialog):
        state = copy.deepcopy(self.store.get(self.oid)['state'])
        directory = Path(self.store.setting('export_dir',str(Path.home()/'Documents')))
        suggested = directory / ('ХИМБЭСТ — Заказ '+date.today().strftime('%d.%m.%Y')+'.xlsx')
        while True:
            path,_ = QFileDialog.getSaveFileName(self,'Сохранить новый заказ',str(suggested),'Excel (*.xlsx)',
                                                options=QFileDialog.Option.DontConfirmOverwrite)
            if not path:
                return
            output = Path(path)
            if output.suffix.lower()!='.xlsx':
                output = output.with_suffix('.xlsx')
            protected = self.store.protected_paths()
            if any(output.resolve()==p.resolve() or (output.exists() and p.exists() and os.path.samefile(output,p)) for p in protected):
                error_box(self,'Этот путь принадлежит исходному прайсу. Его заменять запрещено; выбери другое имя.')
                suggested = output.parent/('Заказ — '+output.name)
                continue
            replace = False
            if output.exists():
                box = QMessageBox(QMessageBox.Icon.Question,'Файл уже существует',str(output),parent=self)
                clone = box.addButton('Создать копию',QMessageBox.ButtonRole.AcceptRole)
                rename = box.addButton('Выбрать другое имя',QMessageBox.ButtonRole.ActionRole)
                overwrite = box.addButton('Заменить этот экспорт',QMessageBox.ButtonRole.DestructiveRole)
                box.addButton('Отмена',QMessageBox.ButtonRole.RejectRole)
                box.exec()
                clicked = box.clickedButton()
                if clicked is clone:
                    index = 2
                    candidate = output
                    while candidate.exists():
                        candidate = output.with_name(output.stem+f' ({index})'+output.suffix)
                        index += 1
                    output = candidate
                elif clicked is rename:
                    suggested = output
                    continue
                elif clicked is overwrite:
                    if not confirm(self,'Подтвердить замену файла','Прежний файл будет заменён только после успешной проверки нового XLSX.\n'+str(output)):
                        return
                    replace = True
                else:
                    return
            break
        self.store.set_setting('export_dir',str(output.parent))
        oid,book = self.oid,self.book
        def work():
            report = self.adapter.export(book,state['items'],output,replace=replace,protected=protected)
            self.store.complete_export(oid,output,report,expected=state)
            LOG.info('operation=%s stage=export verified=true changed_cells=%d',uuid.uuid4().hex[:10],len(report['changed_cells']))
            return report
        def success(report):
            preview_dialog.accept()
            dialog = QDialog(self)
            dialog.setWindowTitle('Заказ успешно сохранён')
            layout = QVBoxLayout(dialog)
            label = QLabel(f'Заказ успешно сохранён и помещён в архив.\n{output}\n\nOOXML проверен: изменённых ячеек G — {len(report["changed_cells"])}; остальные части сохранены.\nСовместимость с 1С поставщика требует практической проверки.')
            label.setWordWrap(True)
            layout.addWidget(label)
            bar = QHBoxLayout()
            bar.addWidget(button('Открыть файл','Открыть XLSX в приложении Windows по умолчанию.',lambda:self.safe(lambda:open_path(self,output))))
            bar.addWidget(button('Показать в папке','Показать готовый XLSX в Проводнике.',lambda:self.safe(lambda:open_path(self,output,True))))
            bar.addWidget(button('Готово','Вернуться к активным черновикам. Новый пустой заказ не создаётся.',dialog.accept))
            layout.addLayout(bar)
            fill_tooltips(dialog)
            dialog.exec()
            self.back_to_list()
        self.run_job('Записываю XLSX и проверяю каждую часть книги…',work,success)

    def settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Настройки и резервные копии')
        dialog.resize(760,430)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        theme = QComboBox()
        for text,key in [('Системная','system'),('Светлая','light'),('Тёмная','dark')]:
            theme.addItem(text,key)
        theme.setCurrentIndex(max(0,theme.findData(self.store.setting('theme','system'))))
        theme.setToolTip('Системная тема следует настройке Windows. Выбор применяется ко всем окнам и сохраняется.')
        def change_theme():
            self.store.set_setting('theme',theme.currentData())
            apply_theme(QApplication.instance(),theme.currentData())
        theme.currentIndexChanged.connect(change_theme)
        form.addRow('Тема:',theme)
        data_label = QLabel(str(self.store.root))
        data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data_label.setWordWrap(True)
        data_label.setToolTip('Установленная версия: LocalAppData/PriceListHandler. Портативная: Data рядом с EXE. Хранилища независимы.')
        form.addRow('Данные:',data_label)
        backup_label = QLabel(str(self.store.backup_dir()))
        backup_label.setWordWrap(True)
        backup_label.setToolTip('Хранятся 10 последних проверенных обычных резервных копий. Защитные точки перед восстановлением автоматически не удаляются.')
        form.addRow('Резервные копии:',backup_label)
        layout.addLayout(form)
        def folder():
            path = QFileDialog.getExistingDirectory(dialog,'Папка резервных копий',str(self.store.backup_dir()))
            if path:
                def set_folder():
                    p = Path(path)
                    fd,tmp = tempfile.mkstemp(dir=p)
                    os.close(fd)
                    Path(tmp).unlink()
                    self.store.set_setting('backup_dir',path)
                    backup_label.setText(path)
                self.safe(set_folder)
        layout.addWidget(button('Выбрать папку резервных копий','Проверить доступность папки и использовать её для следующих копий.',folder))
        layout.addWidget(button('Создать резервную копию сейчас','Включает базу, все заказы, полную историю, исходные прайсы, словарь и настройки.',
            lambda:self.run_job('Создаю и проверяю резервную копию…',self.store.backup,lambda p:QMessageBox.information(dialog,'Копия создана',str(p)))))
        def restore():
            path,_ = QFileDialog.getOpenFileName(dialog,'Выбрать резервную копию',str(self.store.backup_dir()),'Резервные копии (*.zip)')
            if not path:
                return
            def inspected(summary):
                if confirm(dialog,'Заменить рабочие данные?',f'Копия: {summary["created"]}\nЗаказов: {summary["orders"]}; исходных прайсов: {summary["sources"]}.\nТекущие рабочие данные будут заменены. Перед заменой создаётся отдельная защитная копия.'):
                    def restored(result):
                        dialog.accept()
                        self.back_to_list()
                        apply_theme(QApplication.instance(),self.store.setting('theme','system'))
                        QMessageBox.information(self,'Данные восстановлены','Восстановление проверено.\nЗащитная копия предыдущего состояния:\n'+str(result[1]))
                    self.run_job('Создаю защитную копию и восстанавливаю данные…',lambda:self.store.restore_backup(path),restored)
            self.run_job('Проверяю целостность и состав резервной копии…',lambda:self.store.inspect_backup(path),inspected)
        layout.addWidget(button('Восстановить из резервной копии','Сначала проверка и сводка, затем подтверждение и защитная копия текущего состояния.',restore))
        layout.addWidget(button('Открыть папку данных','Показать папку данных и диагностических логов.',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.root)))))
        layout.addWidget(QLabel(f'Версия {__version__}. Работа с заказами доступна без интернета.\nСовместимость с обработчиком 1С ХИМБЭСТ пока не подтверждена.'))
        layout.addWidget(button('Проверить обновления','Проверить стабильные GitHub Releases. Скачивание и установка выполняются только вручную.',self.check_updates))
        layout.addWidget(button('Готово','Закрыть настройки. Изменения сохраняются сразу.',dialog.accept))
        fill_tooltips(dialog)
        dialog.exec()

    def startup(self):
        if (self.store.root/'restore-journal.json').exists():
            QMessageBox.warning(self,'Прерванное восстановление','Предыдущее восстановление было прервано. База заменяется атомарно, исходные прайсы сохранены. Проверь заказы; для отката выбери защитную копию в настройках. Автоматическая подмена данных не выполняется.')
        self.run_job('Ежедневное резервирование',self.store.daily_backup,
                     lambda _:None,modal=False,on_error=lambda e:self.statusBar().showMessage('Не удалось создать ежедневную копию: '+str(e)))
        def drafts_loaded(drafts):
            if drafts and self.isVisible() and self.oid is None:
                last = self.store.setting('last_order')
                oid = last if any(r['id']==last for r in drafts) else drafts[0]['id']
                if confirm(self,'Продолжить незавершённый заказ?',self.store.get(oid)['name']+'\nБудут восстановлены товары, история и незавершённая массовая вставка.'):
                    self.open_order(oid)
        self.run_job('Восстанавливаю список черновиков',lambda:self.store.list_orders('draft'),drafts_loaded,modal=False)

    def check_updates(self):
        def fetch():
            from urllib.request import Request,urlopen
            from urllib.error import HTTPError
            request = Request('https://api.github.com/repos/BadBox873/PriceListHandler/releases/latest',headers={'User-Agent':'PriceListHandler/'+__version__,'Accept':'application/vnd.github+json'})
            try:
                with urlopen(request,timeout=8) as response:
                    return json.loads(response.read(1024*1024))
            except HTTPError as exc:
                if exc.code==404:
                    return None
                raise
        def received(data):
            if not data:
                self.statusBar().showMessage('Новых стабильных выпусков нет.')
                return
            import re
            def version(text):
                m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)',text)
                return tuple(map(int,m.groups())) if m else (0,0,0)
            if version(data.get('tag_name',''))<=version(__version__):
                self.statusBar().showMessage('Установлена актуальная стабильная версия.')
                return
            box = QMessageBox(QMessageBox.Icon.Information,'Доступна новая версия',data['tag_name']+'\nОбновление устанавливается вручную.',parent=self)
            box.setDetailedText(data.get('body',''))
            open_button = box.addButton('Открыть страницу релиза',QMessageBox.ButtonRole.AcceptRole)
            box.addButton('Позже',QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is open_button:
                QDesktopServices.openUrl(QUrl('https://github.com/BadBox873/PriceListHandler/releases/latest'))
        self.run_job('Проверка обновлений',fetch,received,modal=False,
                     on_error=lambda _:self.statusBar().showMessage('Проверка обновлений недоступна. Работа с заказами продолжается.'))

    def closeEvent(self,event):
        if any(not job.function.__name__=='<lambda>' for job in self.jobs) and QApplication.activeModalWidget():
            event.ignore()
            return
        if self.oid:
            self.store.set_setting('last_order',self.oid)
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description='PriceListHandler — заказы ХИМБЭСТ')
    parser.add_argument('--portable',action='store_true')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--smoke-test',type=Path,metavar='OUTPUT_DIR')
    args = parser.parse_args(argv)
    if args.smoke_test:
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    app = QApplication(sys.argv[:1])
    app.setApplicationName('PriceListHandler')
    app.setApplicationVersion(__version__)
    app.setOrganizationName('BadBox873')
    app.setWindowIcon(app_icon())
    root = args.data_dir or (args.smoke_test/'Data' if args.smoke_test else data_directory(True if args.portable else None))
    try:
        root.mkdir(parents=True,exist_ok=True)
        fd,tmp = tempfile.mkstemp(dir=root)
        os.close(fd)
        Path(tmp).unlink()
        lock = QLockFile(str(root/'application.lock'))
        lock.setStaleLockTime(30000)
        if not lock.tryLock(100):
            raise UserError('Это хранилище уже открыто другим экземпляром. Переключись на запущенное окно или выбери отдельную портативную папку.')
        store = Store(root)
    except Exception as exc:
        error_box(None,'Не удалось открыть папку данных. Для портативной версии выбери доступную для записи папку.\n'+str(exc))
        return 1
    handler = RotatingFileHandler(root/'diagnostics.log',maxBytes=1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(DiagnosticFormatter())
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    apply_theme(app,store.setting('theme','system'))
    app.styleHints().colorSchemeChanged.connect(lambda _:apply_theme(app,store.setting('theme','system')) if store.setting('theme','system')=='system' else None)
    window = MainWindow(store,check_updates=not bool(args.smoke_test),recover=not bool(args.smoke_test))
    def unhandled_exception(kind,error,traceback):
        operation=uuid.uuid4().hex[:10]
        LOG.error('operation=%s stage=callback error=%s',operation,kind.__name__,exc_info=(kind,error,traceback))
        error_box(window,str(error)+f'\nКод диагностики: {operation}. Черновик хранится в локальной базе.')
    sys.excepthook=unhandled_exception
    window.show()
    if args.smoke_test:
        # Exercise the frozen application's actual imports, UI, storage, parser and exporter.
        def smoke():
            try:
                from .smoke import run_smoke
                args.smoke_test.mkdir(parents=True,exist_ok=True)
                result = run_smoke(store,window,args.smoke_test)
                (args.smoke_test/'smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                window.grab().save(str(args.smoke_test/'window.png'))
                app.exit(0)
            except Exception:
                import traceback
                args.smoke_test.mkdir(parents=True,exist_ok=True)
                (args.smoke_test/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
                app.exit(2)
        QTimer.singleShot(5,smoke)
    result = app.exec()
    QThreadPool.globalInstance().waitForDone(12000)
    lock.unlock()
    return result


if __name__=='__main__':
    raise SystemExit(main())
