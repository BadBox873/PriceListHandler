import copy
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from pricelisthandler.domain import UserError
from pricelisthandler.ooxml import (CELL, digest, export_workbook, load_workbook, patch_cell, patch_sheet, read_package, verify_export)
from pricelisthandler.smoke import write_fixture


def rewrite(path, updates):
    with ZipFile(path) as z:
        parts = {i.filename:z.read(i) for i in z.infolist()}
        infos,comment = z.infolist(),z.comment
    parts.update(updates)
    with ZipFile(path,'w') as z:
        z.comment = comment
        for i in infos:
            z.writestr(i,parts[i.filename])


def test_catalog_identifiers_and_categories(book):
    assert len(book.products)==5
    assert book.products[0].row==4
    assert book.products[0].code=='00000016700'
    assert book.products[1].code=='ЦБ-00001868'
    assert book.products[2].price is None
    assert book.products[3].stock is None
    assert not book.imported


def test_round_trip_only_g(book,tmp_path):
    original = book.path.read_bytes()
    out = tmp_path/'Заказ.xlsx'
    report = export_workbook(book,{'00000016700':'20','ЦБ-00001868':'10'},out)
    assert report['changed_cells']==['G4','G5']
    assert report['verified']
    assert book.path.read_bytes()==original
    result = load_workbook(out)
    assert result.imported=={'00000016700':'20','ЦБ-00001868':'10'}
    for name,data in book.parts.items():
        if name!=book.sheet:
            assert data==result.parts[name]
    assert result.cells['G2']==book.cells['G2']
    assert result.cells['G3']==book.cells['G3']
    assert result.cells['G9']==book.cells['G9']
    assert result.cells['G4']==b'<c r="G4" s="1" t="n"><v>20</v></c>'


def test_compatible_viewer_reads_export(book,tmp_path):
    import openpyxl
    out = tmp_path/'order.xlsx'
    export_workbook(book,{'00000016700':'20'},out)
    wb = openpyxl.load_workbook(out,read_only=False,data_only=False)
    assert wb.active['G4'].value==20
    assert wb.active['C4'].value=='00000016700'
    assert wb.active['G4'].style_id==1
    wb.close()


def test_import_all_invalid_values_and_clear(tmp_path):
    book = load_workbook(write_fixture(tmp_path/'filled.xlsx',original={4:0,5:'1.5',6:-2,7:1,8:2}))
    assert len(book.imported)==5
    assert len(book.errors(book.imported))==5
    out = tmp_path/'corrected.xlsx'
    export_workbook(book,{'ЦБ-00001868':'3'},out)
    result = load_workbook(out)
    assert result.imported=={'ЦБ-00001868':'3'}
    for address in ('G4','G6','G7','G8'):
        assert b'<v>' not in result.cells[address]


def test_text_quantity_requires_manual_correction(tmp_path):
    book = load_workbook(write_fixture(tmp_path/'text.xlsx',original={4:('inlineStr','20')}))
    assert '00000016700' in book.errors(book.imported)
    out = tmp_path/'fixed.xlsx'
    export_workbook(book,{'00000016700':'20'},out)
    assert load_workbook(out).by_code['00000016700'].original_type=='n'


def test_unchanged_numeric_import_needs_no_rewrite(tmp_path):
    book = load_workbook(write_fixture(tmp_path/'filled.xlsx',original={4:'20.0'}))
    report = export_workbook(book,book.imported,tmp_path/'same.xlsx')
    assert report['changed_cells']==[]


@pytest.mark.parametrize('items',[{}, {'00000016700':'0'}, {'00000016700':'-1'}, {'00000016700':'1.5'},
    {'00000016700':'text'}, {'00000016700':'73'}, {'00000000004':'1'}, {'00000000005':'1'}, {'missing':'1'}])
def test_export_validation(book,tmp_path,items):
    out = tmp_path/'bad.xlsx'
    with pytest.raises(UserError):
        export_workbook(book,items,out)
    assert not out.exists()


def test_missing_price_allowed_and_preserved(book,tmp_path):
    out = tmp_path/'no-price.xlsx'
    export_workbook(book,{'00000000003':'5'},out)
    assert load_workbook(out).by_code['00000000003'].price is None


def test_source_and_hardlink_are_protected(book,tmp_path):
    import os
    original = book.path.read_bytes()
    with pytest.raises(UserError):
        export_workbook(book,{'00000016700':'1'},book.path,replace=True)
    other = tmp_path/'linked.xlsx'
    os.link(book.path,other)
    with pytest.raises(UserError):
        export_workbook(book,{'00000016700':'1'},other,replace=True)
    assert book.path.read_bytes()==original


def test_failed_verification_keeps_previous_file(book,tmp_path,monkeypatch):
    import pricelisthandler.ooxml as module
    out = tmp_path/'existing.xlsx'
    out.write_bytes(b'previous export')
    def fail(*args):
        raise UserError('injected verification failure')
    monkeypatch.setattr(module,'verify_export',fail)
    with pytest.raises(UserError):
        export_workbook(book,{'00000016700':'1'},out,replace=True)
    assert out.read_bytes()==b'previous export'
    assert not list(tmp_path.glob('.plh-*'))


