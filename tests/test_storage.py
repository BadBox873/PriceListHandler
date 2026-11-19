import copy
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from pricelisthandler.domain import UserError
from pricelisthandler.ooxml import export_workbook
from pricelisthandler.storage import Store


def test_independent_drafts_and_immutable_copy(store,book):
    a,b=store.create('Первый',book),store.create('Второй',book)
    state=store.get(a)['state'];state['items']={'00000016700':'2'};store.change(a,state)
    assert not store.get(b)['state']['items']
    sha=state['price']
    original=book.path.read_bytes()
    book.path.unlink()
    assert store.book(sha).path.read_bytes()==original


def test_full_undo_redo_survives_restart_and_branching(store,book):
    oid=store.create('history',book)
    for count in ['1','2','3']:
        state=store.get(oid)['state'];state['items']={'00000016700':count};store.change(oid,state)
    root=store.root
    store=Store(root)
    store.undo(oid)
    assert store.get(oid)['state']['items']['00000016700']=='2'
    store=Store(root);store.undo(oid,1)
    assert store.get(oid)['state']['items']['00000016700']=='3'
    store.undo(oid)
    state=store.get(oid)['state'];state['items']['00000016700']='5';store.change(oid,state)
    store.undo(oid,1)
    assert store.get(oid)['state']['items']['00000016700']=='5'


def test_optimistic_conflict_rejected(store,book):
    oid=store.create('conflict',book)
    initial=store.get(oid)['state']
    state=copy.deepcopy(initial);state['items']={'00000016700':'1'};store.change(oid,state)
    with pytest.raises(UserError):
        store.change(oid,initial,expected=initial)


def test_archive_restores_new_draft_trash_preserves_history_and_export(store,book,tmp_path):
    oid=store.create('archive',book)
    state=store.get(oid)['state'];state['items']={'00000016700':'2'};store.change(oid,state)
    out=tmp_path/'external.xlsx'
    report=export_workbook(store.book(state['price']),state['items'],out)
    store.complete_export(oid,out,report,expected=state)
    clone=store.restore(oid)
    assert clone!=oid and store.get(oid)['status']=='archive'
    assert store.get(clone)['history']==store.get(oid)['history']
    store.trash(oid);store.restore(oid)
    assert store.get(oid)['status']=='archive'
    store.trash(clone);store.trash(oid)
    assert store.trash_size()[0]==2
    store.purge()
    assert out.exists()
    assert store.exports()[0]['path']==str(out)
    assert not list(store.sources.glob('*.xlsx'))


def test_search_orders_by_content_date_total_and_name(store,book):
    a=store.create('Зета',book)
    b=store.create('Альфа',book)
    state=store.get(a)['state'];state['items']={'00000016700':'1','00000000003':'2'};store.change(a,state)
    assert [r['id'] for r in store.list_orders(query='буренка')]==[a]
    assert [r['id'] for r in store.list_orders(query='00000016700')]==[a]
    assert store.list_orders(query='буренка')[0]['incomplete']
    assert [r['id'] for r in store.list_orders(sort='name',descending=False)]==[b,a]
    assert not store.list_orders(start='2099-01-01')


def test_dictionary_scoped_and_normalized(store):
    store.save_alias('МОЯ  МАЗЬ','00000016700')
    assert store.aliases('моя')[0]['alias']=='моя мазь'
    store.save_alias('Новая мазь','ЦБ-00001868',old='моя мазь')
    assert len(store.aliases())==1
    store.delete_alias('новая мазь')
    assert not store.aliases()


def test_backup_roundtrip_full_state_and_protective_copy(store,book):
    oid=store.create('backup',book)
    state=store.get(oid)['state'];state['items']={'00000016700':'2'};state['bulk_text']='неоконченный список';store.change(oid,state)
    store.save_alias('мазь','00000016700')
    backup=store.backup()
    initial=store.get(oid)
    store.rename(oid,'changed')
    summary,protect=store.restore_backup(backup)
    assert summary['orders']==1 and protect.exists()
    assert store.get(oid)==initial
    assert store.aliases()[0]['code']=='00000016700'
    assert store.get(oid)['state']['bulk_text']=='неоконченный список'
    assert store.inspect_backup(protect)['orders']==1


def test_backup_rotation_only_after_verification(store,book):
    store.create('rotation',book)
    protect=store.backup(protective=True)
    for i in range(11):
        store.backup()
    assert len(list(store.backup_dir().glob('backup-*.zip')))==10
    assert protect.exists()


def test_failed_backup_never_rotates_good_copies(store,book,monkeypatch):
    store.create('safe',book)
    good=store.backup()
    def fail(path):
        raise UserError('injected validation failure')
    monkeypatch.setattr(store,'inspect_backup',fail)
    with pytest.raises(UserError):
        store.backup()
    assert good.exists()
    assert len(list(store.backup_dir().glob('backup-*.zip')))==1


def test_daily_backup_no_duplicate_same_day_or_unchanged(store,book):
    store.create('daily',book)
    assert store.daily_backup()
    assert store.daily_backup() is None
    marker=store.root/'backup-marker.json'
    data=json.loads(marker.read_text());data['date']='2000-01-01';marker.write_text(json.dumps(data))
    assert store.daily_backup() is None
    store.set_setting('theme','dark')
    assert store.daily_backup()


def test_corrupt_backup_rejected_without_mutation(store,book,tmp_path):
    oid=store.create('safe',book)
    good=store.backup()
    bad=tmp_path/'bad.zip'
    with ZipFile(good) as z:
        files={n:z.read(n) for n in z.namelist()}
    files['orders.sqlite3']=b'broken'
    with ZipFile(bad,'w') as z:
        for n,b in files.items():
            z.writestr(n,b)
    original=store.db_path.read_bytes()
    with pytest.raises(UserError):
        store.restore_backup(bad)
    assert store.db_path.read_bytes()==original
    assert store.get(oid)['name']=='safe'


def test_installed_and_portable_data_are_independent(tmp_path,book):
    a,b=Store(tmp_path/'LocalAppData/PriceListHandler'),Store(tmp_path/'Portable/Data')
    a.create('only installed',book)
    assert len(a.list_orders())==1 and not b.list_orders()


def test_corrupt_source_blocks_access(store,book):
    oid=store.create('integrity',book)
    sha=store.get(oid)['state']['price']
    (store.sources/(sha+'.xlsx')).write_bytes(b'broken')
    with pytest.raises(UserError):
        store.book(sha)
