"""内网灰度连接的双向请求和响应证明，运行时密钥留在本机进程。"""
import hashlib
import hmac
import re
import secrets
import time


def _mac(key, *parts):
    return hmac.digest(key.encode("ascii"), "\n".join(parts).encode("utf-8"), "sha256").hex()


def client_ticket(key, method, route):
    nonce = f"{int(time.time()):08x}" + secrets.token_hex(12)
    return nonce + "." + _mac(key, "request", nonce, method, route)


def request_nonce(key, method, route, ticket):
    if not re.fullmatch(r"[a-f0-9]{64}", key) or not re.fullmatch(r"[a-f0-9]{32}\.[a-f0-9]{64}", ticket):
        return None
    nonce, signature = ticket.split(".")
    if abs(int(time.time()) - int(nonce[:8], 16)) > 20:
        return None
    if hmac.compare_digest(signature, _mac(key, "request", nonce, method, route)):
        return nonce
    return None


def response_proof(key, nonce, status, payload):
    return _mac(key, "response", nonce, str(status), hashlib.sha256(payload).hexdigest())
