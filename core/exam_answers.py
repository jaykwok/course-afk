from __future__ import annotations

import logging
import re
import traceback

from core.config import (
    AI_ENABLE_THINKING,
    AI_ENABLE_WEB_SEARCH,
    AI_REASONING_EFFORT,
    AI_REQUEST_TYPE,
    AI_RESPONSE_TOOLS,
    AI_SYSTEM_PROMPT,
    AI_TEMPERATURE,
)


TYPE_LABELS = {
    "single": "单选题",
    "multiple": "多选题/不定项选择题",
    "judge": "判断题(请回答'正确'或'错误')",
    "ordering": "排序题(请按正确顺序给出选项字母, 如'ACBDEF')",
    "reading": "阅读理解题",
}

TYPE_HINTS = {
    "ordering": "请直接给出正确的排序顺序, 只需按字母顺序列出, 如'ACBDEF'。",
    "reading": "请直接回答选项代号(如A、B、C、D)。",
    "judge": "请直接回答'正确'或'错误'。",
}


class ExamAiConfigurationError(RuntimeError):
    """AI 考试配置错误，例如模型名不受当前接口支持。"""


def _is_unsupported_model_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "unsupported model" in message


def _extract_chat_message_text(completion) -> str:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
                continue
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return "".join(parts)
    return str(content or "")


