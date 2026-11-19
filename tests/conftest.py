import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest

from pricelisthandler.ooxml import load_workbook
from pricelisthandler.smoke import write_fixture
from pricelisthandler.storage import Store


@pytest.fixture
def book(tmp_path):
    return load_workbook(write_fixture(tmp_path/'Прайс с пробелами.xlsx'))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/'Данные программы')


@pytest.fixture(scope='session')
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope='session')
def real_book():
    p = Path(os.environ.get('PRICE_REAL_FILE',str(Path(__file__).resolve().parents[2]/'reference/Эталон_ХИМБЭСТ_16.11.xlsx')))
    if not p.exists():
        pytest.skip('Настоящий прайс не включён в публичный репозиторий; задай PRICE_REAL_FILE для локальной проверки.')
    return load_workbook(p)
