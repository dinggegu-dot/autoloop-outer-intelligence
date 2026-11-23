"""Read the local AFR catalog and resolve selected fixed editions for the workbench."""

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import unicodedata
from urllib.parse import quote


PAGE_SIZE = 25
MAX_QUERY = 200
MAX_REQUEST = 8192
PUBLICATION_RECEIPT_MAX = 8192
TOPIC_TEXT_MAX = 2400
CITATION_TEXT_MAX = 600
CITATION_COUNT_MAX = 3
SHA = re.compile(r"[0-9a-f]{64}")
QUESTION_ID = re.compile(r"q-[0-9a-f]{32}")
QUESTION_REVISION = re.compile(r"[1-9][0-9]{0,17}")
ANCHOR = re.compile(r"\^(?:topic-|e)[1-9][0-9]*")


class BridgeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def bad_request():
    return BridgeError(400, "资料请求格式不正确。")


def unavailable():
    return BridgeError(503, "暂时无法读取 AFR 知识库。")


def changed():
    return BridgeError(409, "资料的版本或链接已变化，请重新打开资料目录。")


def missing():
    return BridgeError(404, "找不到所选资料。")


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def safe_file(vault, relative):
    from afr_agent.intake import inside

    if (not isinstance(relative, str) or "\\" in relative or "#" in relative
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise changed()
    return inside(vault, relative)


def article_folder(folder, identifier):
    from afr_agent.filing import DOMAIN_TERMS, PENDING_DOMAIN

    if not isinstance(folder, str):
        raise changed()
    parts = folder.split("/")
    suffix = " · " + identifier[:16]
    if (len(parts) != 3 or parts[0] != "资料库"
            or parts[1] not in {*DOMAIN_TERMS, PENDING_DOMAIN, "新收研报"}
            or not parts[2].endswith(suffix) or len(parts[2]) <= len(suffix)
            or parts[2].startswith(".")):
        raise changed()
    return parts[1], parts[2][:-len(suffix)]


def obsidian_link(vault, relative):
    filename, separator, anchor = relative.partition("#")
    path = safe_file(vault, filename)
    if not path.is_file() or (separator and not ANCHOR.fullmatch(anchor)):
        raise changed()
    destination = str(path) + separator + anchor
    return "obsidian://open?path=" + quote(destination, safe="")


@contextlib.contextmanager
def intake_database(vault):
    path = safe_file(vault, ".afr/intake.sqlite")
    if not path.exists():
        yield None
        return
    if not path.is_file():
        raise changed()
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as db:
        yield db


def publication_candidate(vault, identifier, folder, mapping):
    from afr_agent.semantic import publication_paths, receipt

    path = receipt(vault, identifier, "publication")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > PUBLICATION_RECEIPT_MAX:
                raise changed()
            raw = stream.read(PUBLICATION_RECEIPT_MAX + 1)
            after = os.fstat(stream.fileno())
            if len(raw) != info.st_size or info.st_mtime_ns != after.st_mtime_ns:
                raise changed()
            record = json.loads(raw)
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict) or set(record) - {"sha256", "state", "plan_sha256", "paths"}
            or record.get("sha256") != identifier or record.get("state") != "published"
            or not matches(SHA, record.get("plan_sha256"))):
        raise changed()
    paths = publication_paths(vault, identifier, folder, publication=record, mapping=mapping)
    if not all(safe_file(vault, target).is_file() for target in paths.values()):
        raise changed()
    return record["plan_sha256"]


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def in_query(query, *values):
    haystack = normalized(" ".join(values))
    return all(word in haystack for word in normalized(query).split())


