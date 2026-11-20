"""Single-ticket, local AFR research worker. Only the bounded receipt leaves the machine."""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time

from afr_command_agent import HEX, Reject, REQUEST, SOURCE, identity, private_request
from afr_research_models import OfflineCommand, SyntheticCodex, SYNTHETIC, check_synthetic, init_synthetic

EVENT = 'afr.research.requested'
TICKET = re.compile(r'wb-[a-f0-9]{32}')
CANVAS = '研究白板.canvas'
RECORD = '研究记录.json'
SOURCES = '原文出处.md'
MAX_READ = 24
MAX_BATCH = 6
MAX_TURNS = 9
LIST_SIZE = 18
MAX_TEXT_BYTES = 48_000
TERMINAL_ERRORS = {
    'invalid_event', 'invalid_request', 'invalid_configuration', 'invalid_local_input', 'request_conflict',
    'invalid_parent', 'invalid_research_history', 'stale_parent', 'synthetic_scope_denied', 'offline_sandbox_unavailable', 'model_failed',
    'invalid_action', 'invalid_citation', 'action_budget', 'read_budget', 'source_too_large', 'no_match',
    'revision_changed', 'not_found', 'afr_timeout', 'afr_response_limit', 'invalid_afr_response',
    'read_verification_failed', 'invalid_vault', 'io_error', 'worker_busy',
}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def decode_event(context):
    events = context['events']
    if not isinstance(events, list) or len(events) != 1:
        raise Reject('invalid_event')
    event = events[0]
    identifier = event.get('subject')
    if (event.get('source') != SOURCE or event.get('type') != EVENT or
            not isinstance(identifier, str) or not REQUEST.fullmatch(identifier) or
            any(event.get(key) for key in ('detail', 'summary', 'link'))):
        raise Reject('invalid_event')
    return identifier


def decode_request(value, request_id):
    if (not isinstance(value, dict) or set(value) not in
            ({'ticket', 'query', 'parent'}, {'ticket', 'query', 'parent', 'consent'}) or
            ('consent' in value and value['consent'] != 'synthetic_query_to_codex')):
        raise Reject('invalid_request')
    ticket, query, parent = value['ticket'], value['query'], value['parent']
    if (not isinstance(ticket, str) or not TICKET.fullmatch(ticket) or
            not isinstance(query, str) or not query.strip() or len(query.encode()) > 3000 or '\0' in query or
            (parent is not None and (not isinstance(parent, str) or not REQUEST.fullmatch(parent)
                                     or parent == request_id))):
        raise Reject('invalid_request')
    return value


