import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _responses_stream(*events):
    return list(events)


def _chat_stream(*chunks):
    return list(chunks)


class ExamAnswerTests(unittest.TestCase):
    def test_build_question_prompt_includes_type_text_and_options(self):
        from core.exam_answers import build_question_prompt

        prompt = build_question_prompt(
            {
                "type": "single",
                "text": "示例公司的英文缩写是什么？",
                "options": [
                    {"label": "A", "text": "CT"},
                    {"label": "B", "text": "CU"},
                ],
            }
        )

        self.assertIn("单选题", prompt)
        self.assertIn("示例公司的英文缩写是什么？", prompt)
        self.assertIn("A. CT", prompt)
        self.assertIn("B. CU", prompt)

    def test_normalize_ai_answer_text_handles_judge_and_ordering_and_multi(self):
        from core.exam_answers import normalize_ai_answer_text

        self.assertEqual(normalize_ai_answer_text("judge", "答案：正确"), ["正确"])
        self.assertEqual(normalize_ai_answer_text("judge", "The answer is false"), ["错误"])
        self.assertEqual(normalize_ai_answer_text("judge", "TRUE"), ["正确"])
        self.assertEqual(normalize_ai_answer_text("ordering", "正确顺序是 ACBD"), ["A", "C", "B", "D"])
        self.assertEqual(normalize_ai_answer_text("multiple", "我选 A、C、A、D"), ["A", "C", "D"])
        self.assertEqual(normalize_ai_answer_text("single", "答案是 b"), ["B"])
        self.assertEqual(normalize_ai_answer_text("multiple", "我选 ac"), ["A", "C"])

    def test_normalize_ai_answer_text_keeps_only_final_single_choice_from_reasoning(self):
        from core.exam_answers import normalize_ai_answer_text

        self.assertEqual(
            normalize_ai_answer_text("single", "A 不符合题意，B 才是正确答案。"),
            ["B"],
        )
        self.assertEqual(
            normalize_ai_answer_text("single", "A 看起来相关，B 也可排除，最终答案仍是 A。"),
            ["A"],
        )


class ExamAnswerResponsesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_ai_answers_raises_configuration_error_for_unsupported_model(self):
        from core import exam_answers

        create = Mock(
            side_effect=RuntimeError(
                "Error code: 400 - {'code': 'InvalidParameter', 'message': "
                "\"Unsupported model: 'qwen3.6-max-preview'.\"}"
            )
        )
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "responses"),
            self.assertRaises(exam_answers.ExamAiConfigurationError) as ctx,
        ):
            await exam_answers.get_ai_answers(
                client,
                "qwen3.6-max-preview",
                question_data,
            )

        self.assertIn("qwen3.6-max-preview", str(ctx.exception))

    async def test_get_ai_answers_uses_responses_web_search_when_enabled(self):
        from core import exam_answers

        create = Mock(
            return_value=_responses_stream(
                SimpleNamespace(type="response.output_text.delta", delta="A"),
                SimpleNamespace(type="response.output_text.done", text="A"),
            )
        )
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "responses"),
            patch.object(exam_answers, "AI_RESPONSE_TOOLS", [{"type": "web_search"}]),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-plus", question_data)

        self.assertEqual(answers, ["A"])
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["model"], "qwen3.6-plus")
        self.assertEqual(create.call_args.kwargs["tools"], [{"type": "web_search"}])
        self.assertTrue(create.call_args.kwargs["stream"])

    async def test_get_ai_answers_omits_tools_when_web_search_disabled(self):
        from core import exam_answers

        create = Mock(
            return_value=_responses_stream(
                SimpleNamespace(type="response.output_text.delta", delta="A"),
                SimpleNamespace(type="response.output_text.done", text="A"),
            )
        )
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "responses"),
            patch.object(exam_answers, "AI_RESPONSE_TOOLS", None),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-plus", question_data)

        self.assertEqual(answers, ["A"])
        create.assert_called_once()
        self.assertNotIn("tools", create.call_args.kwargs)
        self.assertTrue(create.call_args.kwargs["stream"])

    async def test_get_ai_answers_uses_responses_reasoning_effort_when_configured(self):
        from core import exam_answers

        create = Mock(
            return_value=_responses_stream(
                SimpleNamespace(type="response.output_text.delta", delta="A"),
                SimpleNamespace(type="response.output_text.done", text="A"),
            )
        )
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "responses"),
            patch.object(exam_answers, "AI_ENABLE_THINKING", True),
            patch.object(exam_answers, "AI_REASONING_EFFORT", "high"),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-plus", question_data)

        self.assertEqual(answers, ["A"])
        self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "high"})
        self.assertNotIn("extra_body", create.call_args.kwargs)

    async def test_get_ai_answers_uses_responses_enable_thinking_when_effort_missing(self):
        from core import exam_answers

        create = Mock(
            return_value=_responses_stream(
                SimpleNamespace(type="response.output_text.delta", delta="A"),
                SimpleNamespace(type="response.output_text.done", text="A"),
            )
        )
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "responses"),
            patch.object(exam_answers, "AI_ENABLE_THINKING", True),
            patch.object(exam_answers, "AI_REASONING_EFFORT", None),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-plus", question_data)

        self.assertEqual(answers, ["A"])
        self.assertEqual(
            create.call_args.kwargs["extra_body"],
            {"enable_thinking": True},
        )


class ExamAnswerChatApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_ai_answers_uses_chat_completions_with_web_search_when_enabled(self):
        from core import exam_answers

        create = Mock(
            return_value=_chat_stream(
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="A"),
                        )
                    ]
                )
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "chat"),
            patch.object(exam_answers, "AI_ENABLE_WEB_SEARCH", True),
            patch.object(exam_answers, "AI_ENABLE_THINKING", False),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-max-preview", question_data)

        self.assertEqual(answers, ["A"])
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["model"], "qwen3.6-max-preview")
        self.assertEqual(
            create.call_args.kwargs["extra_body"],
            {"enable_thinking": False, "enable_search": True},
        )
        self.assertTrue(create.call_args.kwargs["stream"])
        self.assertEqual(
            create.call_args.kwargs["messages"][0]["role"],
            "system",
        )
        self.assertEqual(
            create.call_args.kwargs["messages"][1]["role"],
            "user",
        )

    async def test_get_ai_answers_sends_chat_thinking_disabled_when_configured(self):
        from core import exam_answers

        create = Mock(
            return_value=_chat_stream(
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="A"),
                        )
                    ]
                )
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "chat"),
            patch.object(exam_answers, "AI_ENABLE_WEB_SEARCH", False),
            patch.object(exam_answers, "AI_ENABLE_THINKING", False),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-max-preview", question_data)

        self.assertEqual(answers, ["A"])
        create.assert_called_once()
        self.assertEqual(
            create.call_args.kwargs["extra_body"],
            {"enable_thinking": False},
        )
        self.assertTrue(create.call_args.kwargs["stream"])

    async def test_get_ai_answers_uses_chat_enable_thinking_and_search_together(self):
        from core import exam_answers

        create = Mock(
            return_value=_chat_stream(
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="A"),
                        )
                    ]
                )
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch.object(exam_answers, "AI_REQUEST_TYPE", "chat"),
            patch.object(exam_answers, "AI_ENABLE_WEB_SEARCH", True),
            patch.object(exam_answers, "AI_ENABLE_THINKING", True),
        ):
            answers = await exam_answers.get_ai_answers(client, "qwen3.6-max-preview", question_data)

        self.assertEqual(answers, ["A"])
        self.assertEqual(
            create.call_args.kwargs["extra_body"],
            {"enable_search": True, "enable_thinking": True},
        )

    async def test_get_ai_answers_rejects_unknown_request_type(self):
        from core import exam_answers

        client = SimpleNamespace()
        question_data = {
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with patch.object(exam_answers, "AI_REQUEST_TYPE", "invalid"):
            with self.assertRaises(exam_answers.ExamAiConfigurationError):
                await exam_answers.get_ai_answers(client, "qwen3.6-max-preview", question_data)


if __name__ == "__main__":
    unittest.main()
