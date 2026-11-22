"""从固定公开快照构建检索词表；不访问网络或生产服务。"""
import argparse
import hashlib
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / 'backend/src/alphapulse/briefings/data/lexicon'


def build(directory=DATA):
    manifest = json.loads((directory / 'sources/manifest.json').read_text())
    sources = {}
    for name, metadata in manifest['sources'].items():
        data = (directory / 'sources' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata['sha256']:
            raise ValueError(f'词表来源摘要不匹配：{name}')
        sources[name] = json.loads(data)
    entries = {}
    for item in sources['sec.json'].values():
        identity = f"sec:{item['cik_str']}"
        entry = entries.setdefault(identity, {'id': identity, 'canonical': item['title'], 'kind': 'entity', 'aliases': []})
        entry['aliases'].append(item['title'])
        # ticker 标识证券，不等同公司，也可能是普通英语词；不自动收录。
    for category in ('territories', 'currencies'):
        localized = {}
        for language in ('en', 'zh'):
            tree = sources[f'{language}-{category}.json']['main'][language]
            localized[language] = tree['localeDisplayNames']['territories'] if category == 'territories' else tree['numbers']['currencies']
        for code, chinese in localized['zh'].items():
            if '-alt-' in code or (category == 'territories' and not (len(code) == 2 and code.isalpha())):
                continue
            if code in ('ZZ', 'XA', 'XB', 'XXX', 'XTS'):
                continue
            english = localized['en'].get(code)
            if english is None:
                raise ValueError(f'缺少英文名称：{code}')
            identity = f'cldr:{category}:{code}'
            if category == 'territories':
                aliases = [name for values in localized.values() for key, name in values.items()
                           if key == code or key.startswith(code + '-alt-')]
                canonical, kind = chinese, 'country'
            else:
                aliases = [name for value in (chinese, english) for key, name in value.items() if key.startswith('displayName')]
                canonical, kind = chinese['displayName'], 'asset'
            entries[identity] = {'id': identity, 'canonical': canonical, 'kind': kind, 'aliases': aliases}
    for override in json.loads((directory / 'overrides.json').read_text()):
        identity = override['id']
        previous = entries.get(identity, {})
        if identity.startswith(('sec:', 'cldr:')) and not previous:
            raise ValueError(f'规范词覆盖了不存在的来源ID：{identity}')
        entries[identity] = {**previous, **override, 'aliases': previous.get('aliases', []) + override['aliases']}
    for entry in entries.values():
        entry['aliases'] = sorted(set([entry['canonical'], *entry['aliases']]))
    return {'version': '2026-11-15.1', 'sources': manifest,
            'entries': sorted(entries.values(), key=lambda item: item['id'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只核对已生成词表')
    args = parser.parse_args()
    content = json.dumps(build(), ensure_ascii=False, separators=(',', ':')) + '\n'
    target = DATA / 'lexicon.json'
    if args.check:
        if target.read_text() != content:
            raise SystemExit('词表与来源/覆盖规则不一致，请重新生成')
    else:
        target.write_text(content)
    print(f'Lexicon verified: {len(json.loads(content)["entries"])} entries')
