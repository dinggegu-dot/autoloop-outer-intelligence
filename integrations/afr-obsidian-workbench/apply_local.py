"""Generate a patch bundle against the installed Odyssey Workbench."""
from argparse import ArgumentParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ("server.py", "index.html", "research.js", "tasks.js")
ADDITIONS = ("afr_handoff.py", "afr_handoff_reader.py", "afr_handoff.js", "afr_handoff.css")
TICK, TEMPLATE = chr(96), "$" + "{"


def transform(code, filename, pairs):
    for old, new in pairs:
        if code.count(old) != 1:
            raise ValueError(f"工作台版本不兼容或已接入：{filename}")
        code = code.replace(old, new, 1)
    return code


def server_code(code):
    return transform(code, "server.py", (
        ("import research_library\n", "import research_library\nfrom afr_handoff import Handoff, HandoffError\n"),
        ("    def do_POST(self):", """    def log_request(self, code='-', size='-'):
        if self.path.startswith('/api/afr-handoff/'):
            return self.log_message('%s /api/afr-handoff/* %s', self.command, code)
        return super().log_request(code, size)

    def respond_afr_handoff(self, action, data, *, write=False):
        bridge = getattr(self.server, 'afr_handoff', None)
        if bridge is None:
            return self.json_response({'error': 'AFR 交接尚未连接，原知识库入口仍可使用'}, 503)
        try:
            return self.json_response(bridge.handle(action, data, write=write))
        except HandoffError as error:
            return self.json_response({'error': str(error)}, error.status)
        except (OSError, sqlite3.Error):
            return self.json_response({'error': '本机交接暂不可用，请刷新核对'}, 503)

    def do_POST(self):"""),
        ("            workbench = self.server.workbench\n",
         """            if path.startswith('/api/afr-handoff/'):
                return self.respond_afr_handoff(path.removeprefix('/api/afr-handoff/'), body, write=True)
            workbench = self.server.workbench
"""),
        ("        if route in ('/api/library/archives','/api/library/afr-materials')",
         """        if route.startswith('/api/afr-handoff/'):
            if not self.session():
                return self.json_response({'error': '会话已过期，请刷新'}, 403)
            try:
                values = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=6)
                if any(len(items) != 1 for items in values.values()):
                    raise ValueError()
                args = {key: items[0] for key, items in values.items()}
                if 'offset' in args:
                    args['offset'] = int(args['offset'])
            except ValueError:
                return self.json_response({'error': '查询参数无效'}, 400)
            return self.respond_afr_handoff(route.removeprefix('/api/afr-handoff/'), args)
        if route in ('/api/library/archives','/api/library/afr-materials')"""),
        ("for name in ('research.js','research.css',",
         "for name in ('afr_handoff.js','afr_handoff.css','research.js','research.css',"),
        ("    parser.add_argument('--afr-config',",
         """    parser.add_argument('--afr-handoff-config', type=Path, help='本机 AFR 交接的私有配置')
    parser.add_argument('--afr-config',"""),
        ("        server.task_actions = threading.Lock()", """        server.task_actions = threading.Lock()
        handoff_config = args.afr_handoff_config or ROOT / 'afr-handoff.local.json'
        if handoff_config.exists():
            try:
                server.afr_handoff = Handoff(handoff_config,server.workbench.store,
                                             research_library.archive,server.server_port)
            except (HandoffError, OSError, ValueError):
                print('AFR 交接配置未通过核对，工作台其它入口继续运行', flush=True)"""),
    ))