def _extract_chat_delta_text(delta) -> tuple[str, str]:
    """返回 (content_part, reasoning_part)"""
    def _read_field(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
                    continue
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
            return "".join(parts)
        return ""

    content = _read_field(getattr(delta, "content", None))
    reasoning = _read_field(getattr(delta, "reasoning_content", None))
    return content, reasoning


def _close_stream_if_possible(stream_or_response) -> None:
    close = getattr(stream_or_response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _extract_responses_output_text(response_or_stream) -> str:
    if hasattr(response_or_stream, "output_text"):
        return getattr(response_or_stream, "output_text", "") or ""

    deltas: list[str] = []
    final_text = None
    try:
        for event in response_or_stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    deltas.append(str(delta))
            elif event_type == "response.output_text.done":
                text = getattr(event, "text", None)
                if text is not None:
                    final_text = str(text)
    finally:
        _close_stream_if_possible(response_or_stream)

    return final_text if final_text is not None else "".join(deltas)


def _extract_chat_stream_text(stream_or_completion) -> str:
    if hasattr(stream_or_completion, "choices"):
        return _extract_chat_message_text(stream_or_completion)

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    try:
        for chunk in stream_or_completion:
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content, reasoning = _extract_chat_delta_text(delta)
                if content:
                    content_parts.append(content)
                if reasoning:
                    reasoning_parts.append(reasoning)
    finally:
        _close_stream_if_possible(stream_or_completion)

    content_text = "".join(content_parts)
    if content_text:
        return content_text
    return "".join(reasoning_parts)


def _build_responses_request(model: str, prompt: str) -> dict:
    request_kwargs = {
        "model": model,
        "instructions": AI_SYSTEM_PROMPT,
        "input": prompt,
        "stream": True,
        "temperature": AI_TEMPERATURE,
    }
    if AI_RESPONSE_TOOLS:
        request_kwargs["tools"] = [tool.copy() for tool in AI_RESPONSE_TOOLS]
    if AI_REASONING_EFFORT:
        request_kwargs["reasoning"] = {"effort": AI_REASONING_EFFORT}
    elif AI_ENABLE_THINKING:
        request_kwargs["extra_body"] = {"enable_thinking": True}
    return request_kwargs


def _build_chat_request(model: str, prompt: str) -> dict:
    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "temperature": AI_TEMPERATURE,
    }
    extra_body: dict = {"enable_thinking": AI_ENABLE_THINKING}
    if AI_ENABLE_WEB_SEARCH:
        extra_body["enable_search"] = True
    request_kwargs["extra_body"] = extra_body
    return request_kwargs


def _request_ai_answer_text(client, model: str, prompt: str) -> str:
    if AI_REQUEST_TYPE == "responses":
        response_or_stream = client.responses.create(
            **_build_responses_request(model, prompt),
        )
        return _extract_responses_output_text(response_or_stream)

    if AI_REQUEST_TYPE == "chat":
        completion_or_stream = client.chat.completions.create(
            **_build_chat_request(model, prompt),
        )
        return _extract_chat_stream_text(completion_or_stream)

    raise ExamAiConfigurationError(
        f"AI_REQUEST_TYPE 配置无效: {AI_REQUEST_TYPE!r}，仅支持 'chat' 或 'responses'。"
    )


def build_question_prompt(question_data) -> str:
    question_type_str = TYPE_LABELS.get(question_data["type"], "")
    options_str = "".join(
        f"{option['label']}. {option['text']}\n"
        for option in question_data["options"]
    )
    prompt = f"""
        请回答以下{question_type_str}：

        问题：{question_data['text']}

        选项：
        {options_str}
        """
    prompt += TYPE_HINTS.get(
        question_data["type"],
        "请直接回答选项代号(如A、B、C、D等), 不定项选择题、多选题可以选择多个选项。",
    )
    return prompt


def normalize_ai_answer_text(question_type: str, answer_text: str) -> list[str]:
    final_answer = (answer_text or "").strip()
    normalized_upper = final_answer.upper()

    if question_type == "judge":
        lowered = final_answer.lower()
        if (
            "错误" in final_answer
            or re.search(r"\b(false|incorrect|wrong)\b", lowered)
            or re.search(r"(?<![a-z])f(?![a-z])", lowered)
        ):
            return ["错误"]
        if (
            "正确" in final_answer
            or re.search(r"\b(true|correct)\b", lowered)
            or re.search(r"(?<![a-z])t(?![a-z])", lowered)
        ):
            return ["正确"]
        logging.warning(f"无法识别的判断题答案: {final_answer}")
        return ["正确"]

    if question_type == "ordering":
        sequences = re.findall(r"(?<![A-Z])[A-Z]{2,}(?![A-Z])", normalized_upper)
        if sequences:
            return list(max(sequences, key=len))
        return re.findall(r"(?<![A-Z])[A-Z](?![A-Z])", normalized_upper)

    grouped_answers = re.findall(r"(?<![A-Z])[A-Z]+(?![A-Z])", normalized_upper)
    answers = [char for group in grouped_answers for char in group]
    if question_type in {"single", "reading"} and answers:
        return [answers[-1]]

    seen = set()
    unique_answers = [x for x in answers if not (x in seen or seen.add(x))]
    return unique_answers


async def get_ai_answers(client, model, question_data):
    """使用AI分析题目并获取答案"""
    try:
        if not model:
            raise ExamAiConfigurationError("AI 模型配置为空，请在 .env 中设置 MODEL_NAME。")

        if question_data["type"] == "fill_blank":
            logging.info("检测到填空题, 将跳过自动作答")
            return []

        answer_content = _request_ai_answer_text(
            client,
            model,
            build_question_prompt(question_data),
        )
        logging.info(f"AI最终答案: {answer_content}")
        return normalize_ai_answer_text(question_data["type"], answer_content)
    except ExamAiConfigurationError:
        raise
    except Exception as exc:
        if _is_unsupported_model_error(exc):
            logging.error(f"获取AI答案出错: {exc}")
            logging.error(traceback.format_exc())
            raise ExamAiConfigurationError(
                f"AI 请求方式与模型不兼容: 当前 MODEL_NAME={model!r} 与 AI_REQUEST_TYPE={AI_REQUEST_TYPE!r} 组合不可用，请调整 .env 中的 MODEL_NAME 或 AI_REQUEST_TYPE。"
            ) from exc
        logging.error(f"获取AI答案出错: {exc}")
        logging.error(traceback.format_exc())
        return []
