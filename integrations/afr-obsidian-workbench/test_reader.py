"""Exercise the isolated reader against the real AFR runtime and a synthetic vault."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


READER = Path(__file__).with_name("afr_handoff_reader.py").resolve()
RUNTIME_VALUE = os.environ.get("AFR_TEST_RUNTIME")


@unittest.skipUnless(RUNTIME_VALUE, "Set AFR_TEST_RUNTIME to the AFR repository or release root.")
class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = Path(RUNTIME_VALUE).resolve(strict=True)
        sys.path.insert(0, str(cls.runtime))
        cls.addClassCleanup(sys.path.remove, str(cls.runtime))

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.vault = self.base / "知识 库"
        self.inbox = self.base / "研报"
        self.approvals = self.base / "approvals.json"
        self.vault.mkdir()
        self.inbox.mkdir()

    def article(self, title="城市治理的合成研究", *, publish=True, content=None, argument=None):
        from afr_agent import intake, semantic
        from afr_agent.presentation import evidence_for

        note = self.inbox / f"{title}.md"
        content = content or "不可出现在桥接响应中的合成正文"
        note.write_text(f"# {title}\n\n{content}", encoding="utf-8")
        receipt, = intake.scan(self.vault, self.inbox)["added"]
        identifier = receipt["sha256"]
        folder = self.vault / receipt["folder"]
        if not publish:
            return identifier, folder, None
        self.approvals.write_text(json.dumps(dict(destination="OpenAI Codex", sha256=[identifier])), encoding="utf-8")
        self.approvals.chmod(0o600)
        _, evidence = evidence_for(self.vault, identifier)
        proposal = dict(start=1, end=len(evidence["parts"]), title="固定话题", branch="合成",
                        summary="研究摘要", argument=argument or "桥接不可返回的合成论述。")
        with patch.object(semantic.codex, "run_structured",
                          side_effect=[{"groups": [proposal]}, {"passed": True, "issues": []}]):
            semantic.process(self.vault, identifier, self.approvals)
        revision = semantic.reviewed_plan(self.vault, identifier)["plan_sha256"]
        return identifier, folder, revision

    def request(self, action, **data):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(READER), str(self.runtime), str(self.vault), action],
            input=json.dumps(data, ensure_ascii=False), text=True, capture_output=True,
            check=True, timeout=10,
        )
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def catalogue(self, kind, q="", offset=0):
        return self.request("library", kind=kind, q=q, offset=offset)

    def detail(self, kind, identifier, revision):
        return self.request("item", kind=kind, id=identifier, revision=revision)

    def link_target(self, link):
        parts = urlsplit(link)
        self.assertEqual((parts.scheme, parts.netloc), ("obsidian", "open"))
        return parse_qs(parts.query)["path"][0]

    def files(self):
        return {file.relative_to(self.vault): file.read_bytes()
                for file in self.vault.rglob("*") if file.is_file()}

    def test_empty_library_does_not_create_state(self):
        self.assertEqual(self.catalogue("article"), {"items": [], "next": None})
        self.assertEqual(self.catalogue("question"), {"items": [], "next": None})
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_only_signed_metadata_and_fixed_links_are_returned(self):
        identifier, folder, revision = self.article()
        self.article("未签发的合成材料", publish=False)
        before = self.files()
        catalog = self.catalogue("article", "治理")
        self.assertEqual(catalog, {"items": [dict(kind="article", id=identifier, revision=revision,
                                                  title="城市治理的合成研究", domain="治理与地缘")], "next": None})
        self.assertEqual(self.catalogue("article", "合成正文")["items"], [])
        self.assertEqual(self.catalogue("article", offset=1)["items"], [])
        item = self.detail("article", identifier, revision)["item"]
        self.assertEqual(item["topics"], [{"title": "固定话题", "index": 1}])
        self.assertEqual(item["sources"], [])
        self.assertEqual({key: self.link_target(link) for key, link in item["links"].items()},
                         {"canvas": str(folder / "文章白板.canvas"), "reading": str(folder / "完整阅读.md"),
                          "original": str(folder / "固定原文.md")})
        self.assertNotIn("合成论述", json.dumps(item, ensure_ascii=False))
        self.assertEqual(self.files(), before)

    def test_catalog_does_not_open_original_but_item_refuses_changed_original(self):
        identifier, folder, revision = self.article()
        (folder / "原件.md").write_text("损坏的合成原件", encoding="utf-8")
        self.assertEqual(self.catalogue("article")["items"][0]["id"], identifier)
        self.assertEqual(self.detail("article", identifier, revision)["status"], 409)
        self.assertEqual(self.detail("article", identifier, "1" * 64)["status"], 409)
        self.assertEqual(self.detail("article", "2" * 64, revision)["status"], 404)
        self.assertEqual(self.detail("article", "../../任意文件", revision)["status"], 400)

    def test_topic_preview_reads_only_selected_signed_topic_and_exact_bounded_passages(self):
        from afr_agent import questions

        paragraphs = [f"合成段落 {index}：" + "证据" * 400 for index in range(1, 5)]
        argument = "经校读的合成论述。" * 300
        content = "\n\n## 小节\n\n".join(paragraphs)
        identifier, folder, revision = self.article(content=content, argument=argument)
        before = self.files()
        identity = dict(kind="article", id=identifier, revision=revision, index=1)
        topic = self.request("topic", **identity)["topic"]
        self.assertEqual(topic["revision"], questions.read_topic(self.vault, identifier, 1)["revision"])
        self.assertEqual(topic["title"], "固定话题")
        self.assertEqual(topic["index"], 1)
        self.assertEqual(topic["text"], argument[:2400])
        self.assertEqual(topic["totalCitations"], 8)
        self.assertEqual(self.link_target(topic["link"]), str(folder / "完整阅读.md") + "#^topic-1")
        self.assertEqual([(row["number"], row["text"], self.link_target(row["link"])) for row in topic["citations"]],
                         [(index, paragraph[:600], str(folder / "固定原文.md") + f"#^e{index}")
                          for index, paragraph in zip((2, 4, 6), paragraphs)])
        self.assertEqual(self.files(), before)
        self.assertEqual(self.request("topic", **(identity | {"index": True}))["status"], 400)
        self.assertEqual(self.request("topic", **(identity | {"index": 2}))["status"], 404)
        self.assertEqual(self.request("topic", **(identity | {"revision": "1" * 64}))["status"], 409)
        reading = folder / "完整阅读.md"
        reading.write_text(reading.read_text(encoding="utf-8") + "\n^topic-1\n", encoding="utf-8")
        self.assertEqual(self.request("topic", **identity)["status"], 409)

    def test_topic_preview_uses_fixed_passages_for_original_preservation(self):
        from afr_agent.presentation import ORIGINAL_ARGUMENT

        content = "以固定原文阅读这一组论述。"
        identifier, _, revision = self.article(content=content, argument=ORIGINAL_ARGUMENT)
        topic = self.request("topic", kind="article", id=identifier, revision=revision, index=1)["topic"]
        self.assertIn(content, topic["text"])
        self.assertNotIn(ORIGINAL_ARGUMENT, topic["text"])

    def test_reviewed_aliases_are_resolved_and_symlinks_rejected(self):
        from afr_agent import semantic

        identifier, folder, revision = self.article()
        receipt = semantic.receipt(self.vault, identifier, "publication")
        value = json.loads(receipt.read_bytes())
        reading = folder / "完整阅读（本轮校读）.md"
        canvas = folder / "文章白板（本轮校读）.canvas"
        (folder / "完整阅读.md").rename(reading)
        (folder / "文章白板.canvas").rename(canvas)
        value["paths"] = {"reading": reading.relative_to(self.vault).as_posix(),
                          "canvas": canvas.relative_to(self.vault).as_posix()}
        receipt.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        item = self.detail("article", identifier, revision)["item"]
        self.assertEqual(self.link_target(item["links"]["reading"]), str(reading))
        self.assertEqual(self.link_target(item["links"]["canvas"]), str(canvas))
        canvas.unlink()
        elsewhere = self.base / "外部.canvas"
        elsewhere.write_text("{}", encoding="utf-8")
        canvas.symlink_to(elsewhere)
        self.assertEqual(self.catalogue("article")["items"], [])
        response = self.detail("article", identifier, revision)
        self.assertEqual(response["status"], 409)
        self.assertNotIn(str(self.base), response["error"])

    def test_relocated_report_preserves_selection_identity(self):
        from afr_agent import intake, locations

        with patch.object(intake, "classify_article", return_value=("旧投放名", "新收研报")):
            identifier, original, revision = self.article()
        before, = self.catalogue("article")["items"]
        target = self.vault / "资料库" / "治理与地缘" / f"归档题名 · {identifier[:16]}"
        target.parent.mkdir()
        original.rename(target)
        old = original.relative_to(self.vault).as_posix()
        new = target.relative_to(self.vault).as_posix()
        mapping = {old: new}
        (self.vault / locations.MANIFEST).write_text(json.dumps(dict(version=1, folders=mapping)), encoding="utf-8")
        with sqlite3.connect(self.vault / ".afr" / "intake.sqlite") as db:
            db.execute("UPDATE articles SET folder=? WHERE sha=?", (new, identifier))
        fixed = target / "固定原文.md"
        fixed.write_text(locations.rewrite(fixed.read_text(encoding="utf-8"), mapping), encoding="utf-8")
        after, = self.catalogue("article", "归档题名")["items"]
        self.assertEqual((after["id"], after["revision"]), (before["id"], before["revision"]))
        item = self.detail("article", identifier, revision)["item"]
        self.assertEqual(item["title"], "归档题名")
        self.assertEqual(self.link_target(item["links"]["canvas"]), str(target / "文章白板.canvas"))

    def test_question_history_resolves_signed_sources_without_notes_or_body(self):
        from afr_agent import questions

        identifier, folder, _ = self.article()
        question, = questions.apply(self.vault, identifier, [{
            "new_question": "如何验证合成观点？", "topic_id": 1, "relation": "SUPPORTS",
            "reason": "不可出现在响应中的归类理由",
        }], confirm_new=True)["questions"]
        question_id = question["id"]
        questions.save_discussion(self.vault, question_id, question["revision"], "不可出现在响应中的讨论")
        before = self.files()
        hit, = self.catalogue("question", "观点")["items"]
        self.assertEqual(hit, dict(kind="question", id=question_id, revision="2", title=question["title"], sourceCount=1))
        item = self.detail("question", question_id, "1")["item"]
        self.assertEqual(item["revision"], "1")
        self.assertEqual(item["topics"], [])
        self.assertEqual(self.link_target(item["links"]["question"]), str(self.vault / question["path"]))
        source, = item["sources"]
        self.assertEqual({key: source[key] for key in ("title", "relation", "sha", "index")},
                         dict(title="固定话题", relation="SUPPORTS", sha=identifier, index=1))
        self.assertEqual(self.link_target(source["link"]), str(folder / "完整阅读.md") + "#^topic-1")
        self.assertNotIn("不可出现在", json.dumps(item, ensure_ascii=False))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.detail("question", question_id, "3")["status"], 409)
        reading = folder / "完整阅读.md"
        reading.write_text(reading.read_text(encoding="utf-8") + "\n^topic-1\n", encoding="utf-8")
        self.assertEqual(self.detail("question", question_id, "1")["status"], 409)

    def test_pagination_has_no_overlap_or_phantom_page(self):
        from afr_agent import questions

        identifier, _, _ = self.article()
        questions.apply(self.vault, identifier, [
            dict(new_question=f"合成问题 {index:02}", topic_id=1, relation="SUPPORTS", reason="固定材料")
            for index in range(26)
        ], confirm_new=True)
        first_page = self.catalogue("question")
        self.assertEqual(len(first_page["items"]), 25)
        self.assertEqual(first_page["next"], 25)
        last_page = self.catalogue("question", offset=first_page["next"])
        self.assertEqual(len(last_page["items"]), 1)
        self.assertIsNone(last_page["next"])
        self.assertNotIn(last_page["items"][0]["id"], {item["id"] for item in first_page["items"]})

    def test_bad_inputs_and_broken_storage_return_safe_errors(self):
        self.assertEqual(self.request("library", kind="question", q="", offset=True)["status"], 400)
        self.assertEqual(self.request("library", kind="article", q="x" * 201, offset=0)["status"], 400)
        self.assertEqual(self.request("publish", kind="article", q="", offset=0)["status"], 400)
        bad = self.vault / ".afr" / "intake.sqlite"
        bad.parent.mkdir()
        with sqlite3.connect(bad) as db:
            db.execute("CREATE TABLE wrong (data TEXT)")
        result = self.catalogue("article")
        self.assertEqual(result["status"], 503)
        self.assertNotIn(str(bad), result["error"])


if __name__ == "__main__":
    unittest.main()
