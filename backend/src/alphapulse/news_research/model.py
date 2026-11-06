"""Bounded model submission, validation repair and provider selection."""
import json
from pydantic import BaseModel, ValidationError
from alphapulse.ai import llm
from alphapulse.models import AiProvider


MAX_CONTEXT_WINDOW = 250_000


class OutputTruncated(ValueError):
    """A read-only model call may be repeated with a smaller input batch."""

    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage

class SubmissionFailure(ValueError):
    def __init__(self, message, usage, correction_attempted=False):
        super().__init__(message)
        self.usage = usage
        self.correction_attempted = correction_attempted

async def model_stream(*args):
    """Scope transport retry changes to news requests only."""
    async for item in llm.stream(*args):
        yield item


async def validate_submission(provider, system, messages, schema, completion, usages, allow_repair=True):
    """Repair structural or time-contract errors once using the same evidence."""
    calls = completion.tool_calls
    try:
        return schema.model_validate(calls[0]['arguments'])
    except ValidationError as error:
        errors = error.errors(include_input=False, include_url=False, include_context=False)
        if not allow_repair or not all(e['type'] in ('missing', 'extra_forbidden') or 'timeline_points' in e['loc'] and
                   (any(f in e['loc'] for f in ('occurred_at', 'reported_at', 'time_precision', 'planned_at', 'planned_precision'))
                    or e['msg'] in ('Value error, 发生时间与精度不一致', 'Value error, 发生时间必须有时区', 'Value error, 报道时间必须有时区', 'Value error, 计划时间与精度不一致', 'Value error, 计划时间必须有时区'))
                   for e in errors):
            raise SubmissionFailure(str(error), {'rounds': usages}) from error
    from alphapulse.news_research.progress import step
    step('纠正结构化输出（1/1）', model=provider.model, reasoning_effort=provider.reasoning_effort)
    assistant = {'role': 'assistant', 'text': completion.text, 'tool_calls': calls}
    if completion.thinking_blocks:
        assistant['thinking_blocks'] = completion.thinking_blocks
    messages = [*messages, assistant, {'role': 'tool', 'tool_call_id': calls[0]['id'],
        'result': json.dumps({'errors': errors, 'instruction': '根据errors逐项纠正字段缺失、层级或时间错误并重新submit完整结果；不得省略必填字段或补造事实。未知时间用null和unknown；保留原文证据，不添加查询。'}, ensure_ascii=False)}]
    if not fits_context(provider, system, {'messages': messages}, schema):
        raise SubmissionFailure('纠错上下文不足；证据未截断', {'rounds': usages}, True)
    repaired = None
    tool = llm.ToolSpec('submit', '纠正字段后提交完整结果', schema.model_json_schema())
    try:
        async for item in model_stream(provider, system, messages, [tool], provider.reasoning_effort):
            if isinstance(item, llm.Completion): repaired = item
        if repaired is not None: usages.append(repaired.usage)
        if repaired is None or repaired.truncated:
            raise ValueError('结构化输出纠错输出未完成或被截断')
        if len(repaired.tool_calls) != 1 or repaired.tool_calls[0]['name'] != 'submit':
            raise ValueError('结构化输出纠错必须只提交一次结果')
        return schema.model_validate(repaired.tool_calls[0]['arguments'])
    except Exception as error:
        raise SubmissionFailure(str(error), {'rounds': usages}, True) from error

async def complete(provider: AiProvider, system: str, payload: dict, schema: type[BaseModel]):
    from alphapulse.news_research.progress import step, current
    if current.get() is not None: step('等待模型响应', model=provider.model, reasoning_effort=provider.reasoning_effort)
    text = json.dumps(payload, ensure_ascii=False)
    if not fits_context(provider, system, payload, schema):
        raise ValueError('新闻输入超出模型上下文；未截断或发送，请调整模型配置')
    completion = None
    tool = llm.ToolSpec('submit', '提交有依据的结构化结果', schema.model_json_schema())
    async for item in model_stream(provider, system, [{'role': 'user', 'text': text}], [tool], provider.reasoning_effort):
        if isinstance(item, llm.Completion):
            completion = item
    if completion is not None and completion.truncated:
        raise OutputTruncated('模型输出被截断', completion.usage)
    if completion is None:
        raise ValueError('模型未完成输出或输出被截断')
    calls = [call for call in completion.tool_calls if call['name'] == 'submit']
    if len(calls) != 1 or len(completion.tool_calls) != 1:
        raise SubmissionFailure('模型必须提交一次完整结构化结果', completion.usage)
    usages = [completion.usage]
    result = await validate_submission(provider, system, [{'role': 'user', 'text': text}], schema, completion, usages, allow_repair="correction" not in payload)
    return result, completion.usage if len(usages) == 1 else {'rounds': usages}

def fits_context(provider, system, payload, schema):
    # UTF-8 bytes bound source tokens conservatively, including the tool schema.
    text = system + json.dumps(payload, ensure_ascii=False) + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return len(text.encode('utf-8')) + provider.max_tokens <= min(provider.context_window, MAX_CONTEXT_WINDOW)

async def provider_for(session, settings):
    provider = await session.get(AiProvider, settings.provider_id) if settings.provider_id else None
    if provider is None or not settings.model.strip():
        raise ValueError('请为新闻风控选择接入配置和模型名称')
    # Override only the detached instance; never change shared provider settings.
    session.expunge(provider)
    provider.model = settings.model
    return provider
