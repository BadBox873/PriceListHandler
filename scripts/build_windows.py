"""Build Windows installer and portable bundle. Run on Windows x64."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from pricelisthandler import __version__

ROOT=Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(list(map(str,args)),cwd=ROOT,check=True)


def main():
    if os.name!='nt':
        raise SystemExit('Сборка Windows выполняется на Windows. Используй GitHub Actions или Windows 11 x64.')
    release=ROOT/'release'
    release.mkdir(exist_ok=True)
    run(sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
        '--name','PriceListHandler','--paths','src','--hidden-import','pricelisthandler.smoke',
        '--exclude-module','PySide6.QtWebEngineCore','--exclude-module','PySide6.QtWebEngineWidgets',
        '--exclude-module','PySide6.QtQml','--exclude-module','PySide6.QtQuick','run.py')
    app=ROOT/'dist/PriceListHandler'
    for name in ['README.md','LICENSE']:
        shutil.copy2(ROOT/name,app/name)
    shutil.copy2(ROOT/'docs/Руководство.md',app/'Руководство.md')
    portable=ROOT/'build/portable/PriceListHandler'
    if portable.exists():
        shutil.rmtree(portable)
    portable.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(app,portable)
    (portable/'portable.flag').write_text('Portable data: Data next to this executable.\n',encoding='utf-8')
    shutil.make_archive(str(release/f'PriceListHandler-{__version__}-Portable-x64'),'zip',portable.parent,'PriceListHandler')
    compiler=Path(os.environ.get('PROGRAMFILES(X86)',r'C:\Program Files (x86)'))/'Inno Setup 6/ISCC.exe'
    if not compiler.is_file():
        compiler=Path(shutil.which('ISCC.exe') or '')
    if not compiler.is_file():
        raise SystemExit('Не найден Inno Setup 6 (ISCC.exe). Установи его и повтори сборку.')
    run(compiler,f'/DAppVersion={__version__}',ROOT/'packaging/installer.iss')
    hashes=[]
    for path in sorted(release.iterdir()):
        if path.suffix in ('.exe','.zip'):
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name)
    (release/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='utf-8')
    print('\n'.join(hashes))


if __name__=='__main__':
    main()