class ScopedNote:
    """A path-shaped input to AFR sections, with descriptor-relative reads."""
    def __init__(self, path):
        self.path = path

    def relative_to(self, vault):
        self.vault = vault
        return self.path.relative_to(vault)

    def read_text(self, encoding):
        directories = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as stack:
            fd = os.open(self.vault, directories)
            stack.callback(os.close, fd)
            parts = self.path.relative_to(self.vault).parts
            for component in parts[:-1]:
                fd = os.open(component, directories, dir_fd=fd)
                stack.callback(os.close, fd)
            file = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(file, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2_000_000:
                    raise OSError('unsupported_note')
                data = stream.read(2_000_001)
                if len(data) > 2_000_000:
                    raise OSError('oversized_note')
        return data.decode(encoding).replace('\r\n', '\n').replace('\r', '\n')


class AfrResearchLibrary:
    def __init__(self, root, vault, output, synthetic):
        self.vault = vault
        self.output = output
        self.synthetic = synthetic
        spec = importlib.util.spec_from_file_location('_afr_research_library', root/'afr_agent/library.py')
        self.library = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.library)
        raw_notes, raw_sections = self.library.notes, self.library.sections

        def notes(vault):
            for note in raw_notes(vault):
                if self.visible(note.relative_to(vault).as_posix()):
                    yield note

        def sections(vault, path, skipped):
            return raw_sections(vault, ScopedNote(path), skipped)

        self.library.notes = notes
        self.library.sections = sections

    def visible(self, path):
        relative = Path(path)
        if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.md':
            return False
        if self.synthetic and path not in SYNTHETIC:
            return False
        return not (self.vault / relative).is_relative_to(self.output)

    def rows(self):
        # 每份材料先列首段，再列其余有定位的段落，避免长文垄断首屏。
        first, rest, skipped = [], [], []
        bounded = False
        for index, path in enumerate(self.library.notes(self.vault)):
            if index >= 1000:
                bounded = True
                break
            for current, (record, _) in enumerate(self.library.sections(self.vault, path, skipped)):
                (first if current == 0 else rest).append(self.metadata(record))
                if len(first) + len(rest) >= 2000:
                    bounded = True
                    break
            if bounded:
                break
        return first + rest, len(skipped), bounded

    @staticmethod
    def metadata(record):
        return dict(identity(record), path=record['path'], heading=record['heading'], line=record['line'])

    def search(self, query, deadline):
        result = self.library.search(self.vault, query, 100)
        hits = [self.metadata(row) for row in result['hits']]
        return hits, result['skipped_notes'], len(hits) == 100

    def read(self, record, deadline):
        try:
            result = self.library.read(self.vault, record['ref'], record['revision'])
        except self.library.RequestError as error:
            if str(error) not in ('invalid_request', 'revision_changed', 'not_found'):
                raise Reject('invalid_afr_response') from None
            raise Reject(str(error)) from None
        text = result.get('text')
        verified = self.metadata(result)
        if (identity(result) != identity(record) or not self.visible(verified['path']) or
                not isinstance(text, str) or not text or sha(text) != verified['revision']):
            raise Reject('read_verification_failed')
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise Reject('source_too_large')
        return dict(verified, text=text)

def action(kind, query='', refs=(), claims=(), questions=()):
    return dict(action=kind, query=query, refs=list(refs), claims=list(claims), open_questions=list(questions))


def _terms(question):
    # 无本机模型时可以为任意问题提供有证据的检索结果。常见中文语气词只用于切分关键词。
    parts = re.split(r'[^\w]+|请问|帮我|哪些|以及|如何|什么|怎么|梳理|研究|试点的|材料|对比|比较|和|与|的',
                     question, flags=re.UNICODE)
    words = [part[:50] for part in parts if part.strip()]
    pairs = [word[i:i+2] for word in words if re.search(r'[\u3400-\u9fff]', word)
             for i in range(len(word)-1)]
    return list(dict.fromkeys([*words[:3], *pairs, *words[3:]]))[:6]


