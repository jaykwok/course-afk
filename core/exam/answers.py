from __future__ import annotations

import asyncio
import inspect
import logging
import time

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from core.exam.contracts import SYSTEM_PROMPT, answer_schema, build_question_prompt, parse_answer_payload, question_problem, valid_answers
from core.exam.settings import AiSettings, ExamAiConfigurationError
from core.runtime import close_safely

_RETRYABLE_API_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
MAX_ANSWER_CHARACTERS = 8192


class AiOutputError(ValueError):
    """A response did not finish or cannot safely be used as an answer."""


async def _close_stream(stream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await close_safely(result, label="AI stream")


async def _extract_responses_output_text(stream) -> str:
    if hasattr(stream, "output_text"):
        if getattr(stream, "status", None) != "completed":
            raise AiOutputError("AI 响应未完成")
        return stream.output_text or ""
    parts: dict[tuple[int, int], str] = {}
    completed = False
    try:
        async for event in stream:
            kind = getattr(event, "type", "")
            if kind in {"error", "response.failed", "response.incomplete", "response.cancelled"} or "refusal" in kind:
                raise AiOutputError("AI 响应失败、拒绝或不完整")
            key = (getattr(event, "output_index", 0), getattr(event, "content_index", 0))
            if kind == "response.output_text.delta":
                parts[key] = parts.get(key, "") + (getattr(event, "delta", "") or "")
            elif kind == "response.output_text.done":
                parts[key] = getattr(event, "text", "") or ""
            elif kind == "response.completed":
                completed = getattr(getattr(event, "response", None), "status", None) == "completed"
            if sum(map(len, parts.values())) > MAX_ANSWER_CHARACTERS:
                raise AiOutputError("AI 答案超过长度上限")
    finally:
        await _close_stream(stream)
    if not completed:
        raise AiOutputError("AI 流未收到 completed 终态")
    return "".join(parts[key] for key in sorted(parts))


async def _extract_chat_stream_text(stream) -> str:
    parts = []
    finished = False
    try:
        async for chunk in stream:
            for choice in getattr(chunk, "choices", None) or []:
                if getattr(choice, "index", 0) != 0:
                    raise AiOutputError("AI 返回了多个候选答案")
                delta = getattr(choice, "delta", None)
                if getattr(delta, "refusal", None) or getattr(delta, "tool_calls", None):
                    raise AiOutputError("AI 拒答或请求了未授权工具")
                content = getattr(delta, "content", None)
                if isinstance(content, str):
                    parts.append(content)
                reason = getattr(choice, "finish_reason", None)
                if reason is not None:
                    if reason != "stop":
                        raise AiOutputError("AI 答案未正常结束")
                    finished = True
                if sum(map(len, parts)) > MAX_ANSWER_CHARACTERS:
                    raise AiOutputError("AI 答案超过长度上限")
    finally:
        await _close_stream(stream)
    if not finished:
        raise AiOutputError("AI 流未收到 stop 终态")
    return "".join(parts)


def _build_request(settings: AiSettings, model: str, question: dict) -> dict:
    prompt = build_question_prompt(question)
    request = {"model": model, "stream": True}
    if settings.temperature is not None:
        request["temperature"] = settings.temperature
    if settings.request_type == "responses":
        request.update(instructions=SYSTEM_PROMPT, input=prompt)
        if settings.web_search:
            request["tools"] = [{"type": "web_search"}]
        if settings.reasoning_effort:
            request["reasoning"] = {"effort": settings.reasoning_effort}
        if settings.output_mode == "json_schema":
            request["text"] = {"format": {"type": "json_schema", "name": "exam_answer", "strict": True, "schema": answer_schema(question)}}
    else:
        request["messages"] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        if settings.reasoning_effort:
            request["reasoning_effort"] = settings.reasoning_effort
        if settings.output_mode == "json_schema":
            request["response_format"] = {"type": "json_schema", "json_schema": {"name": "exam_answer", "strict": True, "schema": answer_schema(question)}}
    if settings.provider == "compatible":
        extra = {}
        if not settings.reasoning_effort:
            extra["enable_thinking"] = settings.thinking
        if settings.request_type == "chat" and settings.web_search:
            extra["enable_search"] = True
        if extra:
            request["extra_body"] = extra
    return request


async def _request_ai_answer_text(client, model: str, question: dict, settings: AiSettings) -> str:
    request = _build_request(settings, model, question)
    create = client.responses.create if settings.request_type == "responses" else client.chat.completions.create
    if not inspect.iscoroutinefunction(inspect.unwrap(create)):
        raise ExamAiConfigurationError("AI 客户端必须使用异步接口")
    async with asyncio.timeout(settings.total_timeout):
        for attempt in range(settings.max_retries + 1):
            try:
                stream = await create(**request)
                if settings.request_type == "responses":
                    return await _extract_responses_output_text(stream)
                return await _extract_chat_stream_text(stream)
            except _RETRYABLE_API_ERRORS as exc:
                if attempt >= settings.max_retries:
                    raise
                logging.warning("AI 请求失败 %s，准备第 %s 次重试", type(exc).__name__, attempt + 1)
                await asyncio.sleep(min(2 ** attempt, 8))
    raise AiOutputError("AI 请求未完成")


def normalize_ai_answer_text(question_type: str, answer_text: str) -> list[str]:
    """Only a complete JSON payload is accepted; prose and reasoning are rejected."""
    answers = parse_answer_payload(answer_text)
    if question_type in {"single", "reading", "judge"} and len(answers) != 1:
        return []
    return answers


async def get_ai_answers(client, model, question_data):
    question_data.pop("answer_failure_reason", None)
    problem = question_problem(question_data)
    if problem:
        question_data["answer_failure_reason"] = problem
        logging.info("题目需要人工处理：%s", problem)
        return []
    if not model:
        raise ExamAiConfigurationError("AI 模型配置为空，请在 .env 中设置 MODEL_NAME")
    settings = getattr(client, "_course_afk_settings", None) or AiSettings.load()
    started = time.monotonic()
    try:
        text = await _request_ai_answer_text(client, model, question_data, settings)
        result = normalize_ai_answer_text(question_data["type"], text)
        if not valid_answers(question_data, result):
            raise AiOutputError("AI 输出不满足本题的选项约束")
        logging.info("AI 答题完成：题目 %s，耗时 %.2fs", question_data.get("item_id", question_data.get("index", "single")), time.monotonic() - started)
        return result
    except ExamAiConfigurationError:
        raise
    except Exception as exc:
        if "unsupported model" in str(exc).lower():
            raise ExamAiConfigurationError(f"模型 {model!r} 与当前协议不兼容，请调整 .env 后重试") from exc
        detail = str(exc) if isinstance(exc, AiOutputError) else "AI 请求失败或超时"
        question_data["answer_failure_reason"] = f"{detail}（{type(exc).__name__}）"
        logging.warning("题目 %s：AI 未提供可执行答案（%s），等待人工核对", question_data.get("item_id", question_data.get("index", "single")), type(exc).__name__)
        return []
