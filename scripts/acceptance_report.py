"""Run a real-file acceptance roundtrip without publishing commercial data."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from pricelisthandler.domain import search
from pricelisthandler.ooxml import digest,export_workbook,load_workbook


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('price',type=Path)
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    book=load_workbook(args.price)
    initial=digest(args.price.read_bytes())
    timings=[]
    for query in ['мазь буренка','ЦБ-00001868','томат','белая головушка']:
        start=time.perf_counter()
        found,fuzzy=search(book.products,query,fuzzy=False)
        timings.append({'query':query,'milliseconds':round((time.perf_counter()-start)*1000,2),'matches':len(found)})
    with tempfile.TemporaryDirectory() as folder:
        folder=Path(folder)
        single=export_workbook(book,{'00000016700':'20'},folder/'single.xlsx')
        selected=[p for p in book.products if p.stock is not None and p.stock>=3][::97]
        items={p.code:'1' for p in selected}
        many=export_workbook(book,items,folder/'many.xlsx')
        imported=load_workbook(folder/'many.xlsx')
        del items[selected[0].code]
        items[selected[1].code]='2'
        edited=export_workbook(imported,items,folder/'edited.xlsx')
        import openpyxl
        wb=openpyxl.load_workbook(folder/'edited.xlsx',read_only=True)
        assert wb.active['G'+str(selected[0].row)].value is None
        assert wb.active['G'+str(selected[1].row)].value==2
        wb.close()
    assert initial==digest(args.price.read_bytes())
    report={'source_sha256':initial,'source_unchanged':True,'products':len(book.products),
            'missing_price':sum(p.price is None for p in book.products),'unknown_stock':sum(p.stock is None for p in book.products),
            'missing_barcode':sum(not p.barcode for p in book.products),'search':timings,
            'single_order':single,'many_order':many,'edit_and_clear':edited,'independent_openpyxl_read':True,
            'microsoft_excel_visual_test':'not_performed','supplier_1c_test':'not_performed','interactive_windows11_test':'not_performed'}
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Verified',report['products'],'products;',len(many['changed_cells']),'ordered cells; edit and clear passed.')


if __name__=='__main__':
    main()
