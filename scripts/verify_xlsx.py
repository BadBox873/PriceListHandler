"""Re-verify an exported order against its original price list."""
import argparse
import json
from pathlib import Path
from pricelisthandler.ooxml import load_workbook, verify_export


def main():
    parser=argparse.ArgumentParser(description='Проверить изменение только разрешённых значений G.')
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    source=load_workbook(args.source)
    output=load_workbook(args.output)
    report=verify_export(source,args.output,output.imported)
    text=json.dumps(report,ensure_ascii=False,indent=2)
    print(text)
    if args.report:
        args.report.write_text(text,encoding='utf-8')


if __name__=='__main__':
    main()