def test_write_failure_does_not_damage_previous_export(book,tmp_path,monkeypatch):
    out = tmp_path/'old.xlsx'
    out.write_bytes(b'old')
    def fail(*args,**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(ZipFile,'writestr',fail)
    with pytest.raises(OSError):
        export_workbook(book,{'00000016700':'1'},out,replace=True)
    assert out.read_bytes()==b'old'


def test_existing_file_cannot_be_overwritten_without_flag(book,tmp_path):
    out = tmp_path/'exists.xlsx'
    out.write_bytes(b'old')
    with pytest.raises(UserError):
        export_workbook(book,{'00000016700':'1'},out)
    assert out.read_bytes()==b'old'


@pytest.mark.parametrize('target',['custom/keep.xml','xl/worksheets/sheet1.xml'])
def test_independent_verifier_rejects_tampering(book,tmp_path,target):
    out = tmp_path/'order.xlsx'
    items = {'00000016700':'20'}
    export_workbook(book,items,out)
    with ZipFile(out) as z:
        data = z.read(target)
    data = data.replace(b'unchanged',b'changed') if target.startswith('custom') else data.replace(b'212.24',b'999.99')
    rewrite(out,{target:data})
    with pytest.raises(UserError):
        verify_export(book,out,items)


def test_verifier_rejects_wrong_g_and_style(book,tmp_path):
    out = tmp_path/'order.xlsx'
    items = {'00000016700':'20'}
    export_workbook(book,items,out)
    with ZipFile(out) as z:
        data=z.read(book.sheet)
    rewrite(out,{book.sheet:data.replace(b'<c r="G4" s="1"',b'<c r="G4" s="0"')})
    with pytest.raises(UserError):
        verify_export(book,out,items)


@pytest.mark.parametrize('kind',['headers','multiple_sheets','duplicate_code','missing_g','formula_g','numeric_code','bad_rel','entity','duplicate_row','duplicate_g'])
def test_fail_closed_on_ambiguous_format(tmp_path,kind):
    path = write_fixture(tmp_path/'unsafe.xlsx')
    with ZipFile(path) as z:
        sheet,workbook,rels = (z.read(n) for n in ('xl/worksheets/sheet1.xml','xl/workbook.xml','xl/_rels/workbook.xml.rels'))
    updates = {}
    if kind=='headers':
        sheet=sheet.replace('Заказано'.encode(),'Количество'.encode())
    elif kind=='multiple_sheets':
        workbook=workbook.replace(b'</sheets>',b'<sheet name="Other" sheetId="2" r:id="rId1"/></sheets>')
    elif kind=='duplicate_code':
        sheet=sheet.replace('ЦБ-00001868'.encode(),b'00000016700')
    elif kind=='missing_g':
        sheet=sheet.replace(b'<c r="G4" s="1" t="e"/>',b'')
    elif kind=='formula_g':
        sheet=sheet.replace(b'<c r="G4" s="1" t="e"/>',b'<c r="G4" s="1"><f>1+1</f><v>2</v></c>')
    elif kind=='numeric_code':
        sheet=sheet.replace(b'<c r="C4" s="1" t="inlineStr"><is><t>00000016700</t></is></c>',b'<c r="C4"><v>16700</v></c>')
    elif kind=='bad_rel':
        rels=rels.replace(b'Target="styles.xml"',b'Target="missing.xml"')
    elif kind=='entity':
        sheet=b'<!DOCTYPE worksheet [<!ENTITY x SYSTEM "file:///etc/passwd">]>'+sheet
    elif kind=='duplicate_row':
        sheet=sheet.replace(b'<row r="3">',b'<row r="2">')
    elif kind=='duplicate_g':
        sheet=sheet.replace(b'<c r="G4" s="1" t="e"/>',b'<c r="G4" s="1" t="e"/><c r="G4"/>')
    updates.update({'xl/worksheets/sheet1.xml':sheet,'xl/workbook.xml':workbook,'xl/_rels/workbook.xml.rels':rels})
    rewrite(path,updates)
    with pytest.raises(UserError):
        load_workbook(path)


@pytest.mark.parametrize('name',['../evil.xml','/evil.xml','C:/evil.xml','dir\\evil.xml'])
def test_zip_path_traversal_rejected(tmp_path,name):
    path = write_fixture(tmp_path/'bad-path.xlsx')
    with ZipFile(path,'a') as z:
        info=ZipInfo('raw.xml')
        info.filename=name
        info.orig_filename=name
        z.writestr(info,b'<evil/>')
    with pytest.raises(UserError):
        load_workbook(path)


def test_real_price_counts_and_twenty(real_book,tmp_path):
    b = real_book
    assert b.sha256=='71303589b849c5cb397aa733ab21d84aba459e36b06f79362b7c7baecef807eb'
    assert len(b.products)==11160
    assert sum(p.price is None for p in b.products)==108
    assert sum(p.stock is None for p in b.products)==198
    assert sum(not p.barcode for p in b.products)==423
    report = export_workbook(b,{'00000016700':'20'},tmp_path/'Real-20.xlsx')
    assert report['changed_cells']==['G4']
    assert report['unchanged_parts']==13
    import openpyxl
    wb=openpyxl.load_workbook(tmp_path/'Real-20.xlsx',read_only=True)
    assert wb.active['G4'].value==20
    assert wb.active['C4'].value=='00000016700'
    wb.close()


def test_real_many_rows_and_imported_cleanup(real_book,tmp_path):
    chosen = [p for p in real_book.products if p.stock is not None and p.stock>=3][::97]
    items = {p.code:'1' for p in chosen}
    first = tmp_path/'Many.xlsx'
    report = export_workbook(real_book,items,first)
    assert len(report['changed_cells'])==len(chosen)>50
    filled = load_workbook(first)
    items.pop(chosen[0].code)
    items[chosen[1].code]='2'
    second = tmp_path/'Cleared.xlsx'
    report = export_workbook(filled,items,second)
    assert set(report['changed_cells'])=={'G'+str(chosen[0].row),'G'+str(chosen[1].row)}