def article_catalog(vault, query, offset):
    from afr_agent import locations
    from afr_agent.library import RequestError

    mapping = locations.load(vault)
    seen = 0
    items = []
    with intake_database(vault) as db:
        if db is None:
            return {"items": items, "next": None}
        rows = db.execute("""
            SELECT a.sha, a.folder FROM articles a
            JOIN receipts r ON r.sha = a.sha
            ORDER BY a.rowid DESC
        """)
        for identifier, folder in rows:
            try:
                if not matches(SHA, identifier):
                    continue
                domain, title = article_folder(folder, identifier)
                if not in_query(query, title, domain, identifier):
                    continue
                revision = publication_candidate(vault, identifier, folder, mapping)
            except (BridgeError, RequestError, OSError, ValueError, TypeError):
                continue
            if revision is None:
                continue
            if seen >= offset:
                if len(items) == PAGE_SIZE:
                    return {"items": items, "next": offset + PAGE_SIZE}
                items.append(dict(kind="article", id=identifier, revision=revision, title=title, domain=domain))
            seen += 1
    return {"items": items, "next": None}


def question_catalog(vault, query, offset):
    from afr_agent import questions

    rows = (
        dict(kind="question", id=record["id"], revision=str(record["revision"]),
             title=record["title"], sourceCount=len(record["links"]))
        for record in questions.catalog(vault)
        if in_query(query, record["title"], record["id"])
    )
    records = sorted(rows, key=lambda row: (normalized(row["title"]), row["id"]))
    next_offset = offset + PAGE_SIZE
    return {"items": records[offset:next_offset], "next": next_offset if next_offset < len(records) else None}


def article_selection(vault, identifier, revision):
    from afr_agent import locations

    with intake_database(vault) as db:
        row = None if db is None else db.execute(
            "SELECT folder FROM articles WHERE sha=?", (identifier,)).fetchone()
    if row is None:
        raise missing()
    domain, title = article_folder(row[0], identifier)
    mapping = locations.load(vault)
    if publication_candidate(vault, identifier, row[0], mapping) != revision:
        raise changed()
    return row[0], domain, title, mapping


def article_detail(vault, identifier, revision):
    from afr_agent import semantic
    from afr_agent.presentation import evidence_for

    selected, domain, title, mapping = article_selection(vault, identifier, revision)
    folder, evidence = evidence_for(vault, identifier)
    plan = semantic.reviewed_plan(vault, identifier)
    if plan["plan_sha256"] != revision:
        raise changed()
    actual_folder = folder.relative_to(vault).as_posix()
    if actual_folder != selected:
        raise changed()
    if evidence["folder"] == actual_folder:
        title = evidence["title"]
    paths = semantic.publication_paths(vault, identifier, evidence["folder"], mapping=mapping)
    links = {key: obsidian_link(vault, paths[key]) for key in ("canvas", "reading")}
    links["original"] = obsidian_link(vault, f"{actual_folder}/固定原文.md")
    topics = [dict(title=group["title"], index=index)
              for index, group in enumerate(plan["groups"], start=1)]
    return dict(kind="article", id=identifier, revision=revision, title=title, domain=domain,
                links=links, topics=topics, sources=[])


def topic_detail(vault, identifier, revision, index):
    from afr_agent import questions
    from afr_agent.library import RequestError
    from afr_agent.presentation import ORIGINAL_ARGUMENT, evidence_for

    selected, _, _, mapping = article_selection(vault, identifier, revision)
    try:
        # AFR checks the signed plan, the reading block and its Canvas link.
        topic = questions.read_topic(vault, identifier, index)
    except RequestError as exc:
        if str(exc) == "not_found":
            raise missing() from None
        raise
    folder, evidence = evidence_for(vault, identifier)
    if (folder.relative_to(vault).as_posix() != selected
            or publication_candidate(vault, identifier, selected, mapping) != revision):
        raise changed()

    # Prefer the first substantive paragraphs, keeping headings and other source units as fallback.
    citations = topic["citations"]
    preferred = sorted(citations, key=lambda entry: evidence["parts"][entry["number"] - 1]["kind"] != "paragraph")
    previews = [dict(number=entry["number"], text=entry["text"][:CITATION_TEXT_MAX],
                     link=obsidian_link(vault, entry["path"]))
                for entry in preferred[:CITATION_COUNT_MAX]]
    text = topic["text"]
    if text == ORIGINAL_ARGUMENT:
        # 正式原文保全模式的论述来自来源段落，校读标记本身不是正文。
        text = "\n\n".join(entry["text"][:TOPIC_TEXT_MAX] for entry in citations[:CITATION_COUNT_MAX])
    return dict(title=topic["title"], index=index, revision=topic["revision"],
                text=text[:TOPIC_TEXT_MAX], link=obsidian_link(vault, topic["path"]),
                citations=previews, totalCitations=len(citations))


