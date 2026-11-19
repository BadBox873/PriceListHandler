"""Supplier boundary. New formats require an explicitly registered adapter."""
from pathlib import Path
from typing import Protocol

from .ooxml import Workbook,export_workbook,load_workbook


class SupplierAdapter(Protocol):
    id: str
    name: str

    def load(self,path: str | Path) -> Workbook: ...
    def export(self,book: Workbook,items: dict[str,str],path: Path,**options) -> dict: ...


class KhimbestAdapter:
    id='khimbest'
    name='ХИМБЭСТ'

    def load(self,path):
        return load_workbook(path)

    def export(self,book,items,path,**options):
        return export_workbook(book,items,path,**options)


ADAPTERS: dict[str,SupplierAdapter] = {'khimbest':KhimbestAdapter()}
