import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from core.abort import WafBlockError
from core.browser.page_auth import fetch_json
from core.diagnostics import redact_snapshot, redact_text
from core.discovery import reference_collector as reference
from core.discovery import subject_parse
from core.learning.exam_api import evaluate_course_exam_state
from core.queues.exam import append_exam_url, read_exam_urls
from core.storage import write_text_atomic


class PersistenceBoundaryTests(unittest.TestCase):
    def test_concurrent_appends_do_not_overwrite_other_tasks(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "exam.json"
            urls = [f"https://example.com/{i}" for i in range(24)]
            with ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(lambda url: append_exam_url(url, file_path=path), urls))
            self.assertEqual(set(read_exam_urls(path)), set(urls))

    def test_failed_replace_keeps_previous_file_and_removes_temporary(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.json"
            path.write_text("previous", encoding="utf-8")
            with patch("core.storage.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_text_atomic(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(Path(tmp).iterdir()), [path])


class RedactionBoundaryTests(unittest.TestCase):
    def test_nested_urls_tokens_and_login_fragments_are_redacted(self):
        value = {
            "authorization": "secret-1",
            "error": 'https://user:secret-2@kc.zhixueyun.com/#/login/secret-3 Authorization: "secret-4"',
            "items": ["https://cdn.zhixueyun.com/file?Signature=secret-5&x=ok", "Bearer secret-6"],
            "bodyHtml": '<div class="layout" data-token="secret-7">secret-8<script>secret-9</script></div>',
            "plainHtml": "https://example.test/secret-10",
        }
        rendered = json.dumps(redact_snapshot(value))
        for i in range(1, 11):
            self.assertNotIn(f"secret-{i}", rendered)
        self.assertIn("layout", rendered)
        self.assertNotIn("SECRET", redact_text("https://kc.zhixueyun.com/#LOGIN/SECRET"))


class DownloadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_to_cdn_does_not_forward_authorization_or_cookies(self):
        requests = []
        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": "https://cdn.zhixueyun.com/file.pdf", "set-cookie": "secret=value; Domain=.zhixueyun.com; Path=/"})
            return httpx.Response(200, content=b"%PDF-1.7\nbody")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)
        with patch.object(reference.httpx, "AsyncClient", return_value=client):
            self.assertTrue((await reference._download_preview("https://kc.zhixueyun.com/api/file", "Bearer__test")).startswith(b"%PDF-"))
        self.assertEqual(requests[0].headers.get("authorization"), "Bearer__test")
        self.assertNotIn("authorization", requests[1].headers)
        self.assertNotIn("cookie", requests[1].headers)
        self.assertTrue(client.is_closed)

    async def test_untrusted_redirect_is_rejected_before_request(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://untrusted.example/file"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(reference.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(ValueError):
                await reference._download_preview("https://kc.zhixueyun.com/api/file", "dummy")
        self.assertEqual(len(requests), 1)
        self.assertTrue(client.is_closed)

    async def test_download_limit_closes_response(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"123456789")))
        with patch.object(reference, "MAX_DOCUMENT_BYTES", 8), patch.object(reference.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(ValueError):
                await reference._download_preview("https://cdn.zhixueyun.com/file", "dummy")
        self.assertTrue(client.is_closed)

    def test_userinfo_lookalike_hosts_and_html_files_are_rejected(self):
        for url in ("@evil.example/file", "//evil.example/file", "https://kc.zhixueyun.com@evil.example/file", "https://kc.zhixueyun.com:444/file", "https://evilzhixueyun.com/file", "https://kc.zhixueyun.com\\@evil.example/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                reference.full_preview_url(url)
        with self.assertRaises(ValueError):
            reference.decode_preview_bytes(b"<html>login</html>", "pdf")

    async def test_api_response_is_disposed_on_json_failure_or_cancellation(self):
        for failure in (ValueError("bad JSON"), asyncio.CancelledError()):
            response = SimpleNamespace(ok=True, json=AsyncMock(side_effect=failure), dispose=AsyncMock())
            request = SimpleNamespace(get=AsyncMock(return_value=response))
            page = SimpleNamespace(context=SimpleNamespace(request=request))
            with self.assertRaises(type(failure)):
                await fetch_json(page, "https://kc.zhixueyun.com/api/test", headers={})
            response.dispose.assert_awaited_once()
            self.assertEqual(request.get.call_args.kwargs["max_redirects"], 0)

    async def test_invalid_course_and_guide_payloads_are_not_successful_empty_results(self):
        failures = []
        with patch.object(reference, "fetch_json", new=AsyncMock(return_value={"error": "invalid"})):
            self.assertEqual(await reference._fetch_course_infos(object(), [reference.SubjectCourse("id", "title")], subject_id="subject", auth_header="dummy", failure_results=failures), [])
            with self.assertRaises(ValueError):
                await reference._fetch_video_guide(object(), reference.SectionResource(1, "id", "title", "", 1, "video", "attachment", "mp4"), auth_header="dummy")
        self.assertEqual(len(failures), 1)

    async def test_retry_interrupted_before_first_resource_preserves_prior_manifest(self):
        @asynccontextmanager
        async def browser(**_kwargs):
            yield None, SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
        resource = reference.SectionResource(1, "course", "title", "", 1, "document", "attachment", "pdf")
        info = {"course_id": "course", "data": {"name": "title", "courseChapters": []}}
        url = "https://kc.zhixueyun.com/#/study/subject/detail/11111111-1111-1111-1111-111111111111"
        with TemporaryDirectory() as tmp:
            async def download(_page, resource, *, docs_dir, **_kwargs):
                path = docs_dir / "test.pdf"
                path.write_bytes(b"%PDF-test")
                import hashlib
                return {"ok": True, "course_id": resource.course_id, "section_index": 1, "attachment_id": resource.attachment_id, "saved": {"path": str(path), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
            with patch.object(reference, "create_browser_context", browser), patch.object(reference, "get_authorization_header", new=AsyncMock(return_value="dummy")), patch.object(reference, "_collect_courses_from_subject_page", new=AsyncMock(return_value=[reference.SubjectCourse("course", "title")])), patch.object(reference, "_fetch_course_infos", new=AsyncMock(return_value=[info])), patch.object(reference, "collect_section_resources", return_value=[resource]), patch.object(reference, "_download_document_resource", new=download):
                result = await reference.collect_reference_materials([url], output_root=Path(tmp))
            manifest_path = Path(result["output_dir"]) / "manifest.json"
            with patch.object(reference, "create_browser_context", browser), patch.object(reference, "_collect_courses_from_subject_page", new=AsyncMock(side_effect=asyncio.CancelledError())):
                with self.assertRaises(asyncio.CancelledError):
                    await reference.collect_reference_materials([url], output_root=Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "interrupted")
            self.assertEqual(len(manifest["documents"]), 1)
            self.assertEqual(len(manifest["courses"]), 1)
            self.assertIn("test.pdf", next((Path(result["output_dir"]) / "courses").glob("*.md")).read_text(encoding="utf-8"))


class WorkflowBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_browser_start_preserves_all_discovery_entries(self):
        from core.learning import zone
        from core.queues.learning import remember_discovery_entries, read_learning_failures
        urls = ["https://cms.mylearning.cn/topic/a", "https://cms.mylearning.cn/topic/b"]
        with TemporaryDirectory() as tmp:
            failure_file = Path(tmp) / "failures.json"
            with patch.object(zone, "remember_discovery_entries", side_effect=lambda values: remember_discovery_entries(values, file_path=failure_file)):
                with self.assertRaises(OSError):
                    await zone.collect_learning_links_from_learning_zone_urls(urls, context=SimpleNamespace(new_page=AsyncMock(side_effect=OSError("browser failed"))))
            self.assertEqual([item.url for item in read_learning_failures(failure_file)], urls)

    async def test_pending_grading_is_queued_for_review_instead_of_completed(self):
        from core.learning import exam_api
        exam_id = "11111111-1111-1111-1111-111111111111"
        page = SimpleNamespace(url="https://kc.zhixueyun.com/#/study/course/detail/22222222-2222-2222-2222-222222222222")
        course = {"courseChapters": [{"courseChapterSections": [{"sectionType": 9, "resourceId": exam_id}]}]}
        payloads = [course, [{"id": exam_id, "passScore": 60}], {"examRecord": {"isFinished": 1, "score": None}}]
        with patch.object(exam_api, "get_authorization_header", new=AsyncMock(return_value="dummy")), patch.object(exam_api, "fetch_json", new=AsyncMock(side_effect=payloads)), patch.object(exam_api, "append_manual_exam_entry") as manual:
            result = await exam_api.queue_course_exams_from_api(page)
        self.assertEqual(result.completed, 0)
        self.assertEqual(result.manual_queued, 1)
        self.assertEqual(manual.call_args.kwargs["reason"], "pending_grading")

    async def test_subject_waf_preserves_unvisited_entries_and_propagates(self):
        urls = ["https://kc.zhixueyun.com/#/study/subject/detail/11111111-1111-1111-1111-111111111111", "https://kc.zhixueyun.com/#/study/subject/detail/22222222-2222-2222-2222-222222222222"]
        from core.discovery.recursive import collect_discovery_entries
        from core.queues.learning import read_learning_failures
        page = SimpleNamespace(close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with patch.object(subject_parse, "expand_subject_from_page", new=AsyncMock(side_effect=WafBlockError())) as expand:
            with self.assertRaises(WafBlockError):
                await collect_discovery_entries(urls, context=context)
        expand.assert_awaited_once()
        page.close.assert_awaited_once()
        self.assertTrue(set(urls) <= {item.url for item in read_learning_failures()})

    def test_scores_are_monotone_and_unknown_attempts_remain_unknown(self):
        outcomes = [evaluate_course_exam_state({"passScore": 3, "paperClass": {"totalScore": 500}}, {"examRecord": {"isFinished": 1, "score": score}}).passed for score in (0, 99, 100, 101, 299, 300, 400)]
        self.assertEqual(outcomes, [False, False, False, False, False, True, True])
        self.assertIsNone(evaluate_course_exam_state({"allowExamTimes": 5}, {}).remaining_attempts)

    def test_old_top_score_cannot_hide_new_pending_grading(self):
        state = evaluate_course_exam_state({"passScore": 60, "examRegist": {"topScore": 30}}, {"examRecord": {"id": "new", "isFinished": 1, "score": None}})
        self.assertEqual(state.outcome, "pending_grading")
