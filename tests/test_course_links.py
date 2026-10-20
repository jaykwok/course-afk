import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from core.file_ops import build_subject_course_url, is_compliant_url_regex, is_course_detail_url, normalize_url
from core.links import normalize_urls, split_manual_selection_urls

COURSE = "d85a1e39-c850-47ff-99f6-98e0791352ef"
SUBJECT = "5ecd454b-9d78-41f8-891b-1ee4428929cf"
OTHER_SUBJECT = "1d40d4e0-a622-4535-8f02-ad108a930656"
BARE = f"https://kc.zhixueyun.com/#/study/course/detail/{COURSE}"
SUBJECT_URL = f"https://kc.zhixueyun.com/#/study/subject/detail/{SUBJECT}"
FULL = f"{BARE}/6/1/{SUBJECT}"
QUERY = f"{BARE}?type=6&required=1&sourceId={SUBJECT}"


class CourseLinkTests(unittest.TestCase):
    def test_context_survives_repeated_normalization_and_manual_classification(self):
        self.assertEqual(normalize_url(normalize_url(FULL)), FULL)
        self.assertTrue(is_course_detail_url(FULL))
        self.assertTrue(is_compliant_url_regex(FULL))
        self.assertEqual(split_manual_selection_urls([FULL]), ([FULL], [], [], [], []))
        self.assertEqual(normalize_url(FULL + '?from=topic'), FULL)

    def test_complete_query_becomes_route_but_partial_context_is_rejected(self):
        self.assertEqual(normalize_url(QUERY), FULL)
        self.assertEqual(normalize_url(QUERY.replace('required=', 'isRequired=')), FULL)
        for bad in (
            BARE + '/6', BARE + '/6/1', BARE + '/6/' + SUBJECT,
            BARE + '/6/2/' + SUBJECT, BARE + '/6/1/not-a-uuid',
            BARE + '/6/1/' + SUBJECT + '/extra',
            BARE + '?type=6&sourceId=' + SUBJECT,
            BARE + '?type=6&required=1&sourceId=',
            QUERY + '&sourceId=' + OTHER_SUBJECT,
            QUERY + '&require=0',
        ):
            with self.subTest(url=bad):
                self.assertNotEqual(normalize_url(bad), BARE)
                self.assertFalse(is_compliant_url_regex(bad))

    def test_encoded_and_numeric_marker_routes_preserve_context(self):
        self.assertEqual(normalize_url(FULL.replace('/6/1/', '%2F6%2F1%2F')), FULL)
        self.assertEqual(normalize_url(FULL.replace('/detail/', '/detail/99@@')), FULL)

    def test_context_is_part_of_identity(self):
        other = build_subject_course_url(COURSE, OTHER_SUBJECT, 1)
        optional = build_subject_course_url(COURSE, SUBJECT, 0)
        self.assertEqual(normalize_urls([FULL, QUERY, other, optional, BARE]), [FULL, other, optional, BARE])

    def test_builder_preserves_required_and_rejects_missing_or_invalid_metadata(self):
        self.assertEqual(build_subject_course_url(COURSE, SUBJECT, True), FULL)
        for required in (None, '', 2, 'true', 1.0):
            with self.subTest(required=required), self.assertRaises(ValueError):
                build_subject_course_url(COURSE, SUBJECT, required)

    def test_subject_without_required_retains_subject_instead_of_generating_bare_link(self):
        from core.discovery.subject_parse import expand_chapter_progress
        payload = [{'courseChapterSections': [{'sectionType': 10, 'resourceId': COURSE}]}]
        result = expand_chapter_progress(payload, subject_url=SUBJECT_URL)
        self.assertEqual(result.course_urls, [])
        self.assertEqual(result.residual_subject_url, SUBJECT_URL)
        payload[0]['courseChapterSections'][0]['required'] = 0
        self.assertEqual(expand_chapter_progress(payload, subject_url=SUBJECT_URL).course_urls, [f'{BARE}/6/0/{SUBJECT}'])

    def test_queue_round_trip_retry_and_failure_removal_keep_context(self):
        from core.queues.learning import append_learning_urls, read_learning_urls, record_learning_failure, requeue_retryable_learning_failures, remove_learning_failure, read_learning_failures, write_learning_urls
        from core.learning.afk_runner import prepare_afk_batch
        with TemporaryDirectory() as tmp:
            queue, failures = Path(tmp)/'queue.json', Path(tmp)/'failures.json'
            other = build_subject_course_url(COURSE, OTHER_SUBJECT, 1)
            self.assertEqual(append_learning_urls([QUERY, FULL, other], file_path=queue), [FULL, other])
            self.assertEqual([x['url'] for x in json.loads(queue.read_text(encoding='utf-8'))], [FULL, other])
            self.assertEqual(prepare_afk_batch(learning_file=queue).urls, [FULL, other])
            write_learning_urls([], file_path=queue)
            record_learning_failure(QUERY, reason='course_info_api_error', reason_text='retry', file_path=failures)
            self.assertEqual(requeue_retryable_learning_failures(learning_file=queue, failures_file=failures), [FULL])
            self.assertEqual(read_learning_urls(queue), [FULL])
            record_learning_failure(FULL, reason='sync_timeout', reason_text='retry', file_path=failures)
            remove_learning_failure(QUERY, file_path=failures)
            self.assertEqual(read_learning_failures(failures), [])

    def test_verified_migration_is_scoped_idempotent_and_keeps_failure_details(self):
        from core.queues.learning import replace_learning_urls, write_learning_urls, read_learning_urls, read_learning_failures, record_learning_failure
        with TemporaryDirectory() as tmp:
            queue, failures = Path(tmp)/'queue.json', Path(tmp)/'failures.json'
            other = build_subject_course_url(COURSE, OTHER_SUBJECT, 1)
            write_learning_urls([BARE, other, FULL], file_path=queue)
            record_learning_failure(BARE, reason='course_info_api_error', reason_text='422', detail={'http_status':422}, file_path=failures)
            self.assertEqual(replace_learning_urls({BARE: FULL}, learning_file=queue, failures_file=failures), (1,1))
            self.assertEqual(read_learning_urls(queue), [FULL, other])
            entry = read_learning_failures(failures)[0]
            self.assertEqual((entry.url, entry.detail), (FULL, {'http_status':422}))
            self.assertEqual(replace_learning_urls({BARE: FULL}, learning_file=queue, failures_file=failures), (0,0))
            with self.assertRaises(ValueError):
                replace_learning_urls({FULL: BARE+'/6'}, learning_file=queue, failures_file=failures)
            self.assertEqual(read_learning_urls(queue), [FULL, other])

    def test_prune_keeps_context_and_explicit_invalid_context_record(self):
        from core.queues.learning import record_learning_failure, prune_invalid_learning_failures, read_learning_failures
        with TemporaryDirectory() as tmp:
            failures = Path(tmp)/'failures.json'
            record_learning_failure(FULL, reason='sync_timeout', reason_text='retry', file_path=failures)
            record_learning_failure(BARE+'/6', reason='non_compliant_url', reason_text='fix source', file_path=failures)
            self.assertEqual(prune_invalid_learning_failures(file_path=failures), [])
            self.assertEqual(len(read_learning_failures(failures)), 2)


class TemporaryCourseRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_recording_resolves_placeholder_to_durable_subject_route(self):
        from core.app.workflows import _wait_for_manual_popup_record_url
        page = type('Page', (), {'url':BARE.rsplit('/',1)[0]+'/detailInfoABCD/detailInfo1/detailInfo2', 'evaluate':AsyncMock(return_value={'id':COURSE,'type':6,'required':1,'sourceId':SUBJECT})})()
        self.assertEqual(await _wait_for_manual_popup_record_url(page), FULL)

    async def test_unresolved_or_invalid_placeholder_does_not_turn_into_bare_link(self):
        from core.browser.course_links import resolve_course_page_url
        for data in (None, {'id':COURSE,'type':6,'sourceId':SUBJECT}, {'id':COURSE,'type':6,'required':1,'sourceId':'invalid'}):
            page = type('Page', (), {'url':BARE.rsplit('/',1)[0]+'/detailInfoABCD/detailInfo1/detailInfo2', 'evaluate':AsyncMock(return_value=data)})()
            self.assertIsNone(await resolve_course_page_url(page))

    async def test_process_url_navigates_with_full_context_and_installs_course_monitor(self):
        from core.learning.afk_runner import _process_url
        page = type('Page', (), {'is_closed': lambda self:False, 'close':AsyncMock()})()
        context = object()
        with TemporaryDirectory() as tmp, patch('core.learning.afk_runner._open_course_page', AsyncMock(return_value=page)), patch('core.learning.afk_runner._install_course_info_monitor', return_value=object()) as monitor, patch('core.learning.afk_runner._wait_for_course_bootstrap', AsyncMock()), patch('core.learning.afk_runner.goto_and_prepare_async', AsyncMock()) as goto, patch('core.learning.afk_runner._close_page_quiet', AsyncMock()):
            self.assertFalse(await _process_url(context, FULL, AsyncMock(return_value=True), failure_file=Path(tmp)/'failures.json'))
        monitor.assert_called_once_with(page)
        goto.assert_awaited_once_with(page, FULL)

