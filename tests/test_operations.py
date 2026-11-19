import copy
from decimal import Decimal
import time

import pytest

from pricelisthandler.domain import UserError,money,normalize,quantity,search,totals
from pricelisthandler.operations import (apply_bulk,apply_transfer,bulk_preview,parse_bulk,transfer_preview,transfer_rows)
from pricelisthandler.ooxml import load_workbook
from pricelisthandler.smoke import write_fixture


@pytest.mark.parametrize('value',['0','-1','1.5','1,5','text','','1000000000000','NaN','inf','+1'])
def test_bad_manual_quantity(value):
    with pytest.raises(UserError):
        quantity(value)


@pytest.mark.parametrize('value',['1','20','001',' 2 '])
def test_good_manual_quantity(value):
    assert quantity(value)==int(value)


@pytest.mark.parametrize('query,code',[('мазь буренка','00000016700'),('ЦБ-00001868','ЦБ-00001868'),
    ('4601234567890','00000016700'),('белая капуста','00000000003')])
def test_search(book,query,code):
    rows,fuzzy=search(book.products,query)
    assert not fuzzy
    assert code in [r.code for r in rows]


def test_search_filters_and_fuzzy(book):
    rows,_=search(book.products,'',available=True,priced=True)
    assert {r.code for r in rows}=={'00000016700','ЦБ-00001868'}
    rows,_=search(book.products,'',ordered={'ЦБ-00001868'})
    assert [r.code for r in rows]==['ЦБ-00001868']
    rows,fuzzy=search(book.products,'буренкка')
    assert fuzzy and rows[0].code=='00000016700'


def test_decimal_totals(book):
    total,incomplete=totals(book.by_code,{'00000016700':'3','ЦБ-00001868':'2'})
    assert total==Decimal('678.82') and not incomplete
    total,incomplete=totals(book.by_code,{'00000016700':'3','00000000003':'2'})
    assert total==Decimal('636.72') and incomplete


@pytest.mark.parametrize('line',['00000016700\t20','00000016700;20','00000016700: 20',
    '00000016700 - 20','00000016700 20','Буренка мазь 200мл/24:20','Буренка мазь 200мл/24 20 шт.'])
def test_bulk_unambiguous(line,book):
    rows=parse_bulk(line,book)
    assert len(rows)==1 and rows[0]['confirmed'] and rows[0]['qty']=='20'
    proposed,accepted,errors=bulk_preview(book,{},rows)
    assert proposed=={'00000016700':'20'} and len(accepted)==1 and not errors


def test_bulk_ambiguous_name_and_alias_need_confirmation(book):
    rows=parse_bulk('Томат Акварель 20шт Уральский Дачник 10\nмоя мазь: 2',book,[{'alias':'моя мазь','code':'00000016700'}])
    assert not rows[0]['confirmed'] and not rows[1]['confirmed']
    assert rows[1]['candidates'][0]=='00000016700'
    assert not bulk_preview(book,{},rows)[1]


def test_bulk_duplicates_require_explicit_policy(book):
    rows=parse_bulk('00000016700:20\n00000016700:10',book)
    assert len(bulk_preview(book,{},rows)[2])==2
    for row in rows:
        row['duplicate']='sum'
    assert bulk_preview(book,{},rows)[0]=={'00000016700':'30'}
    rows[1]['skip']=True
    rows[0]['duplicate']='unresolved'
    assert bulk_preview(book,{},rows)[0]=={'00000016700':'20'}


def test_bulk_final_stock_and_per_row_mode(book):
    rows=parse_bulk('00000016700:20\nЦБ-00001868:10',book)
    rows[0]['mode']='add'
    proposed,accepted,errors=bulk_preview(book,{'00000016700':'60','ЦБ-00001868':'5'},rows)
    assert proposed=={'ЦБ-00001868':'10'}
    assert rows[0]['id'] in errors
    rows[0]['mode']='replace'
    rows[1]['mode']='add'
    assert bulk_preview(book,{'00000016700':'60','ЦБ-00001868':'5'},rows)[0]=={'00000016700':'20','ЦБ-00001868':'15'}


def test_bulk_processed_and_quantities_atomic_and_undoable(store,book):
    oid=store.create('bulk',book)
    state=store.get(oid)['state']
    state['bulk']=parse_bulk('00000016700:20\nnot found:2',book)
    store.change(oid,state)
    accepted,errors=apply_bulk(store,oid)
    assert len(accepted)==1 and errors
    after=store.get(oid)['state']
    assert after['bulk'][0]['processed'] and after['items']['00000016700']=='20'
    with pytest.raises(UserError):
        apply_bulk(store,oid)
    store.undo(oid)
    before=store.get(oid)['state']
    assert not before['bulk'][0]['processed'] and not before['items']
    store.undo(oid,1)
    assert store.get(oid)['state']==after


def test_transfer_changed_codes_price_stock_and_conflict(store,book,tmp_path):
    products=[{'row':4,'name':'Буренка новая','detail':'','code':'NEW-1','barcode':'4601234567890','price':'250.50','stock':'10'},
              {'row':5,'name':'Томат переименован','code':'ЦБ-00001868','price':'30','stock':'120'}]
    new=load_workbook(write_fixture(tmp_path/'new.xlsx',products))
    oid=store.create('transfer',book)
    state=store.get(oid)['state'];state['items']={'00000016700':'20','ЦБ-00001868':'3'};store.change(oid,state)
    rows=transfer_rows(book,new,state['items'])
    assert not rows[0]['confirmed'] and rows[0]['candidates'][0]=='NEW-1'
    assert rows[1]['confirmed']
    original=store.get(oid)['state']
    with pytest.raises(UserError):
        apply_transfer(store,oid,new,rows)
    assert store.get(oid)['state']==original
    rows[0].update(code='NEW-1',confirmed=True)
    assert transfer_preview(book,new,rows)[1]
    rows[0]['qty']='10'
    apply_transfer(store,oid,new,rows)
    current=store.get(oid)['state']
    assert current['items']=={'NEW-1':'10','ЦБ-00001868':'3'}
    assert store.book(current['price']).by_code['NEW-1'].price==Decimal('250.50')
    store.undo(oid)
    assert store.get(oid)['state']==original
    rows[0].update(code='ЦБ-00001868',qty='1')
    assert len(transfer_preview(book,new,rows)[1])==2


def test_real_normal_search_latency(real_book):
    times=[]
    for query in ['мазь буренка','ЦБ-00001868','томат','белая головушка']:
        start=time.perf_counter()
        search(real_book.products,query,fuzzy=False)
        times.append((time.perf_counter()-start)*1000)
    print('Real-price direct search milliseconds:',times)
    assert max(times)<300
