"""Explicitly configured local AFR queries. No ticket, model, or audit mutations."""
import hashlib
from pathlib import Path
import sys
from threading import BoundedSemaphore
import time

from workbench import Problem

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    from afr_command_agent import HEX, LIMIT, Reject, identity, run_afr
finally:
    sys.path.pop(0)


class AfrPanel:
    def __init__(self, root, python, vault):
        paths = (root,python,vault)
        if not all(path.is_absolute() for path in paths):
            raise ValueError('AFR 配置需要绝对路径')
        self.root, self.python, self.vault = root.resolve(strict=True), python, vault.resolve(strict=True)
        if (not (self.root/'afr_agent/__main__.py').is_file() or not self.python.is_file()
                or not self.vault.is_dir()):
            raise ValueError('AFR 程序、解释器或知识库无效')
        self.slot = BoundedSemaphore()

    def _invoke(self, operation, options):
        if not self.slot.acquire(blocking=False):
            raise Problem('另一个本机查询仍在处理，请稍后重试',503)
        try:
            return run_afr(self.python,self.root,self.vault,operation,options,time.monotonic()+20)
        except Reject as error:
            if str(error)=='revision_changed':
                raise Problem('原文版本已变化，请重新搜索后再读',409) from None
            if str(error)=='not_found':
                raise Problem('原文没有找到，请重新搜索',404) from None
            raise Problem('AFR 本机查询未完成，请核对程序和知识库',503) from None
        except (OSError, ValueError):
            raise Problem('AFR 本机查询暂不可用',503) from None
        finally:
            self.slot.release()

    @staticmethod
    def _metadata(record):
        try:
            references = identity(record)
        except Reject:
            raise Problem('AFR 返回无效定位',503) from None
        if not all(isinstance(record.get(key),str) for key in ('path','heading')):
            raise Problem('AFR 返回无效定位',503)
        return {**references,'path':record['path'][:500],'heading':record['heading'][:200]}

    def dispatch(self, action, body):
        if action=='capability' and not body:
            return {'enabled':True}
        if action=='search':
            query = body.get('query')
            if set(body)!={'query'} or not isinstance(query,str) or not query.strip() or len(query)>300 or '\0' in query:
                raise Problem('请输入 1 至 300 字的检索词')
            hits = self._invoke('search',['--query='+query.strip(),'--limit',str(LIMIT)]).get('hits')
            if not isinstance(hits,list) or len(hits)>LIMIT:
                raise Problem('AFR 返回无效搜索结果',503)
            return {'version':1,'hits':[self._metadata(hit) for hit in hits]}
        if action=='read':
            if (set(body)!={'ref','revision'} or not all(isinstance(value,str) and HEX.fullmatch(value)
                                                        for value in body.values())):
                raise Problem('请从搜索结果选择有效的原文版本')
            result = self._invoke('read',['--ref',body['ref'],'--revision',body['revision']])
            metadata = self._metadata(result)
            text = result.get('text')
            if (any(metadata[key]!=body[key] for key in ('ref','revision')) or not isinstance(text,str)
                    or hashlib.sha256(text.encode('utf-8')).hexdigest()!=body['revision']):
                raise Problem('原文与检索定位不一致，请重新搜索',409)
            return {'version':1,**metadata,'text':text}
        raise Problem('接口不存在',404)
