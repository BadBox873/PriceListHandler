"""Small reproducible OOXML fixture and packaged-application smoke test."""
from __future__ import annotations

import copy
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from .ooxml import NS, REL, export_workbook, load_workbook


def write_fixture(path: Path, products=None, original=None):
    products = products if products is not None else [
        {'row':4,'name':'Буренка мазь 200мл/24','detail':'','code':'00000016700','barcode':'4601234567890','price':'212.24','stock':'72'},
        {'row':5,'name':'Томат Акварель 20шт Уральский Дачник','detail':'Семена','code':'ЦБ-00001868','barcode':'4601234567891','price':'21.05','stock':'120'},
        {'row':6,'name':'Капуста Белая головушка','detail':'СеДеК','code':'00000000003','barcode':'','price':None,'stock':'50'},
        {'row':7,'name':'Томат Боец','detail':'Семена','code':'00000000004','barcode':'','price':'15','stock':None},
        {'row':8,'name':'Рукола ореховая','detail':'Фазенда Сибири','code':'00000000005','barcode':'','price':'12.34','stock':'0'},
    ]
    original = original or {}
    headers = ['Номенклатура','Характеристика','Код','Штрих-код','Спец. цена','Остаток','Заказано']
    def c(col,r,value,kind='inlineStr'):
        address = col+str(r)
        if value is None:
            return f'<c r="{address}" s="1" t="e"/>'
        if kind=='inlineStr':
            return f'<c r="{address}" s="1" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
        return f'<c r="{address}" s="1" t="{kind}"><v>{escape(str(value))}</v></c>'
    rows = ['<row r="1">'+''.join(c(col,1,h) for col,h in zip('ABCDEFG',headers))+'</row>']
    for r,label in [(2,'Зоотовары'),(3,'Мази и средства')]:
        rows.append(f'<row r="{r}">'+c('A',r,label)+c('C',r,None)+c('E',r,'999','n')+c('F',r,'99','n')+c('G',r,None)+'</row>')
    for p in products:
        r = p['row']
        fields = [('A',p['name'],'inlineStr'),('B',p.get('detail',''),'inlineStr'),('C',p['code'],'inlineStr'),
                  ('D',p.get('barcode',''),'inlineStr'),('E',p.get('price'),'n'),('F',p.get('stock'),'n')]
        order = original.get(r)
        if isinstance(order,tuple):
            g = c('G',r,order[1],order[0])
        else:
            g = c('G',r,order,'n')
        rows.append(f'<row r="{r}">'+''.join(c(col,r,v,t) for col,v,t in fields)+g+'</row>')
    end = max(p['row'] for p in products)+1
    rows.append(f'<row r="{end}">'+c('A',end,'Итого')+c('C',end,None)+c('G',end,None)+'</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>\r\n'+f'<worksheet xmlns="{NS}" xmlns:r="{REL}"><sheetData>'+''.join(rows)+
             '</sheetData><pageMargins left="0.3" right="0.3" top="0.3" bottom="0.3" header="0" footer="0"/></worksheet>').encode()
    package_rel = 'http://schemas.openxmlformats.org/package/2006/relationships'
    parts = {
        '[Content_Types].xml':f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        '_rels/.rels':f'<Relationships xmlns="{package_rel}"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml':f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets><sheet name="Лист_1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':f'<Relationships xmlns="{package_rel}"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{REL}/styles" Target="styles.xml"/></Relationships>',
        'xl/styles.xml':f'<styleSheet xmlns="{NS}"><fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
        'xl/worksheets/sheet1.xml':sheet,
        'custom/keep.xml':'<keep attr="unchanged"><text>Комментарии, VML и неизвестные части не пересоздаются.</text></keep>',
    }
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        z.comment = b'Preserve archive comment'
        for name,data in parts.items():
            z.writestr(name,data.encode() if isinstance(data,str) else data)
    return path


def run_smoke(store,window,folder):
    path = write_fixture(folder/'Контрольный прайс.xlsx')
    book = load_workbook(path)
    oid = store.create('Контрольный заказ — запуск сборки',book)
    window.open_order(oid)
    window.set_quantity('00000016700','20')
    window.set_quantity('ЦБ-00001868','10')
    window.set_quantity('00000000003','5')
    state = store.get(oid)['state']
    output = folder/'Проверенный заказ.xlsx'
    report = export_workbook(store.book(state['price']),state['items'],output,protected=store.protected_paths())
    store.complete_export(oid,output,report,expected=state)
    assert store.get(oid)['status']=='archive'
    clone = store.restore(oid)
    window.open_order(clone)
    window.result_model.set_products(book.products,state['items'])
    backup = store.backup()
    summary = store.inspect_backup(backup)
    return {'ui_started':True,'parser':len(book.products),'export':report,'backup':summary,
            'archive_restore':True,'data_directory':str(store.root)}
