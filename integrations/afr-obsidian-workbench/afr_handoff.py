"""The Workbench's local, versioned AFR/Obsidian handoff."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


TICKET = re.compile(r'wb-[a-f0-9]{32}')
REQUEST = re.compile(r'[a-f0-9]{32}')
SHA256 = re.compile(r'[a-f0-9]{64}')
QUESTION = re.compile(r'q-[a-f0-9]{32}')
VERSION = re.compile(r'[1-9][0-9]{0,17}')
DIRECTORY = 'Autoloop 工单交接'
LIMIT = 4 * 1024 * 1024
CONFIG_LIMIT = 8192


class HandoffError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _text(value, size=300, *, empty=False):
    if not isinstance(value, str) or len(value) > size or (not empty and not value.strip()):
        raise HandoffError('交接请求格式无效')
    return value


def _identifier(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HandoffError('交接编号无效')
    return value


def _fields(body, required, optional=()):
    if not isinstance(body, dict) or not set(required) <= body.keys() <= set(required) | set(optional):
        raise HandoffError('交接请求字段无效')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _obsidian(path):
    return 'obsidian://open?' + urlencode({'path': str(path)})


def _inline(value):
    text = ' '.join(str(value).split())
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*:(?=//)|\bwww\.|(?<=\w)@(?=\w))',
                  lambda match: match[0] + '\u200b', text)
    return re.sub(r'([\\`*_{}\[\]<>#!|])', r'\\\1', text)


def _plain_report(report):
    # 归档 Markdown 只作为文本显示，保留原内容并防止嵌入、外链或提前闭合。
    fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', report)), default=0))
    return (f'{fence}text', report.rstrip(), fence, '')


class Handoff:
    def __init__(self, vault_config: Path, store, archive, port: int):
        self.vault_config = Path(vault_config)
        self.store = store
        self.archive = archive
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Workbench port is invalid')
        self.port = port
        self.db = store.root / 'afr-handoff.sqlite3'

    def _settings(self):
        try:
            config = self.vault_config
            if not config.is_absolute() or config.resolve(strict=True) != config:
                raise ValueError
            fd = os.open(config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as file:
                info = os.fstat(file.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_size > CONFIG_LIMIT):
                    raise ValueError
                data = file.read(CONFIG_LIMIT + 1)
            if len(data) > CONFIG_LIMIT:
                raise ValueError
            settings = json.loads(data)
            if not isinstance(settings, dict) or set(settings) != {'root', 'python', 'vault'}:
                raise ValueError
            root, python, vault = (Path(settings[key]) for key in ('root', 'python', 'vault'))
            if (any(not p.is_absolute() for p in (root, python, vault))
                    or any(p.resolve(strict=True) != p or not p.is_dir() for p in (root, vault))
                    or root == vault or root in vault.parents or vault in root.parents
                    or not (root / 'afr_agent').is_dir()
                    or not python.is_file() or not os.access(python, os.X_OK)):
                raise ValueError
            return root, python, vault
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            raise HandoffError('AFR / Obsidian 本机交接配置不可用', 503) from error

    def _read(self, action, body):
        root, python, vault = self._settings()
        reader = Path(__file__).with_name('afr_handoff_reader.py')
        command = [str(python), '-I', '-B', str(reader), str(root), str(vault), action]
        if sys.platform == 'darwin':
            command = ['/usr/bin/sandbox-exec', '-p',
                       '(version 1)(allow default)(deny network-outbound)(deny file-write*)', *command]
        try:
            result = subprocess.run(
                command,
                input=json.dumps(body).encode(), capture_output=True, timeout=25, check=False,
            )
            if len(result.stdout) > LIMIT:
                raise ValueError
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError
            if 'error' in data:
                status = data.get('status')
                if type(status) is not int or not 400 <= status <= 599 or not isinstance(data['error'], str):
                    raise ValueError
                raise HandoffError(data['error'], status)
            if result.returncode or not isinstance(
                    data.get('items' if action == 'library' else action),
                    list if action == 'library' else dict):
                raise ValueError
            return data
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise HandoffError('AFR 本机查询暂不可用', 503) from error

    @contextmanager
    def _ledger(self):
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(self.db, flags, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise OSError('unsafe handoff ledger')
            finally:
                os.close(fd)
            db = sqlite3.connect(self.db, timeout=10, isolation_level='IMMEDIATE')
            try:
                with db:
                    db.execute('''CREATE TABLE IF NOT EXISTS attachment (
                        ticket TEXT, kind TEXT, id TEXT, revision TEXT, title TEXT NOT NULL,
                        obsidian TEXT, declared_at TEXT NOT NULL,
                        PRIMARY KEY (ticket, kind, id, revision)
                    )''')
                    db.execute('''CREATE TABLE IF NOT EXISTS export (
                        ticket TEXT, request TEXT, title TEXT NOT NULL, sha256 TEXT NOT NULL,
                        PRIMARY KEY (ticket, request)
                    )''')
                    yield db
            finally:
                db.close()
        except (OSError, sqlite3.Error) as error:
            raise HandoffError('本机交接记录暂不可用', 503) from error

    def _workbench(self, lookup, ticket):
        try:
            return lookup(ticket)
        except KeyError as error:
            raise HandoffError('工单不存在', 404) from error
        except Exception as error:
            status = getattr(error, 'status', None)
            if type(status) is not int or not 400 <= status <= 599:
                raise
            raise HandoffError(str(error), status) from error

    def _ticket(self, value):
        ticket = _identifier(value, TICKET)
        task = self._workbench(self.store.get, ticket)
        return {'id': ticket, 'name': task['name']}

    @staticmethod
    def _identity(body):
        kind = body['kind']
        if kind not in ('article', 'question'):
            raise HandoffError('资料类型无效')
        identifier_pattern, revision_pattern = (SHA256, SHA256) if kind == 'article' else (QUESTION, VERSION)
        return {'kind': kind, 'id': _identifier(body['id'], identifier_pattern),
                'revision': _identifier(body['revision'], revision_pattern)}

    @staticmethod
    def _attachment(row):
        kind, identifier, revision, title, obsidian, at = row
        entry = {'kind': kind, 'id': identifier, 'revision': revision, 'title': title, 'declaredAt': at}
        if obsidian:
            entry['obsidian'] = obsidian
        return entry

    def _attach(self, body):
        ticket = self._ticket(body['ticket'])['id']
        identity = self._identity(body)
        item = self._read('item', identity)['item']
        if any(item.get(key) != value for key, value in identity.items()):
            raise HandoffError('资料版本已变化，请重新选择', 409)
        title = _text(item.get('title'), 1024)
        key = 'canvas' if identity['kind'] == 'article' else 'question'
        obsidian = item.get('links', {}).get(key)
        if obsidian is not None:
            parsed = urlsplit(_text(obsidian, 8192))
            if parsed.scheme != 'obsidian' or parsed.netloc != 'open' or not parsed.query:
                raise HandoffError('资料的 Obsidian 入口无效', 409)
        values = tuple(identity.values())
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._ledger() as db:
            db.execute('INSERT OR IGNORE INTO attachment VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (ticket, *values, title, obsidian, timestamp))
            row = db.execute('''SELECT kind,id,revision,title,obsidian,declared_at FROM attachment
                                WHERE ticket=? AND kind=? AND id=? AND revision=?''',
                             (ticket, *values)).fetchone()
        return {'item': self._attachment(row)}

    @staticmethod
    @contextmanager
    def _directory(parent, name=None, *, create=False):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(name, flags, dir_fd=parent) if name else os.open(parent, flags)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _filename(ticket, request):
        return f'{ticket}-{request}.md'

    @staticmethod
    def _verify(directory, name, digest, *, links=1):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != links or info.st_size > LIMIT:
                raise HandoffError('Obsidian 笔记不是可核验的普通文件', 409)
            data = file.read(LIMIT + 1)
        if len(data) > LIMIT or _sha(data) != digest:
            raise HandoffError('Obsidian 笔记已被编辑，请保留现有文件', 409)

    @classmethod
    def _publish(cls, directory, name, data):
        digest = _sha(data)
        prefix = '.handoff-' + name + '-'
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 2:
                # 上次若在创建最终链接后中断，只清理同一文件的隐藏写入别名。
                cls._verify(directory, name, digest, links=2)
                for entry in os.listdir(directory):
                    if entry.startswith(prefix) and REQUEST.fullmatch(entry.removeprefix(prefix)):
                        alias = os.stat(entry, dir_fd=directory, follow_symlinks=False)
                        if (alias.st_dev, alias.st_ino) == (info.st_dev, info.st_ino):
                            os.unlink(entry, dir_fd=directory)
                            os.fsync(directory)
                            break
            cls._verify(directory, name, digest)
            return
        temp = prefix + secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            os.unlink(temp, dir_fd=directory)
        os.fsync(directory)
        cls._verify(directory, name, digest)

    def _report(self, task, round_, number):
        ticket, request = task['id'], round_['request']
        title = f'{task["name"]} · 研究交接'
        link = 'http://127.0.0.1:' + str(self.port) + '/?' + urlencode(
            {'task': ticket, 'panel': 'research', 'round': number, 'afrRequest': request}) + '#tasks'
        lines = [f'# {_inline(title)}', '', f'[回到 Autoloop 工单]({link})', '',
                 f'- 工单 `{ticket}`', f'- 轮次 `{request}`', '',
                 f'## {_inline(round_["query"])}', '']
        report = round_.get('report')
        if isinstance(report, str) and report.strip():
            lines.extend(_plain_report(report))
        elif isinstance(round_.get('answer'), str) and round_['answer'].strip():
            lines.extend((_inline(round_['answer']), ''))
        if round_['claims']:
            lines.extend(('## 已核对的观点与引文', ''))
        sources = {source['ref']: source for source in round_['read']}
        for claim in round_['claims']:
            lines.extend((f'### {_inline(claim.get("title") or "观点")}', '',
                          _inline(claim['statement']), ''))
            for cite in claim['citations']:
                source = sources[cite['ref']]
                lines.extend((f'> {_inline(cite["quote"])}', '',
                              f'出处 {_inline(source["heading"])} · 版本 `{source["revision"]}`', ''))
        if sources:
            lines.extend(('## 本轮固定原文', ''))
        for source in sources.values():
            lines.append(f'- {_inline(source["path"])} · 第 {source["line"]} 行 · 版本 `{source["revision"]}`')
        if round_.get('open_questions'):
            lines.extend(('', '## 待解问题', ''))
            lines.extend(f'- {_inline(question)}' for question in round_['open_questions'])
        data = ('\n'.join(lines).rstrip() + '\n').encode()
        if len(data) > LIMIT:
            raise HandoffError('本轮研究报告超过交接大小限制', 413)
        return title, data

    def _export(self, body):
        task = self._ticket(body['ticket'])
        ticket = task['id']
        request = _identifier(body['request'], REQUEST)
        _, _, vault = self._settings()
        archive = self._workbench(lambda tid: self.archive(self.store, tid), ticket)
        selected = next(((number, entry) for number, entry in enumerate(archive['rounds'], 1)
                         if entry['request'] == request), None)
        if selected is None:
            raise HandoffError('未找到本工单已完成并核验的研究轮次', 404)
        number, round_ = selected
        title, data = self._report(task, round_, number)
        filename, digest = self._filename(ticket, request), _sha(data)
        with self._ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT title,sha256 FROM export WHERE ticket=? AND request=?',
                             (ticket, request)).fetchone()
            try:
                with self._directory(vault) as root:
                    with self._directory(root, DIRECTORY, create=row is None) as directory:
                        if row:
                            title, digest = row
                            self._verify(directory, filename, digest)
                        else:
                            self._publish(directory, filename, data)
            except OSError as error:
                raise HandoffError('Obsidian 交接路径不可核验，现有文件已保留', 409) from error
            if not row:
                db.execute('INSERT INTO export VALUES (?, ?, ?, ?)', (ticket, request, title, digest))
        return {'export': self._export_entry(request, title, digest, 'ready', vault / DIRECTORY / filename)}

    @staticmethod
    def _export_entry(request, title, digest, state, path):
        result = {'request': request, 'title': title, 'sha256': digest, 'state': state}
        if state == 'ready':
            result['obsidian'] = _obsidian(path)
        return result

    def _ticket_data(self, body):
        task = self._ticket(body['ticket'])
        ticket = task['id']
        _, _, vault = self._settings()
        if not self.db.exists() and not self.db.is_symlink():
            return {'ticket': task, 'items': [], 'exports': []}
        with self._ledger() as db:
            rows = db.execute('''SELECT kind,id,revision,title,obsidian,declared_at FROM attachment
                                WHERE ticket=? ORDER BY declared_at,kind,id,revision''', (ticket,)).fetchall()
            exports = db.execute('SELECT request,title,sha256 FROM export WHERE ticket=? ORDER BY request',
                                 (ticket,)).fetchall()
        published = []
        for request, title, digest in exports:
            filename = self._filename(ticket, request)
            state = 'ready'
            try:
                with self._directory(vault) as root, self._directory(root, DIRECTORY) as directory:
                    self._verify(directory, filename, digest)
            except FileNotFoundError:
                state = 'missing'
            except (OSError, HandoffError):
                state = 'changed'
            published.append(self._export_entry(request, title, digest, state, vault / DIRECTORY / filename))
        return {'ticket': task, 'items': [self._attachment(row) for row in rows], 'exports': published}

    def handle(self, action: str, body: dict, *, write=False):
        if action == 'library' and not write:
            _fields(body, (), ('kind', 'q', 'offset'))
            kind = body.get('kind', 'article')
            if kind not in ('article', 'question'):
                raise HandoffError('资料类型无效')
            query = _text(body.get('q', ''), empty=True)
            offset = body.get('offset', 0)
            if isinstance(offset, str) and re.fullmatch(r'[0-9]{1,6}', offset):
                offset = int(offset)
            if type(offset) is not int or not 0 <= offset <= 100000:
                raise HandoffError('资料页码无效')
            return self._read('library', {'kind': kind, 'q': query, 'offset': offset})
        if action == 'item' and not write:
            _fields(body, ('kind', 'id', 'revision'))
            return self._read('item', self._identity(body))
        if action == 'topic' and not write:
            _fields(body, ('kind', 'id', 'revision', 'index'))
            identity = self._identity(body)
            if identity['kind'] != 'article':
                raise HandoffError('仅支持查询文章主题')
            index = body['index']
            if isinstance(index, str) and re.fullmatch(r'[1-9][0-9]{0,4}', index):
                index = int(index)
            if type(index) is not int or not 1 <= index <= 10000:
                raise HandoffError('主题编号无效')
            return self._read('topic', {**identity, 'index': index})
        if action == 'ticket' and not write:
            _fields(body, ('ticket',))
            return self._ticket_data(body)
        if action == 'attach' and write:
            _fields(body, ('ticket', 'kind', 'id', 'revision'))
            return self._attach(body)
        if action == 'export' and write:
            _fields(body, ('ticket', 'request'))
            return self._export(body)
        raise HandoffError('交接入口不存在', 404)
