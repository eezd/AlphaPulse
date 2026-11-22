"""单篇分析的草稿清理和共享的一次定向纠错预算。"""
from alphapulse.ai import submission
from alphapulse.briefings import contracts, keywords


class AnalysisFailure(ValueError):
    def __init__(self, message, usage):
        super().__init__(message)
        self.usage = usage


def normalize_draft(draft, article):
    raw = draft.model_dump()
    original = raw['keywords']
    cleaned, positions, decisions = [], {}, []
    for word in original:
        aliases = []
        for alias in word['aliases']:
            if not alias.strip() or alias in aliases:
                decisions.append({'action': 'drop_blank_or_duplicate_alias', 'canonical': word['canonical'], 'alias': alias})
            else:
                aliases.append(alias)
        word = {**word, 'aliases': aliases}
        identity = word['canonical'].casefold()
        if identity in positions:
            existing = cleaned[positions[identity]]
            if (existing['kind'], existing['quote']) != (word['kind'], word['quote']):
                raise ValueError('关键词 canonical 重复且类别或引文冲突')
            existing['aliases'] = list(dict.fromkeys([*existing['aliases'], *aliases]))
            decisions.append({'action': 'merge_identical_keyword', 'canonical': word['canonical']})
        else:
            positions[identity] = len(cleaned)
            cleaned.append(word)
    result = contracts.NewsAnalysisResult.model_validate({**raw, 'keywords': cleaned})
    result, audit = keywords.normalize(result, article)
    return result, {**audit, 'draft_raw_keywords': original, 'draft_decisions': decisions}


class Budget:
    """全部 map/final 共用一个修复名额；传输重试由 llm.stream 独立负责。"""
    def __init__(self, provider, *, on_repair_start=None, on_repair_end=None):
        self.provider = provider
        self.used = False
        self.usages = []
        self.on_repair_start = on_repair_start
        self.on_repair_end = on_repair_end
        self.last_raw = None
        self.active_part = None
        self.active_start = 0

    @property
    def usage(self):
        return {'rounds': list(self.usages)} if self.usages else None

    def _record(self, value):
        if isinstance(value, dict) and isinstance(value.get('rounds'), list):
            self.usages.extend(value['rounds'])
            if len(value['rounds']) > 1:
                self.used = True
        else:
            self.usages.append(value)

    async def submit(self, system, payload, schema, validate):
        self.last_raw = None
        if not submission.fits_context(self.provider, system, payload, schema):
            raise AnalysisFailure('分析输入或纠错上下文不足；原文未截断', self.usage)
        try:
            result, usage = await submission.complete(self.provider, system, payload, schema, allow_repair=False)
        except Exception as error:
            self._record(getattr(error, 'usage', None))
            self.last_raw = getattr(error, 'arguments', None)
            self.used |= getattr(error, 'correction_attempted', False)
            if not isinstance(error, submission.SubmissionFailure) or self.used or error.arguments is None:
                raise AnalysisFailure(str(error), self.usage) from error
            previous, failure = error.arguments, error
        else:
            self._record(usage)
            self.last_raw = result.model_dump()
            try:
                return validate(result)
            except (ValueError, TypeError) as error:
                if self.used:
                    raise AnalysisFailure(str(error), self.usage) from error
                previous, failure = result.model_dump(), error
        self.used = True
        correction = {'correction': True, 'original': payload, 'previous': previous,
                      'error': str(failure),
                      'instruction': ('只修正错误路径；若material_anchors缺少facts，请用同字段连续原文补足；'
                                      '若短引文多处，请提供本段唯一的连续原文anchor或更长的逐字引文。'
                                      '保留冻结原文证据，重新提交完整结果。')}
        if not submission.fits_context(self.provider, system, correction, schema):
            raise AnalysisFailure('纠错上下文不足；原文未截断', self.usage)
        before = len(self.usages)
        if self.on_repair_start:
            await self.on_repair_start(system, correction, schema)
        try:
            repaired, usage = await submission.complete(self.provider, system, correction, schema, allow_repair=False)
            self._record(usage)
            self.last_raw = repaired.model_dump()
            validated = validate(repaired)
        except Exception as error:
            if len(self.usages) == before:
                self._record(getattr(error, 'usage', None))
            if self.on_repair_end:
                await self.on_repair_end(self.last_raw or getattr(error, 'arguments', None),
                                         {'rounds': self.usages[before:]}, error)
            raise AnalysisFailure(str(error), self.usage) from error
        if self.on_repair_end:
            await self.on_repair_end(repaired.model_dump(), {'rounds': self.usages[before:]}, None)
        return validated


async def analyze(provider, system, payload, budget):
    return await budget.submit(system, payload, contracts.NewsAnalysisDraft,
                               lambda draft: normalize_draft(draft, payload))
