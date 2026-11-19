from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .domain import UserError, normalize, totals
from .ooxml import Workbook, digest, load_workbook

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources = self.root / "Sources"
        self.sources.mkdir(exist_ok=True)
        self.db_path = self.root / "orders.sqlite3"
        self.lock = threading.RLock()
        self.cache: dict[str, Workbook] = {}
        try:
            with self.db() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise UserError("Эта база создана более новой версией программы. Обнови приложение; база не изменена.")
                db.executescript("""
                CREATE TABLE IF NOT EXISTS prices(sha TEXT PRIMARY KEY, filename TEXT NOT NULL, external_path TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS original_paths(path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                  previous_status TEXT, parent_id TEXT, created TEXT NOT NULL, modified TEXT NOT NULL,
                  state TEXT NOT NULL, history TEXT NOT NULL, cursor INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases(supplier TEXT NOT NULL, alias TEXT NOT NULL, code TEXT NOT NULL,
                  PRIMARY KEY(supplier,alias));
                CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, order_id TEXT NOT NULL, parent_id TEXT,
                  price_sha TEXT NOT NULL, date TEXT NOT NULL, path TEXT NOT NULL, count INTEGER NOT NULL,
                  total TEXT NOT NULL, incomplete INTEGER NOT NULL, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES('revision','0');
                PRAGMA user_version=1;
                """)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise UserError("Нарушена целостность базы. Выбери проверенную резервную копию для восстановления.")
        except sqlite3.Error as exc:
            raise UserError(f"Не удалось открыть данные приложения: {exc}") from exc

    @contextmanager
    def db(self, *, write: bool = False):
        with self.lock:
            db = sqlite3.connect(self.db_path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            try:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
                if write:
                    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def setting(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        if self.setting(key) == value:
            return
        with self.db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dumps(value)))

    def register_price(self, book: Workbook) -> str:
        target = self.sources / (book.sha256 + ".xlsx")
        with self.lock:
            if target.exists():
                if digest(target.read_bytes()) != book.sha256:
                    raise UserError("Внутренняя копия прайса повреждена. Используй восстановление из резервной копии.")
            else:
                fd, tmp = tempfile.mkstemp(dir=self.sources, suffix=".tmp")
                try:
                    with os.fdopen(fd, "wb") as f:
                        data = book.path.read_bytes()
                        if digest(data) != book.sha256:
                            raise UserError("Внешний прайс изменился во время импорта. Открой его заново.")
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, target)
                finally:
                    Path(tmp).unlink(missing_ok=True)
            with self.db(write=True) as db:
                db.execute("INSERT OR IGNORE INTO prices VALUES(?,?,?)", (book.sha256, book.path.name, str(book.path)))
                db.execute("INSERT OR IGNORE INTO original_paths VALUES(?)", (str(book.path),))
        self.cache[book.sha256] = load_workbook(target)
        return book.sha256

    def book(self, sha: str) -> Workbook:
        p = self.sources / (sha + ".xlsx")
        if not p.exists() or digest(p.read_bytes()) != sha:
            raise UserError("Неизменяемая копия прайса отсутствует или повреждена. Восстанови резервную копию.")
        if sha not in self.cache:
            self.cache[sha] = load_workbook(p)
        return self.cache[sha]

    def protected_paths(self) -> tuple[Path, ...]:
        with self.db() as db:
            rows = db.execute("SELECT sha,external_path FROM prices").fetchall()
            originals = db.execute("SELECT path FROM original_paths").fetchall()
        return tuple(p for r in rows for p in (Path(r['external_path']), self.sources / (r['sha'] + '.xlsx'))) + tuple(Path(r[0]) for r in originals)

    def price_name(self, sha):
        with self.db() as db:
            r = db.execute("SELECT filename FROM prices WHERE sha=?", (sha,)).fetchone()
        return r[0] if r else sha

    def create(self, name: str, book: Workbook):
        if not name.strip():
            raise UserError("Укажи имя заказа.")
        sha = self.register_price(book)
        state = {"supplier": "khimbest", "price": sha, "items": book.imported, "bulk": [], "bulk_text": ""}
        return self._insert(name.strip(), state)

    def _insert(self, name, state, parent=None, history=None, cursor=0):
        oid, date = uuid.uuid4().hex, now()
        history = history if history is not None else [state]
        with self.db(write=True) as db:
            db.execute("INSERT INTO orders VALUES(?,?,?,NULL,?,?,?,?,?,?)",
                       (oid, name, "draft", parent, date, date, dumps(state), dumps(history), cursor))
        return oid

    def get(self, oid):
        with self.db() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise UserError("Заказ больше не существует.")
        data = dict(row)
        data["state"], data["history"] = json.loads(data["state"]), json.loads(data["history"])
        return data

    def change(self, oid, state, *, expected=None):
        with self.db(write=True) as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not row or row["status"] != "draft":
                raise UserError("Редактирование доступно только активному черновику.")
            if expected is not None and json.loads(row["state"]) != expected:
                raise UserError("Заказ уже изменён другой операцией. Обнови окно и повтори действие.")
            if json.loads(row["state"]) == state:
                return
            history = json.loads(row["history"])[:row["cursor"] + 1]
            history.append(state)
            db.execute("UPDATE orders SET state=?,history=?,cursor=?,modified=? WHERE id=?",
                       (dumps(state), dumps(history), len(history)-1, now(), oid))

    def undo(self, oid, step=-1):
        with self.db(write=True) as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not row or row["status"] != "draft":
                return
            history = json.loads(row["history"])
            cursor = max(0, min(len(history)-1, row["cursor"] + step))
            db.execute("UPDATE orders SET cursor=?,state=?,modified=? WHERE id=?", (cursor,dumps(history[cursor]),now(),oid))

    def rename(self, oid, name):
        if not name.strip():
            raise UserError("Название не может быть пустым.")
        with self.db(write=True) as db:
            db.execute("UPDATE orders SET name=?,modified=? WHERE id=?", (name.strip(),now(),oid))

    def list_orders(self, status="draft", query="", start="", end="", sort="modified", descending=True):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM orders WHERE status=?",(status,))]
        out = []
        words = normalize(query).split()
        for row in rows:
            if start and row['modified'][:10] < start or end and row['modified'][:10] > end:
                continue
            state = json.loads(row["state"])
            book = self.book(state["price"])
            content = normalize(row["name"] + " " + " ".join(book.by_code[c].name + " " + c for c in state["items"] if c in book.by_code))
            if not all(w in content for w in words):
                continue
            total, incomplete = totals(book.by_code, state["items"])
            row.update(total=total, incomplete=incomplete, count=len(state["items"]))
            out.append(row)
        key = {"modified":lambda x:x['modified'], "name":lambda x:normalize(x['name']),
               "count":lambda x:x['count'], "total":lambda x:x['total']}[sort]
        return sorted(out,key=key,reverse=descending)

    def trash(self, oid):
        with self.db(write=True) as db:
            db.execute("UPDATE orders SET previous_status=status,status='trash',modified=? WHERE id=? AND status!='trash'",(now(),oid))

    def restore(self, oid):
        row = self.get(oid)
        if row["status"] == "trash":
            with self.db(write=True) as db:
                db.execute("UPDATE orders SET status=COALESCE(previous_status,'draft'),previous_status=NULL,modified=? WHERE id=?",(now(),oid))
            return oid
        if row["status"] == "archive":
            return self._insert(row['name'] + " — восстановлен", row['state'], oid, row['history'], row['cursor'])
        raise UserError("Заказ уже находится в работе.")

    def trash_size(self, oid=None):
        with self.db() as db:
            all_rows = db.execute("SELECT id,status,history,state FROM orders").fetchall()
        targets = [r for r in all_rows if r['status']=='trash' and (oid is None or r['id']==oid)]
        target_ids = {r['id'] for r in targets}
        def refs(rows):
            return {s['price'] for r in rows for s in json.loads(r['history']) + [json.loads(r['state'])]}
        removable = refs(targets) - refs([r for r in all_rows if r['id'] not in target_ids])
        size = sum((self.sources/(s+'.xlsx')).stat().st_size for s in removable if (self.sources/(s+'.xlsx')).exists())
        size += sum(len(r['history'].encode())+len(r['state'].encode()) for r in targets)
        return len(targets),size

    def purge(self, oid=None):
        with self.db(write=True) as db:
            if oid:
                db.execute("DELETE FROM orders WHERE id=? AND status='trash'",(oid,))
            else:
                db.execute("DELETE FROM orders WHERE status='trash'")
            used = {s['price'] for r in db.execute("SELECT history,state FROM orders")
                    for s in json.loads(r['history']) + [json.loads(r['state'])]}
            # Keep metadata (including protected original paths); remove only unreferenced internal files.
        for p in self.sources.glob('*.xlsx'):
            if p.stem not in used:
                p.unlink(missing_ok=True)
                self.cache.pop(p.stem,None)

    def complete_export(self, oid, path, report, *, expected):
        with self.db(write=True) as db:
            row = db.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
            if not row or row['status']!='draft' or json.loads(row['state'])!=expected:
                raise UserError("Файл сохранён, но состояние заказа изменилось. Автоматическое архивирование остановлено.")
            book = self.book(expected['price'])
            total,incomplete = totals(book.by_code,expected['items'])
            db.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex,oid,row['parent_id'],expected['price'],now(),str(path),len(expected['items']),str(total),int(incomplete),dumps(report)))
            db.execute("UPDATE orders SET status='archive',modified=? WHERE id=?",(now(),oid))

    def exports(self, oid=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM exports WHERE order_id=? ORDER BY date DESC",(oid,)) if oid else db.execute("SELECT * FROM exports ORDER BY date DESC")
            return [dict(r) for r in rows]

    def aliases(self, query=""):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM aliases WHERE supplier='ХИМБЭСТ' ORDER BY alias")]
        return [r for r in rows if normalize(query) in normalize(r['alias']+' '+r['code'])]

    def save_alias(self, alias, code, old=None):
        if not normalize(alias) or not code.strip():
            raise UserError("Укажи альтернативное название и код.")
        with self.db(write=True) as db:
            if old:
                db.execute("DELETE FROM aliases WHERE supplier='ХИМБЭСТ' AND alias=?",(old,))
            db.execute("INSERT OR REPLACE INTO aliases VALUES('ХИМБЭСТ',?,?)",(normalize(alias),code))

    def delete_alias(self, alias):
        with self.db(write=True) as db:
            db.execute("DELETE FROM aliases WHERE supplier='ХИМБЭСТ' AND alias=?",(alias,))

    def backup_dir(self):
        return Path(self.setting("backup_dir", str(self.root/'Backups')))

    def fingerprint(self):
        with self.db() as db:
            return db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]

    def daily_backup(self):
        revision = self.fingerprint()
        marker = self.root / 'backup-marker.json'
        try:
            previous = json.loads(marker.read_text('utf-8')) if marker.exists() else {}
        except (OSError,ValueError):
            previous = {}
        date = now()[:10]
        if previous.get('date') == date or previous.get('revision') == revision:
            return None
        with self.db() as db:
            if not db.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
                return None
        path = self.backup()
        temp = marker.with_suffix('.tmp')
        temp.write_text(dumps({'date':date,'revision':revision}),encoding='utf-8')
        os.replace(temp,marker)
        return path

    def backup(self, *, protective=False):
        folder = self.backup_dir()
        folder.mkdir(parents=True,exist_ok=True)
        name = ('protect-' if protective else 'backup-') + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.zip'
        target = folder / name
        with self.lock, tempfile.TemporaryDirectory(prefix='plh-backup-') as tmp:
            tmp = Path(tmp)
            with self.db() as source:
                destination = sqlite3.connect(tmp/'orders.sqlite3')
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            files = {'orders.sqlite3':(tmp/'orders.sqlite3').read_bytes()}
            for p in self.sources.glob('*.xlsx'):
                files['Sources/'+p.name] = p.read_bytes()
            manifest = {n:digest(b) for n,b in files.items()}
            temporary = target.with_suffix('.tmp')
            try:
                with ZipFile(temporary,'w',ZIP_DEFLATED) as z:
                    for n,b in files.items():
                        z.writestr(n,b)
                    z.writestr('manifest.json',dumps({'schema':SCHEMA_VERSION,'created':now(),'files':manifest}))
                self.inspect_backup(temporary)
                os.replace(temporary,target)
            finally:
                temporary.unlink(missing_ok=True)
        # Protective rollback points are separate and never rotated automatically.
        successful = sorted(folder.glob('backup-*.zip'),reverse=True)
        for p in successful[10:]:
            p.unlink()
        return target

    @staticmethod
    def inspect_backup(path):
        try:
            with ZipFile(path) as z:
                names = z.namelist()
                if len(names)!=len(set(names)) or len(names)>2000 or sum(i.file_size for i in z.infolist())>1024*1024*1024:
                    raise UserError("Недопустимый размер или дубли файлов резервной копии.")
                if any(n not in ('orders.sqlite3','manifest.json') and not __import__('re').fullmatch(r'Sources/[0-9a-f]{64}\.xlsx',n) for n in names):
                    raise UserError("Неизвестные или небезопасные пути в резервной копии.")
                manifest = json.loads(z.read('manifest.json'))
                if manifest['schema'] != SCHEMA_VERSION or set(names) != set(manifest['files']) | {'manifest.json'}:
                    raise UserError("Несовместимая или неполная резервная копия.")
                data = {n:z.read(n) for n in manifest['files']}
                if any(digest(b)!=manifest['files'][n] for n,b in data.items()):
                    raise UserError("Не совпали контрольные суммы резервной копии.")
            with tempfile.TemporaryDirectory(prefix='plh-inspect-') as tmp:
                dbpath = Path(tmp)/'orders.sqlite3'
                dbpath.write_bytes(data['orders.sqlite3'])
                db = sqlite3.connect(dbpath)
                try:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION:
                        raise UserError("Повреждённая база в резервной копии.")
                    rows = db.execute('SELECT state,history FROM orders').fetchall()
                    required = {s['price'] for state,history in rows for s in json.loads(history)+[json.loads(state)]}
                    for sha in required:
                        if 'Sources/'+sha+'.xlsx' not in data or digest(data['Sources/'+sha+'.xlsx']) != sha:
                            raise UserError("Не хватает исходного прайса для истории заказов.")
                    for sha in required:
                        p = Path(tmp)/(sha+'.xlsx')
                        p.write_bytes(data['Sources/'+sha+'.xlsx'])
                        book = load_workbook(p)
                        for state,history in rows:
                            for s in json.loads(history)+[json.loads(state)]:
                                if s['price']==sha and not set(s['items']) <= set(book.by_code):
                                    raise UserError("В резервной копии нарушены связи товаров и заказов.")
                finally:
                    db.close()
            return {'created':manifest['created'],'orders':len(rows),'sources':len(required),'files':len(data)}
        except UserError:
            raise
        except Exception as exc:
            raise UserError(f"Резервная копия не прошла проверку: {exc}") from exc

    def restore_backup(self, path):
        summary = self.inspect_backup(path)
        rollback = self.backup(protective=True)
        with self.lock, tempfile.TemporaryDirectory(prefix='plh-restore-',dir=self.root) as temp:
            temp = Path(temp)
            with ZipFile(path) as z:
                manifest = json.loads(z.read('manifest.json'))
                for n in manifest['files']:
                    p = temp/n
                    p.parent.mkdir(parents=True,exist_ok=True)
                    p.write_bytes(z.read(n))
            (temp/'Sources').mkdir(exist_ok=True)
            old_db,old_sources = temp/'old.sqlite3',temp/'OldSources'
            # An on-disk journal permits recovery after a process/machine interruption.
            journal = self.root/'restore-journal.json'
            journal.write_text(dumps({'rollback':str(rollback),'stage':str(temp)}),encoding='utf-8')
            try:
                shutil.copy2(self.db_path,old_db)
                shutil.copytree(self.sources,old_sources)
                for p in (temp/'Sources').glob('*.xlsx'):
                    os.replace(p,self.sources/p.name)
                # Database replacement is the commit point. All sources already exist.
                os.replace(temp/'orders.sqlite3',self.db_path)
                self.cache.clear()
                with self.db() as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise UserError("Не удалось подтвердить восстановление базы.")
                # Extra old sources are harmless and removed only after the commit.
                wanted = {n.split('/')[-1] for n in manifest['files'] if n.startswith('Sources/')}
                for p in self.sources.glob('*.xlsx'):
                    if p.name not in wanted:
                        p.unlink()
                journal.unlink()
            except Exception:
                if old_db.exists():
                    os.replace(old_db,self.db_path)
                if old_sources.exists():
                    for p in old_sources.glob('*.xlsx'):
                        shutil.copy2(p,self.sources/p.name)
                self.cache.clear()
                journal.unlink(missing_ok=True)
                raise
        return summary,rollback
