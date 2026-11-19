"""准备合成验收工单或独立核对资料报告；本身不派发、不审批、不运行生成代码。"""
import argparse
import csv
import json
import re
from pathlib import Path
import shutil
import uuid

from workbench import Store


SAMPLES=Path(__file__).resolve().parent/'验收样例'
CASES={
    '代码修复': ('修正 stats_tool.py 中空白行的统计错误，保留接口，不修改随附的测试；只需一步实施。',
                'python3 -B -m unittest -v test_stats_tool 通过；交付修好的模块和简短说明。'),
    '资料报告': ('读取 input.csv，按行业汇总条目数；只需一步，生成 UTF-8 report.md 和 totals.json。',
                'totals.json 是键为行业、值为整数的对象。report.md 含一张表头为「行业」「条目数」的 Markdown 两列表格，每个行业一行，表外列出三个来源。'),
}


def prepare_cases(root,cases):
    root=Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('请选择空目录；不写入已有任务库')
    store=Store(root)
    return {case:prepare_task(store,case) for case in cases}


def prepare_task(store,case):
    goal,acceptance=CASES[case]
    task,_=store.create({'request_id':str(uuid.uuid4()),'name':'隔离验收：'+case,'goal':goal,'acceptance':acceptance,
                         'budget_minutes':15,'execution_mode':'bounded',
                         'role_models':dict.fromkeys(('plan','execute','review'),'gpt-6-astra')})
    for source in (SAMPLES/case).iterdir():
        if source.is_file():shutil.copy2(source,store.workspace(task['id'])/source.name)
    return task['id']


def prepare(root,case):
    return prepare_cases(root,[case])[case]


def report_totals(report):
    """按表头和行读取数字，避免跨行业配对或前后矛盾的重复行。"""
    lines=report.splitlines()
    def cells(line):
        return [cell.strip() for cell in line.strip().strip('|').split('|')]
    headers=[i for i,line in enumerate(lines) if cells(line)==['行业','条目数']]
    if len(headers)!=1:
        raise ValueError('报告须含一张行业、条目数汇总表')
    start=headers[0]+1
    separator=cells(lines[start]) if start<len(lines) else []
    if len(separator)!=2 or not all(re.fullmatch(r':?-{3,}:?',cell) for cell in separator):
        raise ValueError('汇总表缺少 Markdown 表头分隔行')
    result={}
    for line in lines[start+1:]:
        if '|' not in line:break
        row=cells(line)
        if len(row)!=2 or row[0] in result or not re.fullmatch(r'[0-9]+',row[1]):
            raise ValueError('行业重复或汇总表行格式有误')
        result[row[0]]=int(row[1])
    return result


def read_output(directory,name):
    path=directory/name
    if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:
        raise ValueError('样例输出须为 1 MiB 以内的普通文件')
    return path.read_text(encoding='utf-8')


def check_report(directory):
    expected={}
    with (SAMPLES/'资料报告/input.csv').open(encoding='utf-8',newline='') as stream:
        rows=list(csv.DictReader(stream))
    for row in rows:
        category=row['行业']
        expected[category]=expected.get(category,0)+int(row['条目数'])
    data=json.loads(read_output(directory,'totals.json'))
    if data!=expected or not all(type(value) is int for value in data.values()):
        raise ValueError('行业或数值与原始固定输入不符')
    report=read_output(directory,'report.md')
    if not all(row['来源'] in report for row in rows):
        raise ValueError('报告缺少输入来源')
    if report_totals(report)!=expected:
        raise ValueError('报告逐行汇总与固定输入不符')
    return expected


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    create=sub.add_parser('prepare')
    create.add_argument('root',type=Path)
    create.add_argument('case',choices=[*CASES,'全部'])
    check=sub.add_parser('check-report')
    check.add_argument('output',type=Path)
    args=parser.parse_args()
    if args.mode=='check-report':print(check_report(args.output))
    elif args.case=='全部':print(json.dumps(prepare_cases(args.root,CASES),ensure_ascii=False))
    else:print(prepare(args.root,args.case))