def _quote(text):
    body = next((line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')), text)
    return body[:170]


def _literal(value):
    # 防止资料或生成结果在 Obsidian 中被当作嵌入、外链或 HTML 执行。
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
    return re.sub(r'([\\`*_{}\[\]()#+.!<>|~:$%])', r'\\\1', value)


class LocalExtract:
    name = 'local-extract'

    def __init__(self, question):
        self.terms = _terms(question)
        self.tested = 0
        self.calls = []

    def __call__(self, context, timeout):
        refs = [row['ref'] for row in context['candidates']]
        remaining = MAX_READ - len(context['read'])
        if refs and remaining and 'read' in context['available_actions']:
            return action('read', refs=refs[:min(MAX_BATCH, remaining)])
        if self.tested < len(self.terms) and context['turns_left'] > 1 and remaining:
            self.tested += 1
            return action('search', query=self.terms[self.tested-1])
        claims = [
            {'statement': _quote(row['text']), 'citations': [{'ref':row['ref'], 'quote':_quote(row['text'])}]}
            for row in context['read']
        ]
        questions = ['这些摘录分别支持哪些判断，哪些问题还缺少来源或时间证据？']
        return action('finish', claims=claims, questions=questions)


def _bounded_strings(values, count, size):
    return (isinstance(values, list) and len(values) <= count and
            all(isinstance(value, str) and 0 < len(value) <= size and value.strip() and '\0' not in value
                for value in values))


def validate_action(value):
    if not isinstance(value, dict) or set(value) != set(action('')):
        raise Reject('invalid_action')
    kind, query, refs = value['action'], value['query'], value['refs']
    if (kind not in ('list', 'search', 'read', 'finish') or not isinstance(query, str) or
            not _bounded_strings(refs, MAX_BATCH, 64) or len(set(refs)) != len(refs)):
        raise Reject('invalid_action')
    if (bool(refs) != (kind == 'read') or bool(query) != (kind == 'search') or
            (query and (not query.strip() or len(query) > 120 or '\0' in query)) or
            (kind != 'finish' and (value['claims'] != [] or value['open_questions'] != []))):
        raise Reject('invalid_action')
    return kind


def validate_findings(value, shown):
    claims, questions = value['claims'], value['open_questions']
    if not isinstance(claims, list) or len(claims) > MAX_READ or not _bounded_strings(questions, 12, 350):
        raise Reject('invalid_action')
    if shown and not claims:
        raise Reject('invalid_citation')
    for claim in claims:
        if (not isinstance(claim, dict) or set(claim) != {'statement', 'citations'} or
                not _bounded_strings([claim['statement']], 1, 650) or not isinstance(claim['citations'], list) or
                not 1 <= len(claim['citations']) <= 8):
            raise Reject('invalid_citation')
        for citation in claim['citations']:
            if not isinstance(citation, dict) or set(citation) != {'ref', 'quote'}:
                raise Reject('invalid_citation')
            ref, excerpt = citation['ref'], citation['quote']
            if (not isinstance(ref, str) or ref not in shown or
                    not _bounded_strings([excerpt], 1, 350) or
                    (len(excerpt) < 4 and excerpt != shown[ref]) or excerpt not in shown[ref]):
                raise Reject('invalid_citation')
    return claims, questions


def research(library, request, parent, model, deadline):
    read = {}
    known = {}
    listed = set()
    trace, limits = [], set()
    inventory = None
    inventory_refs = set()
    parent_versions = (parent or {}).get('read', [])
    for version in parent_versions:
        record = library.read(version, deadline)
        read[record['ref']] = record
        known[record['ref']] = identity(record)
    candidates = []
    for turns_left in range(MAX_TURNS, 0, -1):
        if deadline - time.monotonic() < 2:
            raise Reject('action_budget')
        shown = {ref: row['text'][:1500] for ref, row in read.items()}
        context = {
            'question': request['query'], 'follow_up': bool(parent), 'turns_left': turns_left,
            'prior': {key: parent[key] for key in ('query', 'claims', 'open_questions')} if parent else None,
            'available_actions': ['finish'] if turns_left == 1 else ['list', 'search', 'read', 'finish'],
            'candidates': candidates,
            'activity': [{key:entry[key] for key in ('action', 'count') if key in entry} for entry in trace[-4:]],
            'read': [dict(row, text=shown[ref], excerpted=len(row['text']) > len(shown[ref]))
                     for ref, row in read.items()],
        }
        decision = model(context, min(60, int(deadline - time.monotonic())))
        kind = validate_action(decision)
        if kind not in context['available_actions']:
            raise Reject('action_budget')
        entry = {'action': kind}
        trace.append(entry)
        if kind == 'finish':
            if not read:
                raise Reject('no_match')
            claims, questions = validate_findings(decision, shown)
            # 提交前重新读回每个已读版本，捕捉模型调用期间的变化。
            for record in read.values():
                library.read(record, deadline)
            unread = known.keys() - read.keys()
            if inventory is not None:
                inventory['unread_passages'] = len(inventory_refs - read.keys())
            return dict(read=list(read.values()), claims=claims, open_questions=questions,
                        actions=trace, limits=sorted(limits), coverage={
                            'read_passages':len(read), 'read_materials':len({row['path'] for row in read.values()}),
                            'discovered_passages':len(known.keys() | read.keys()), 'known_unread_passages':len(unread),
                            'inventory':inventory,
                        })
        if kind == 'read':
            refs = decision['refs']
            if not set(refs) <= known.keys():
                raise Reject('invalid_action')
            if len(read.keys() | set(refs)) > MAX_READ:
                raise Reject('read_budget')
            for ref in refs:
                read[ref] = library.read(known[ref], deadline)
            entry['references'] = [identity(read[ref]) for ref in refs]
            candidates = [row for row in candidates if row['ref'] not in read]
            continue
        if kind == 'list':
            rows, skipped, bounded = library.rows()
            inventory_refs = {row['ref'] for row in rows}
            inventory = {'listed_materials':len({row['path'] for row in rows}),
                         'listed_passages':len(rows), 'bounded':bounded, 'skipped':skipped}
            candidates = [row for row in rows if row['ref'] not in listed and row['ref'] not in read][:LIST_SIZE]
            listed.update(row['ref'] for row in candidates)
            if len(rows) > len(listed | read.keys()):
                limits.add('列材料有分页，结果只覆盖本轮实际读到的固定版本。')
        else:
            entry['query'] = decision['query']
            rows, skipped, bounded = library.search(decision['query'], deadline)
            # 保留已读的相关命中，追问可以按新关键词重新定位原文。
            candidates = rows[:LIST_SIZE]
            if len(rows) > LIST_SIZE:
                limits.add('检索命中超过单轮候选上限。')
        if skipped:
            limits.add('资料库有文件未能读取。')
        if bounded:
            limits.add('本轮触及检索或材料清单扫描上限。')
        for row in candidates:
            previous = known.get(row['ref'])
            if previous and previous['revision'] != row['revision']:
                raise Reject('revision_changed')
            known[row['ref']] = row
        entry['count'] = len(candidates)
    raise Reject('action_budget')


def _board(result, source_path):
    nodes, edges = [], []

    def node(value, x, y, color=None):
        node_id = sha(f'node:{len(nodes)}')[:16]
        item = dict(id=node_id, x=x, y=y, width=460, height=220, **value)
        if color:
            item['color'] = color
        nodes.append(item)
        return node_id

    def edge(origin, target):
        edges.append(dict(id=sha(f'edge:{len(edges)}')[:16], fromNode=origin, toNode=target,
                          fromSide='right', toSide='left'))

    coverage = result['coverage']
    summary = f'实际核对 {coverage["read_materials"]} 份材料 · {coverage["read_passages"]} 个段落'
    inventory = coverage['inventory']
    if inventory:
        summary += (f'\n\n材料清单列出 {inventory["listed_materials"]} 份、{inventory["listed_passages"]} 段。'
                    f'清单未读 {inventory["unread_passages"]} 段，读取失败 {inventory["skipped"]} 份。')
    else:
        summary += f'\n\n检索选取的候选中另有 {coverage["known_unread_passages"]} 段未读。'
    full_inventory = (inventory and not inventory['bounded'] and not inventory['skipped']
                      and inventory['unread_passages'] == 0)
    if not full_inventory:
        summary += '\n\n覆盖状态：选读。'
    center = node({'type':'text', 'text':'# 研究问题\n\n' + _literal(result['query']) +
                   f'\n\n{summary}\n\n结论适用于本轮实际读回的版本。'}, 0, 0, '4')
    documents = {}
    for i, row in enumerate(result['read']):
        card = {'type':'file', 'file':source_path.as_posix(), 'subpath':f'#出处 {i + 1}'}
        documents[row['ref']] = node(card, 1100, i * 280, '5')
    for i, claim in enumerate(result['claims']):
        text = '### 阅读结果\n\n' + _literal(claim['statement'])
        text += ''.join('\n\n> ' + _literal(row['quote']) for row in claim['citations'])
        card = node({'type':'text', 'text':text}, 550, i * 280)
        edge(center, card)
        for ref in dict.fromkeys(row['ref'] for row in claim['citations']):
            edge(card, documents[ref])
    for i, question in enumerate([*result['open_questions'], *result['limits']]):
        card = node({'type':'text', 'text':'### 待核对\n\n' + _literal(question)}, 0, 280 * (i + 1), '2')
        edge(center, card)
    return {'nodes': nodes, 'edges': edges}


def _source_note(result):
    lines = ['# 研究引用原文', '以下文字保存了本轮实际读回的固定版本。']
    for index, row in enumerate(result['read'], 1):
        lines += [
            f'## 出处 {index}',
            _literal(row['heading'] or row['path']),
            f'{_literal(row["path"])} · 第 {row["line"]} 行',
            f'定位 `{row["ref"]}` · 版本 `{row["revision"]}`',
            '\n'.join('    ' + line for line in row['text'].splitlines()),
        ]
    return '\n\n'.join(lines) + '\n'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _real_directory(path):
    if path.is_symlink() or not path.is_dir():
        raise Reject('invalid_configuration')


@contextmanager
def _lock(folder):
    path = folder / '.research.lock'
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise Reject('worker_busy') from None
    finally:
        os.close(fd)


def _history_key(output):
    key_path = output / '.afr-research-history.key'
    if not key_path.exists():
        with tempfile.TemporaryDirectory(prefix='.research-key-', dir=output) as staging:
            candidate = Path(staging) / 'key'
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(candidate, key_path, follow_symlinks=False)
            except FileExistsError:
                pass
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise Reject('invalid_research_history')
        key = stream.read(33)
    if len(key) != 32:
        raise Reject('invalid_research_history')
    return key


def _seal(row, key):
    return hmac.digest(key, _json(row).encode(), 'sha256').hex()


def _previous(folder, key):
    results = {}
    for entry in folder.iterdir():
        if REQUEST.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
            path = entry / RECORD
            if path.is_file() and not path.is_symlink():
                row = json.loads(path.read_bytes())
                seal = row.pop('history_hmac', '')
                if (row['ticket'] != folder.name or row['request_id'] != entry.name or
                        not isinstance(seal, str) or not HEX.fullmatch(seal) or
                        not hmac.compare_digest(seal, _seal(row, key))):
                    raise Reject('invalid_research_history')
                if not (entry / CANVAS).is_file() or not (entry / SOURCES).is_file():
                    raise Reject('invalid_research_history')
                results[entry.name] = row
    return results


def _preserve_failure(directory, request_id, status, mode, model):
    local = {'version':1, 'status':status, 'request_id':request_id, 'mode':mode,
             'model_calls':model.calls if model else [], 'failed_at':int(time.time())}
    with tempfile.TemporaryDirectory(prefix='.research-failure-', dir=directory) as staging:
        record = Path(staging) / 'record.json'
        record.write_text(_json(local) + '\n', encoding='utf-8')
        record.replace(directory / ('.failed-' + request_id + '.json'))


def execute(library, output, delivery_vault, request_id, request, model_factory, deadline, *, mode=None):
    directory = output / request['ticket']
    directory.mkdir(exist_ok=True, mode=0o700)
    _real_directory(directory)
    digest = sha(_json(request))
    with _lock(directory):
        key = _history_key(output)
        previous = _previous(directory, key)
        if request_id in previous:
            saved = previous[request_id]
            if digest != saved['request_digest'] or (mode is not None and mode != saved['mode']):
                raise Reject('request_conflict')
            return len(saved['read'])
        parent = previous.get(request['parent'])
        if request['parent'] and not parent:
            raise Reject('invalid_parent')
        if previous and (not parent or parent['sequence'] != max(row['sequence'] for row in previous.values())):
            raise Reject('stale_parent')
        model = None
        try:
            model = model_factory(request)
            if parent and (parent['mode'] == 'synthetic-codex') != (model.name == 'synthetic-codex'):
                raise Reject('invalid_research_history')
            result = research(library, request, parent, model, deadline)
        except Reject as error:
            _preserve_failure(directory, request_id, str(error), model.name if model else mode, model)
            raise
        result.update(ticket=request['ticket'], request_id=request_id, request_digest=digest,
                      query=request['query'], parent=request['parent'], mode=model.name, model_calls=model.calls,
                      sequence=parent['sequence'] + 1 if parent else 1, version=1)
        result['limits'].extend([
            '材料日期需要可核对的入库记录。本轮未按时间筛出近期的全部材料。',
            '出处摘录已逐字核对，观点与出处是否相符仍可在白板查看。',
        ])
        if model.name == 'local-extract':
            result['limits'].append('本轮以本机关键词匹配和原文摘录整理，未调用模型综合。')
        final = directory / request_id
        source_target = final / SOURCES
        relative = source_target.relative_to(delivery_vault)
        with tempfile.TemporaryDirectory(prefix='.research-', dir=directory) as staging:
            sealed = Path(staging) / 'sealed'
            sealed.mkdir(mode=0o700)
            (sealed / RECORD).write_text(_json(dict(result, history_hmac=_seal(result, key))) + '\n',
                                         encoding='utf-8')
            (sealed / SOURCES).write_text(_source_note(result), encoding='utf-8')
            (sealed / CANVAS).write_text(_json(_board(result, relative)) + '\n', encoding='utf-8')
            sealed.rename(final)
        return len(result['read'])


def _argparser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--init-synthetic', type=Path)
    parser.add_argument('--afr-root', type=Path)
    parser.add_argument('--afr-python', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--requests', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--delivery-vault', type=Path,
                        help='Obsidian 白板所在知识库的根目录。默认使用研究库，输出在库外时默认使用输出目录。')
    parser.add_argument('--timeout', type=int, default=360)
    model = parser.add_mutually_exclusive_group()
    model.add_argument('--synthetic-codex', type=Path)
    parser.add_argument('--allow-synthetic-query-consent', action='store_true')
    model.add_argument('--local-model-command', type=Path)
    return parser


def main():
    args = _argparser().parse_args()
    if args.init_synthetic:
        init_synthetic(args.init_synthetic)
        return
    receipt = {'version': 1, 'status': 'ok', 'verified_count': 0}
    try:
        request_id = decode_event(json.loads(Path(os.environ['AGENT_AUTOLOOP_CONTEXT']).read_bytes()))
        receipt['request_id'] = request_id
        paths = (args.afr_root, args.vault, args.requests, args.output)
        executable = args.synthetic_codex or args.local_model_command
        if (not all(path and path.is_absolute() for path in paths) or not 15 <= args.timeout <= 720 or
                (executable and (not executable.is_absolute() or not executable.is_file() or
                                 not os.access(executable, os.X_OK)))):
            raise Reject('invalid_configuration')
        for folder in (args.afr_root, args.vault, args.requests):
            _real_directory(folder)
        request = decode_request(private_request(args.requests, request_id), request_id)
        vault = args.vault.resolve()
        output = args.output.resolve()
        requests = args.requests.resolve()
        if (args.output.is_symlink() or output == vault or requests.is_relative_to(output) or
                output.is_relative_to(requests)):
            raise Reject('invalid_configuration')
        if args.synthetic_codex:
            check_synthetic(vault, output, request['query'], request['parent'], request.get('consent'),
                            args.allow_synthetic_query_consent)
        delivery_vault = args.delivery_vault or (vault if output.is_relative_to(vault) else output)
        if not delivery_vault.is_absolute() or not output.is_relative_to(delivery_vault.resolve()):
            raise Reject('invalid_configuration')
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        library = AfrResearchLibrary(args.afr_root, vault, output, bool(args.synthetic_codex))

        def model_factory(value):
            if args.synthetic_codex:
                return SyntheticCodex(args.synthetic_codex, value['query'])
            if args.local_model_command:
                return OfflineCommand(args.local_model_command)
            return LocalExtract(value['query'])

        mode = (SyntheticCodex.name if args.synthetic_codex else
                OfflineCommand.name if args.local_model_command else LocalExtract.name)
        receipt['verified_count'] = execute(library, output, delivery_vault.resolve(), request_id, request,
                                            model_factory, time.monotonic() + args.timeout, mode=mode)
    except Reject as error:
        receipt['status'] = str(error) if str(error) in TERMINAL_ERRORS else 'invalid_local_input'
    except subprocess.TimeoutExpired:
        receipt['status'] = 'model_failed'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, RecursionError):
        receipt['status'] = 'invalid_local_input'
    print(json.dumps(receipt, ensure_ascii=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
