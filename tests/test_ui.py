import copy

from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QWidget

from pricelisthandler.app import MainWindow,apply_theme
from pricelisthandler.dialogs import BulkDialog,DictionaryDialog,ExportHistoryDialog,TransferDialog
from pricelisthandler.widgets import ProductModel,fill_tooltips


def make_window(store,oid):
    window=MainWindow(store,check_updates=False,recover=False)
    # Startup recovery is tested at the storage boundary; avoid modal prompts here.
    window.startup=lambda:None
    window.open_order(oid)
    return window


def test_enter_quantity_clears_search_and_focuses(store,book,qapp):
    oid=store.create('UI',book)
    window=make_window(store,oid)
    window.show()
    window.query.setText('буренка')
    window.result_model.set_products([book.by_code['00000016700']],{})
    index=window.result_model.index(0,7)
    window.results.setCurrentIndex(index)
    window.results.setFocus()
    window.results.edit(index)
    qapp.processEvents()
    editor=QApplication.focusWidget()
    QTest.keyClicks(editor,'20')
    QTest.keyClick(editor,Qt.Key.Key_Return)
    QTest.qWait(30)
    assert store.get(oid)['state']['items']['00000016700']=='20'
    assert window.query.text()==''
    assert QApplication.focusWidget() is window.query
    window.close()


def test_duplicate_add_final_stock_checked(store,book,qapp):
    oid=store.create('duplicate',book)
    window=make_window(store,oid)
    window.set_quantity('00000016700','60')
    def choose_add():
        box=QApplication.activeModalWidget()
        if isinstance(box,QMessageBox):
            next(b for b in box.buttons() if b.text()=='Прибавить').click()
    def close_error():
        box=QApplication.activeModalWidget()
        if isinstance(box,QMessageBox):
            box.accept()
    QTimer.singleShot(5,choose_add)
    QTimer.singleShot(15,close_error)
    window.add_quantity('00000016700','20')
    assert store.get(oid)['state']['items']['00000016700']=='60'
    window.close()


def test_bulk_dialog_persists_text_and_quantity(store,book,qapp):
    oid=store.create('bulk-ui',book)
    dialog=BulkDialog(store,oid)
    dialog.text.setPlainText('00000016700:20\nнепонятная строка')
    dialog.parse()
    dialog.grid.item(0,3).setText('30')
    dialog.accept()
    state=store.get(oid)['state']
    assert state['bulk'][0]['qty']=='30'
    dialog=BulkDialog(store,oid)
    assert dialog.text.toPlainText()=='00000016700:20\nнепонятная строка'
    assert dialog.grid.item(0,3).text()=='30'
    dialog.accept()


def test_themes_dialogs_and_tooltips(store,book,qapp):
    oid=store.create('themes',book)
    window=make_window(store,oid)
    for theme in ['light','dark','system']:
        apply_theme(qapp,theme)
        qapp.processEvents()
    dialogs=[BulkDialog(store,oid,window),DictionaryDialog(store,book,window),ExportHistoryDialog(store,parent=window),TransferDialog(store,oid,book,window)]
    for dialog in dialogs:
        fill_tooltips(dialog)
        assert all(w.toolTip() for w in dialog.findChildren(QWidget))
        dialog.reject()
    window.close()


def test_bulk_missing_price_warning_is_visible(store,book,qapp):
    oid=store.create('price-warning',book)
    dialog=BulkDialog(store,oid)
    dialog.text.setPlainText('00000000003:5')
    dialog.parse()
    assert 'сумма неполная' in dialog.grid.item(0,7).text()
    dialog.accept()
