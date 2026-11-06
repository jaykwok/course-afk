import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.discovery.contracts import DiscoveryManifest
from core.discovery.recursive import collect_discovery_entries, read_entry_manifest
from core.learning.zone import extract_topic_entry_urls
from core.queues.learning import read_learning_failures, read_learning_urls, record_learning_failure
from core.queues.exam import read_exam_urls
from core.tasks.repository import TaskRepository

ROOT = "https://cms.mylearning.cn/safe/topic/root/pc.html"
CHILD = "https://cms.mylearning.cn/safe/topic/child/pc.html"
CLASS = "https://kc.zhixueyun.com/#/train-new/class-detail/11111111-1111-4111-8111-111111111111"
SUBJECT = "https://kc.zhixueyun.com/#/study/subject/detail/22222222-2222-4222-8222-222222222222"
COURSE = "https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-4333-8333-333333333333"
EXAM = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/44444444-4444-4444-8444-444444444444"


class RecursiveDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.repo = TaskRepository(Path(tmp) / "state.sqlite")
        self.enterContext(patch("core.tasks.store.repository_for", return_value=self.repo))
        self.enterContext(patch("core.tasks.store.account_id", return_value="test"))
        self.enterContext(patch("core.tasks.identity.account_id", return_value="test"))
        self.enterContext(patch("core.tasks.relations.account_id", return_value="test"))
        self.pages = []

        async def new_page():
            self.assertTrue(all(p.close.await_count == 1 for p in self.pages))
            page = SimpleNamespace(close=AsyncMock())
            self.pages.append(page)
            return page

        self.context = SimpleNamespace(new_page=AsyncMock(side_effect=new_page))

    async def collect(self, manifests, **kwargs):
        async def read(page, context, url, **options):
            self.assertIs(context, self.context)
            result = manifests[url]
            if isinstance(result, BaseException):
                raise result
            return result

        with patch("core.discovery.recursive.read_entry_manifest", side_effect=read) as reader:
            result = await collect_discovery_entries([ROOT], context=self.context, **kwargs)
        return result, [call.args[2] for call in reader.await_args_list]

    async def test_nested_mixed_entries_deduplicate_shared_children_and_ignore_backlinks(self):
        result, reads = await self.collect({
            ROOT: DiscoveryManifest((CLASS, CHILD), True),
            CLASS: DiscoveryManifest((SUBJECT,), True),
            SUBJECT: DiscoveryManifest((COURSE, EXAM), True, "subject_api"),
            CHILD: DiscoveryManifest((SUBJECT, ROOT, COURSE), True),
        })
        self.assertTrue(result.complete)
        self.assertEqual(reads, [ROOT, CLASS, SUBJECT, CHILD])
        self.assertEqual(read_learning_urls(), [COURSE])
        self.assertEqual(read_exam_urls(), [EXAM])
        self.assertEqual(result.details["course_count"], 1)
        self.assertEqual(result.details["exam_count"], 1)
        self.assertFalse(read_learning_failures())
        self.assertTrue(all(p.close.await_count == 1 for p in self.pages))
        tasks = self.repo.export()["tasks"]
        root = next(t for t in tasks if t["url"] == ROOT and t["kind"] == "entry")
        self.assertEqual(root["status"], "WAITING_CHILDREN")

    async def test_partial_child_keeps_good_tasks_and_exact_cause_then_retry_resolves_discovery(self):
        manifests = {
            ROOT: DiscoveryManifest((CHILD, COURSE), True),
            CHILD: DiscoveryManifest((), False, message="分页 3 未返回", details={"page": 3}),
        }
        result, _ = await self.collect(manifests)
        self.assertFalse(result.complete)
        self.assertEqual(read_learning_urls(), [COURSE])
        failure = next(f for f in read_learning_failures() if f.url == CHILD)
        self.assertEqual(failure.reason_text, "分页 3 未返回")
        self.assertEqual(failure.detail["page"], 3)
        parent = next(f for f in read_learning_failures() if f.url == ROOT)
        self.assertEqual(parent.detail["child_issues"][0]["url"], CHILD)
        record_learning_failure(ROOT, reason="manual_review", reason_text="保留人工事项")
        manifests[CHILD] = DiscoveryManifest((COURSE,), True)
        result, _ = await self.collect(manifests)
        self.assertTrue(result.complete)
        self.assertEqual([(f.url, f.reason) for f in read_learning_failures()], [(ROOT, "manual_review")])

    async def test_limits_keep_unvisited_entry_and_prevent_false_completion(self):
        result, reads = await self.collect({ROOT: DiscoveryManifest((CHILD, COURSE), True)}, max_entries=1)
        self.assertFalse(result.complete)
        self.assertEqual(reads, [ROOT])
        self.assertEqual(read_learning_urls(), [COURSE])
        self.assertTrue(any(f.url == CHILD and "上限" in f.reason_text for f in read_learning_failures()))

    async def test_cancellation_keeps_all_discovered_siblings_and_closes_tab(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.collect({ROOT: DiscoveryManifest((CHILD, CLASS, COURSE), True), CHILD: asyncio.CancelledError()})
        self.assertTrue({ROOT, CHILD, CLASS} <= {f.url for f in read_learning_failures()})
        self.assertEqual(read_learning_urls(), [COURSE])
        self.assertTrue(all(p.close.await_count == 1 for p in self.pages))

    async def test_subject_reader_preserves_source_context_and_residual_sections(self):
        from core.discovery.subject_parse import SubjectExpandResult
        sourced = COURSE + "/6/1/22222222-2222-4222-8222-222222222222"
        with patch("core.discovery.subject_parse.expand_subject_from_page", new=AsyncMock(return_value=SubjectExpandResult(
            course_urls=[sourced], exam_urls=[EXAM], residual_subject_url=SUBJECT,
            url_section_count=1, section_total=3, complete=True,
        ))):
            result = await collect_discovery_entries([SUBJECT], context=self.context)
        self.assertTrue(result.complete)
        self.assertFalse(result.details["local_work_complete"])
        self.assertEqual(read_learning_urls(), [sourced, SUBJECT])
        self.assertEqual(read_exam_urls(), [EXAM])

    async def test_nested_class_does_not_automatically_enroll(self):
        with patch("core.discovery.train_class.collect_learning_links_from_class_page", new=AsyncMock(return_value=[])) as reader:
            await read_entry_manifest(object(), self.context, CLASS, nested=True)
            self.assertFalse(reader.await_args.kwargs["allow_enrollment"])
            await read_entry_manifest(object(), self.context, CLASS, nested=False)
            self.assertTrue(reader.await_args.kwargs["allow_enrollment"])


class TopicCardParsingTests(unittest.TestCase):
    def test_class_subtopic_relative_links_exclude_navigation_and_external_sites(self):
        html = f'''<smart-header><a href="{CHILD}">导航</a></smart-header>
        <a href="https://kc.zhixueyun.com/app/wechat/#/qrScan?businessType=6&amp;businessId=11111111-1111-4111-8111-111111111111\n">班</a>
        <a href="../child/pc.html">子专题</a><a href="../child/pc.html#section">重复</a>
        <a href="{ROOT}">自身</a><a href="https://unrelated.invalid/safe/topic/pc.html">外站</a>
        <a href="javascript:void(0)">脚本</a><a href="https://cms.mylearning.cn.evil.invalid/safe/topic/pc.html">伪装</a>'''
        self.assertEqual(extract_topic_entry_urls(html, ROOT), [CLASS, CHILD])
