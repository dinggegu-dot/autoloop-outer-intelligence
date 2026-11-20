"""Autoloop named-command adapter for the local AFR v1 CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

EVENT = 'afr.lookup.requested'
SOURCE = 'afr-local'
HEX = re.compile(r'[a-f0-9]{64}')
REQUEST = re.compile(r'[a-zA-Z0-9_-]{1,100}')
LIMIT = 5
MAX_RESPONSE = 2 * 1024 * 1024


class Reject(ValueError):
    pass


def private_request(directory, name):
    """Read a bounded request without following a caller-controlled file link."""
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise Reject('invalid_request')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(4097)
            if len(data) > 4096:
                raise Reject('invalid_request')
            return json.loads(data)
        finally:
            os.close(fd)
    finally:
        os.close(root)


def run_afr(python, root, vault, operation, options, deadline):
    argv = [str(python), '-B', '-E', '-m', 'afr_agent', operation, '--vault', str(vault), *options]
    with subprocess.Popen(argv, cwd=root, env={'PATH': os.defpath}, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process, selectors.DefaultSelector() as ready:
        try:
            ready.register(process.stdout, selectors.EVENT_READ)
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise Reject('afr_timeout')
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise Reject('afr_response_limit')
            code = process.wait(timeout=max(0, deadline-time.monotonic()))
        except Reject:
            process.kill()
            raise
        except subprocess.TimeoutExpired:
            process.kill()
            raise Reject('afr_timeout') from None
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise Reject('invalid_afr_response') from None
    if type(result) is not dict or type(result.get('version')) is not int or result['version'] != 1:
        raise Reject('invalid_afr_response')
    if code == 2 and result.get('ok') is False and result.get('error') in (
            'revision_changed', 'not_found', 'invalid_request', 'invalid_vault', 'io_error'):
        raise Reject(result['error'])
    if code != 0 or result.get('ok') is not True:
        raise Reject('invalid_afr_response')
    return result


def identity(record):
    if not isinstance(record, dict):
        raise Reject('invalid_afr_response')
    keys = {key:record.get(key) for key in ('ref','revision')}
    if not all(isinstance(value,str) and HEX.fullmatch(value) for value in keys.values()):
        raise Reject('invalid_afr_response')
    return keys


def verified_read(invoke, reference):
    result = invoke('read', ['--ref',reference['ref'],'--revision',reference['revision']])
    text = result.get('text')
    if (identity(result) != reference or not isinstance(text,str)
            or hashlib.sha256(text.encode('utf-8')).hexdigest() != reference['revision']):
        raise Reject('read_verification_failed')
    return reference


def lookup(request, invoke):
    if not isinstance(request, dict):
        raise Reject('invalid_request')
    if set(request)=={'ref','revision'}:
        result = verified_read(invoke, identity(request))
        return 'ok', [result]
    if set(request) != {'query'} or not isinstance(request['query'],str):
        raise Reject('invalid_request')
    query = request['query']
    if not query.strip() or len(query)>300 or '\0' in query:
        raise Reject('invalid_request')
    hits = invoke('search', ['--query='+query,'--limit',str(LIMIT)]).get('hits')
    if not isinstance(hits,list) or len(hits)>LIMIT:
        raise Reject('invalid_afr_response')
    identifiers = [identity(hit) for hit in hits]
    if len({hit['ref'] for hit in identifiers}) != len(identifiers):
        raise Reject('invalid_afr_response')
    return ('ok',[verified_read(invoke,hit) for hit in identifiers]) if identifiers else ('no_match',[])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--afr-root', required=True, type=Path)
    parser.add_argument('--afr-python', required=True, type=Path)
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--requests', required=True, type=Path)
    args = parser.parse_args()
    ids = {}
    try:
        context = json.loads(Path(os.environ['AGENT_AUTOLOOP_CONTEXT']).read_bytes())
        events = context['events']
        if (not isinstance(events,list) or len(events)!=1 or events[0].get('type')!=EVENT
                or events[0].get('source')!=SOURCE):
            raise Reject('invalid_event')
        event = events[0]
        request_id = event.get('subject')
        if (not isinstance(request_id,str) or not REQUEST.fullmatch(request_id)
                or event.get('detail') or event.get('summary') or event.get('link')):
            raise Reject('invalid_event')
        ids['request_id'] = request_id
        paths = (args.afr_root,args.afr_python,args.vault,args.requests)
        if not all(path.is_absolute() for path in paths) or not (args.afr_root/'afr_agent/__main__.py').is_file():
            raise Reject('invalid_configuration')
        deadline = time.monotonic()+20
        def invoke(operation, options):
            return run_afr(args.afr_python,args.afr_root,args.vault,operation,options,deadline)
        status, refs = lookup(private_request(args.requests,request_id),invoke)
    except Reject as error:
        status, refs = str(error), []
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        status, refs = 'invalid_local_input', []
    # 必须白名单投影，不把异常、路径、文件名、查询、段落标题或正文交给中台。
    print(json.dumps(dict(version=1,status=status,verified_count=len(refs),references=refs,**ids),
                     ensure_ascii=True,separators=(',',':')))


if __name__ == '__main__':
    main()