def question_detail(vault, identifier, revision):
    from afr_agent import questions
    from afr_agent.library import RequestError

    try:
        current = questions.read(vault, identifier)
    except RequestError as exc:
        if str(exc) == "not_found":
            raise missing() from None
        raise
    try:
        record = current if current["revision"] == int(revision) else questions.read(vault, identifier, int(revision))
    except RequestError as exc:
        if str(exc) == "not_found":
            raise changed() from None
        raise
    if (record["id"] != identifier or str(record["revision"]) != revision
            or current["id"] != identifier or current["path"] != f"{questions.ROOT}/{identifier}.md"):
        raise changed()
    question_link = obsidian_link(vault, current["path"])
    sources = []
    for entry in record["links"]:
        sha, index, relation = entry["sha256"], entry["topic_id"], entry["relation"]
        if not matches(SHA, sha) or type(index) is not int or index < 1 or relation not in questions.RELATIONS:
            raise changed()
        topic = questions.read_topic(vault, sha, index)
        if topic["revision"] != entry["topic_revision"]:
            raise changed()
        sources.append(dict(title=topic["title"], relation=relation, sha=sha, index=index,
                            link=obsidian_link(vault, topic["path"])))
    return dict(kind="question", id=identifier, revision=revision, title=record["title"],
                links={"question": question_link}, topics=[], sources=sources)


def execute(vault, action, request):
    if not isinstance(request, dict) or request.get("kind") not in ("article", "question"):
        raise bad_request()
    kind = request["kind"]
    if action == "library" and set(request) == {"kind", "q", "offset"}:
        query, offset = request["q"], request["offset"]
        if not isinstance(query, str) or len(query) > MAX_QUERY or type(offset) is not int or offset < 0:
            raise bad_request()
        catalog = article_catalog if kind == "article" else question_catalog
        return catalog(vault, query, offset)
    if action == "item" and set(request) == {"kind", "id", "revision"}:
        identifier, revision = request["id"], request["revision"]
        if kind == "article" and matches(SHA, identifier) and matches(SHA, revision):
            return {"item": article_detail(vault, identifier, revision)}
        if kind == "question" and matches(QUESTION_ID, identifier) and matches(QUESTION_REVISION, revision):
            return {"item": question_detail(vault, identifier, revision)}
    if action == "topic" and kind == "article" and set(request) == {"kind", "id", "revision", "index"}:
        identifier, revision, index = request["id"], request["revision"], request["index"]
        if matches(SHA, identifier) and matches(SHA, revision) and type(index) is int and 1 <= index <= 10000:
            return {"topic": topic_detail(vault, identifier, revision, index)}
    raise bad_request()


def canonical_directory(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
        raise unavailable()
    return path


def main():
    try:
        if len(sys.argv) != 4:
            raise bad_request()
        runtime = canonical_directory(sys.argv[1])
        vault = canonical_directory(sys.argv[2])
        sys.path.insert(0, str(runtime))
        from afr_agent.library import RequestError

        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise bad_request()
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            raise bad_request() from None
        try:
            response = execute(vault, sys.argv[3], request)
        except (RequestError, FileNotFoundError, KeyError, ValueError, TypeError) as exc:
            if sys.argv[3] in {"item", "topic"}:
                raise changed() from exc
            raise unavailable() from exc
    except BridgeError as exc:
        response = {"error": str(exc), "status": exc.status}
    except (ImportError, OSError, RuntimeError, sqlite3.Error):
        error = unavailable()
        response = {"error": str(error), "status": error.status}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