def research_code(code):
    footer = "在 Obsidian 打开白板 ↗</a>" + TICK + ":''}</footer>"
    title = TEMPLATE + "vault?'本机 Obsidian 知识库':esc(title())}"
    counts = "<p>近 " + TEMPLATE + "c.days} 天 · 日期在范围内 " + TEMPLATE + "c.dated_matches} 份 · 入库日期未知 " + TEMPLATE + "c.unknown_admission} 份</p>"
    known_counts = "<p>" + TEMPLATE + "(Number.isInteger(c.days)?'近 '+c.days+' 天 · ':'')}" + TEMPLATE + "Number.isInteger(c.dated_matches)?'范围内 '+c.dated_matches+' 份':'范围内数量未记录'} · " + TEMPLATE + "Number.isInteger(c.unknown_admission)?'入库日期未知 '+c.unknown_admission+' 份':'未知日期数量未记录'}</p>"
    caption = TEMPLATE + "vault?'在电脑上检索材料，按命中版本回读原文。':'研究工单读过的原文、固定版本与白板'}"
    return transform(code, "research.js", (
        (counts, known_counts),
        ("  const currentRound = () => {", """  const currentRound = () => {
    const request = param('afrRequest');
    if(request)return rounds.findIndex(entry=>entry.request===request);"""),
        ("for(const key of ['panel','mode','round','node',...Object.keys(closedReader)])",
         "for(const key of ['panel','mode','round','node','afrRequest','afrTicket',...Object.keys(closedReader)])"),
        ("    route({view:'tasks',task:tid,panel:'research',...closedReader,...values});",
         """    const changesRound=Object.hasOwn(values,'round');
    route({view:'tasks',task:tid,panel:'research',...closedReader,
      ...(changesRound?{afrRequest:null}:{}),...values});"""),
        ("['sources','boards','vault','intake'].includes", "['sources','boards','vault','intake','afr'].includes"),
        ("    const tab=shelf(), sources=tab==='sources', vault=tab==='vault';",
         """    const tab=shelf(), sources=tab==='sources', vault=tab==='vault';
    const bridgeTicket=param('afrTicket')||'';
    const mounted=byId('afr-handoff-library');
    if(tab==='afr'&&mounted?.dataset.ticket===bridgeTicket)return;"""),
        ("const body=tab==='intake'?", """const body=tab==='afr'
      ?'<div id="afr-handoff-library" data-ticket="'+esc(bridgeTicket)+'"></div>'
      :tab==='intake'?"""),
        (title, title.replace("vault?", "tab==='afr'?'AFR 成果':vault?", 1)),
        (caption, caption.replace("vault?", "tab==='afr'?'查看已签发的研报白板、完整阅读和研究问题。':vault?", 1)),
        ("['vault','本机 Obsidian'],['intake','研报整理进度']",
         "['vault','本机 Obsidian'],['afr','AFR 成果'],['intake','研报整理进度']"),
        ("    if(vault)nativeResults();", """    if(tab==='afr')window.afrHandoff.mountLibrary(byId('afr-handoff-library'),{ticket:bridgeTicket});
    else if(vault)nativeResults();"""),
        ("""    if(!has(tid)||tid!==id||!rounds.length)return emptyArchive(tid);
    const idx=currentRound(), r=rounds[idx], board=mode()==='board';""",
         """    const handoff=(request='',round='')=>'<div data-afr-ticket="'+esc(tid)+'" data-afr-request="'+esc(request)+'" data-afr-round="'+esc(round)+'"></div>';
    if(!has(tid)||tid!==id||!rounds.length)return emptyArchive(tid)+handoff();
    const idx=currentRound(), r=rounds[idx], board=mode()==='board';
    if(!r)return '<p class="notice-strip" role="status">Obsidian 链接绑定的研究轮次不可用，请从知识库重新查阅。</p>'+handoff();"""),
        (footer, footer + TEMPLATE + "handoff(r.request||'',idx+1)}"),
    ))


def tasks_code(code):
    return transform(code, "tasks.js", (
        (TEMPLATE + "parent?researchDesk.report(task.id):''}",
         TEMPLATE + """parent?researchDesk.report(task.id):'<div data-afr-ticket="'+html(task.id)+'"></div>'}"""),
        ("    if(panel==='research')content=section('research',researchDesk.report(task.id));",
         """    if(panel==='overview')content+='<div data-afr-ticket="'+html(task.id)+'"></div>';
    if(panel==='research')content=section('research',researchDesk.report(task.id));"""),
        ("'task','panel','mode','round','node','source','revision','native','ci','cite'",
         "'task','panel','mode','round','node','source','revision','native','ci','cite','afrRequest','afrTicket'"),
        ("    renderInto(node('#live-approvals'),",
         """    node('#live-tasks').querySelectorAll('[data-afr-ticket]').forEach(element=>{
      window.afrHandoff.mountTicket(element,{ticket:element.dataset.afrTicket,
        request:element.dataset.afrRequest,round:Number(element.dataset.afrRound)||null});
    });
    renderInto(node('#live-approvals'),"""),
    ))


def index_code(code):
    return transform(code, "index.html", (
        ("</head>", '<link rel="stylesheet" href="afr_handoff.css">\n</head>'),
        ('<script src="research.js"></script>',
         '<script src="afr_handoff.js"></script>\n<script src="research.js"></script>'),
    ))


def build(source, output):
    source = source.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise ValueError('输出目录必须尚未创建')
    bundle = {}
    for name, patch in zip(FILES, (server_code, index_code, research_code, tasks_code)):
        path = source / name
        if path.is_symlink():
            raise ValueError(f'工作台入口不能是符号链接：{name}')
        bundle[name] = patch(path.read_text(encoding='utf-8')).encode('utf-8')
    for name in ADDITIONS:
        bundle[name] = (HERE / name).read_bytes()
    output.mkdir(mode=0o700, parents=True)
    for name, content in bundle.items():
        (output / name).write_bytes(content)


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.source, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    print(args.output)
