"""원본을 보존하는 시즌별 TXT 복사. 미리보기와 실행은 같은 계획 함수를 사용한다."""
import json
import os
import re
import shutil
import string
import tempfile
from collections import Counter
from pathlib import Path

from .client import Error
from .storage import digest, safe_name

FIELDS = {'title', 'season', 'episode', 'subtitle', 'original_number', 'original_name', 'pid'}
NUMBERS = {'season', 'episode', 'original_number', 'pid'}
SEASON = re.compile(r'시즌\s*(\d+)', re.I)
EPISODE = re.compile(r'(\d+)\s*화(?:\s|[._:：\-]|$)')
FORMATTER = string.Formatter()
DEFAULT_RULE = dict(sid=56505778, title='천재 궁수의 스트리밍', season=5,
    source_path='', destination_path='', folder_template='',
    filename_template='{episode:05d}_{subtitle}.txt', mode='title',
    anchor_source=1, anchor_episode=1, start_number=0, end_number=0,
    enabled=False, match_existing=True)


def validate(raw):
    rule = dict(DEFAULT_RULE, **{k: v for k, v in raw.items() if k in DEFAULT_RULE})
    for key in ('sid', 'season', 'anchor_source', 'anchor_episode', 'start_number', 'end_number'):
        try:
            rule[key] = int(rule[key])
        except (ValueError, TypeError):
            raise Error('번호 항목은 정수로 입력하세요.') from None
        if not 0 <= rule[key] <= 10**15:
            raise Error('번호 범위를 확인하세요.')
    if not rule['sid'] or not rule['season'] or not rule['anchor_episode']:
        raise Error('작품 번호, 시즌, 기준 시즌 회차는 1 이상이어야 합니다.')
    if rule['end_number'] and rule['end_number'] < rule['start_number']:
        raise Error('원본 번호 범위의 끝이 시작보다 작습니다.')
    if rule['mode'] not in ('title', 'offset'):
        raise Error('회차 계산 방식을 확인하세요.')
    for key in ('enabled', 'match_existing'):
        rule[key] = rule[key] is True
    for key in ('title', 'source_path', 'destination_path', 'folder_template', 'filename_template'):
        rule[key] = str(rule[key]).strip()
    if not rule['title'] or not rule['destination_path']:
        raise Error('작품명과 복사 대상 폴더를 입력하세요.')
    for key in ('source_path', 'destination_path'):
        if rule[key]:
            p = Path(rule[key]).expanduser()
            if not p.is_absolute():
                raise Error('폴더는 FF 컨테이너 내부 절대 경로로 입력하세요.')
            rule[key] = str(p.resolve())
    for key in ('folder_template', 'filename_template'):
        template = rule[key]
        if len(template) > 500 or '\\' in template or template.startswith('/'):
            raise Error('이름 규칙의 길이 또는 경로를 확인하세요.')
        if key == 'filename_template' and ('/' in template or not template.lower().endswith('.txt')):
            raise Error('파일명 규칙은 폴더 구분 없이 .txt로 끝나야 합니다.')
        if any(p in ('.', '..') for p in template.split('/')):
            raise Error('상위 폴더로 이동하는 규칙은 사용할 수 없습니다.')
        try:
            parts = list(FORMATTER.parse(template))
        except ValueError:
            raise Error('이름 규칙의 중괄호를 확인하세요.') from None
        for _, field, spec, conversion in parts:
            if field is not None and (field not in FIELDS or conversion or
                (spec and (field not in NUMBERS or not re.fullmatch(r'0?[1-8]d|d', spec)))):
                raise Error('지원하지 않는 이름 변수 또는 자릿수 형식입니다.')
    return rule


def metadata(row):
    stem = Path(row['path']).stem
    prefix = re.match(r'^(\d+)[_ .-]+', stem)
    title = row.get('title') or stem
    if not row.get('title'):
        title = re.sub(r'\s*\[\d{6,}\]$', '', title)
        if prefix:
            title = title[prefix.end():]
    season = SEASON.search(title)
    episode = EPISODE.search(title, season.end() if season else 0)
    subtitle = title[episode.end():].strip(' _.-:：') if episode else title
    return dict(season=int(season[1]) if season else None,
        episode=int(episode[1]) if episode else None, subtitle=subtitle or title,
        original_number=int(row.get('number') or (prefix[1] if prefix else 0)),
        original_name=stem, pid=int(row.get('pid') or 0))


def render(template, values):
    # 값 안의 경로 문자는 구성 요소 안에서 치환한다.
    safe = {k: v if isinstance(v, int) else safe_name(v) for k, v in values.items()}
    return '/'.join(re.sub(r'[\\:*?"<>|\x00-\x1f]', '_', part.format_map(safe)).strip(' .') or 'untitled'
                    for part in template.split('/')) if template else ''


def destination(rule, values):
    root = Path(rule['destination_path'])
    target = root / render(rule['folder_template'], values) / render(rule['filename_template'], values)
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise Error('복사 대상이 지정 폴더 밖을 가리키거나 심볼릭 링크입니다.')
    if any(len(part.encode('utf-8')) > 240 for part in target.relative_to(root).parts):
        raise Error('폴더 또는 파일명이 너무 깁니다. 규칙을 짧게 바꾸세요.')
    return target


def existing_number(name, rule):
    # 사용자 규칙으로 생성한 이름을 먼저 읽고, 일반적인 기존 파일명도 인식한다.
    pattern = ''
    captured = set()
    for literal, field, spec, _ in FORMATTER.parse(rule['filename_template']):
        pattern += re.escape(literal)
        if field:
            if field in NUMBERS and field not in captured:
                pattern += '(?P<%s>\\d+)' % field
                captured.add(field)
            else:
                pattern += '.+?'
    match = re.fullmatch(pattern, name)
    if match and 'episode' in captured:
        groups = match.groupdict()
        if groups.get('season') and int(groups['season']) != rule['season']:
            return None
        return int(groups['episode'])
    season = SEASON.search(name)
    if season and int(season[1]) != rule['season']:
        return None
    episode = EPISODE.search(name, season.end() if season else 0)
    if episode:
        return int(episode[1])
    leading = re.match(r'^(\d+)(?:[_ .-]|화)', name)
    return int(leading[1]) if leading else None


def copy_exclusive(source, target, expected):
    """완성된 임시 파일을 하드링크로 공개. 목적 파일이 생겼으면 절대 덮어쓰지 않음."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.kakao-copy-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as out, open(source, 'rb') as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        if digest(temp) != expected:
            raise Error('복사 도중 원본이 변경되었습니다. 다음 실행에서 재확인하세요.')
        os.chmod(temp, 0o644)
        os.link(temp, target)  # 같은 폴더의 파일시스템에서 원자적 생성, 기존 파일 보호
    finally:
        os.unlink(temp)


class Copier:
    def __init__(self, engine):
        self.engine = engine
        with engine.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS copy_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)')

    def rules(self):
        with self.engine.connect() as db:
            return [dict(json.loads(r['data']), id=r['id']) for r in db.execute('SELECT * FROM copy_rules ORDER BY id')]

    def get(self, rule_id):
        return next((r for r in self.rules() if r['id'] == int(rule_id)), None)

    def save(self, raw):
        rule = validate(raw)
        with self.engine.connect() as db:
            if raw.get('id'):
                rule_id = int(raw['id'])
                if not db.execute('SELECT 1 FROM copy_rules WHERE id=?', (rule_id,)).fetchone():
                    raise Error('복사 규칙이 없습니다.')
                db.execute('UPDATE copy_rules SET data=? WHERE id=?', (json.dumps(rule, ensure_ascii=False), rule_id))
            else:
                rule_id = db.execute('INSERT INTO copy_rules(data) VALUES(?)', (json.dumps(rule, ensure_ascii=False),)).lastrowid
        return dict(rule, id=rule_id)

    def remove(self, rule_id):
        with self.engine.connect() as db:
            db.execute('DELETE FROM copy_rules WHERE id=?', (int(rule_id),))

    def sources(self, rule):
        if rule['source_path']:
            folder = Path(rule['source_path'])
            if not folder.is_dir():
                raise Error('기존 파일 폴더를 찾을 수 없습니다.')
            rows = [dict(path=str(p)) for p in sorted(folder.iterdir())
                    if p.suffix.lower() == '.txt' and p.is_file() and not p.is_symlink()]
        else:
            with self.engine.connect() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM episodes WHERE sid=? AND status='completed' ORDER BY number,pid", (rule['sid'],))]
        if len(rows) > 10000:
            raise Error('한 번에 최대 10,000개 TXT를 처리합니다. 원본 폴더를 나누세요.')
        return rows

    def plan(self, raw):
        rule = validate(raw)
        entries, seen_sources = [], set()
        for row in self.sources(rule):
            source = Path(row['path'])
            if str(source.resolve()) in seen_sources:
                continue
            seen_sources.add(str(source.resolve()))
            entry = dict(source=str(source), target='', episode=None, status='skip', message='')
            entries.append(entry)
            data = metadata(row)
            number = data['original_number']
            if data['season'] is not None and data['season'] != rule['season']:
                entry['message'] = '다른 시즌'; continue
            if (rule['start_number'] and number < rule['start_number']) or (rule['end_number'] and number > rule['end_number']):
                entry['message'] = '원본 번호 범위 밖'; continue
            if rule['mode'] == 'title':
                episode = data['episode']
            else:
                episode = number - rule['anchor_source'] + rule['anchor_episode'] if number else None
            if episode is None or episode < 1:
                entry.update(status='review', message='시즌 회차 인식 불가 — 번호 보정 방식을 확인하세요.'); continue
            values = dict(data, title=rule['title'], season=rule['season'], episode=episode)
            try:
                target = destination(rule, values)
                entry.update(target=str(target), episode=episode)
                if source.is_symlink() or not source.is_file():
                    raise Error('원본이 없거나 심볼릭 링크입니다.')
                if source.resolve() == target.resolve():
                    raise Error('원본과 복사 대상이 같습니다.')
                if target.parent.resolve() == source.parent.resolve():
                    raise Error('기존 파일 폴더와 복사 대상 폴더는 다르게 지정하세요.')
                source_hash = digest(source)
                if row.get('sha256') and source_hash != row['sha256']:
                    raise Error('다운로드 이력과 원본 내용이 다릅니다.')
                entry.update(status='copy', message='복사 예정', sha256=source_hash)
            except (Error, OSError) as exc:
                entry.update(status='review', message=str(exc) if isinstance(exc, Error) else '파일 읽기 권한을 확인하세요.')
        # 같은 대상/회차로 매핑되는 원본을 모두 보류하여 잘못된 한 편이 먼저 저장되지 않게 한다.
        counts = Counter((e['target'],) for e in entries if e['status'] == 'copy')
        episodes = Counter((str(Path(e['target']).parent), e['episode']) for e in entries if e['status'] == 'copy')
        directories = {}
        for entry in entries:
            if entry['status'] != 'copy': continue
            target = Path(entry['target'])
            if counts[(entry['target'],)] > 1 or episodes[(str(target.parent), entry['episode'])] > 1:
                entry.update(status='review', message='여러 원본이 같은 파일명/회차로 변환됩니다.'); continue
            if target.parent not in directories:
                directories[target.parent] = [p for p in target.parent.glob('*') if p.is_file() and p.suffix.lower() == '.txt']
            candidates = [p for p in directories[target.parent] if p.name == target.name or
                          (rule['match_existing'] and existing_number(p.name, rule) == entry['episode'])]
            if target.exists() and target not in candidates:
                candidates.append(target)
            if candidates:
                try:
                    same = len(candidates) == 1 and candidates[0].is_file() and not candidates[0].is_symlink() and digest(candidates[0]) == entry['sha256']
                except OSError:
                    same = False
                entry.update(status='exists' if same else 'conflict', message=('같은 내용이 이미 있음: ' if same else '기존 파일 보호 — 확인 필요: ') + ', '.join(p.name for p in candidates))
        return entries

    def preview(self, raw):
        entries = self.plan(raw)
        folders = {Path(e['target']).parent for e in entries if e['target']}
        if not folders:
            folders = {Path(validate(raw)['destination_path'])}
        examples = []
        for folder in sorted(folders):
            for path in sorted(folder.glob('*.txt')):
                examples.append(str(path))
                if len(examples) >= 20:
                    break
            if len(examples) >= 20:
                break
        return dict(rows=entries[:300], total=len(entries), counts=dict(Counter(e['status'] for e in entries)),
                    truncated=len(entries)>300, existing_examples=examples)

    def execute(self, raw):
        entries = self.plan(raw)
        for entry in entries:
            if self.engine.stop_event.is_set():
                break
            if entry['status'] != 'copy': continue
            try:
                # 대상 심볼릭 링크/경로를 직전에 다시 확인. 기존 파일은 link의 O_EXCL 성질로 보호.
                target = Path(entry['target'])
                if target.is_symlink() or not target.resolve().is_relative_to(Path(raw['destination_path']).resolve()):
                    raise Error('복사 대상 경로가 변경되었습니다.')
                copy_exclusive(entry['source'], target, entry['sha256'])
                entry.update(status='copied', message='복사 완료')
            except FileExistsError:
                entry.update(status='conflict', message='실행 중 같은 이름의 파일이 생겨 건너뜁니다.')
            except (Error, OSError) as exc:
                entry.update(status='review', message=str(exc) if isinstance(exc, Error) else '복사 실패 — 대상 폴더 권한/공간/하드링크 지원을 확인하세요.')
        counts = dict(Counter(e['status'] for e in entries))
        problems = [e for e in entries if e['status'] in ('review','conflict')]
        self.engine.event(raw['sid'], '파일 복사: 완료 %s · 기존 %s · 충돌/확인 %s · 미처리 %s' %
            (counts.get('copied',0), counts.get('exists',0),len(problems),counts.get('copy',0)), 'warning' if problems else 'info')
        for entry in problems[:20]:
            self.engine.event(raw['sid'], Path(entry['source']).name + ': ' + entry['message'], 'warning')
        return dict(counts=counts, total=len(entries))

    def automatic(self, sid):
        for rule in self.rules():
            if rule['sid'] != sid or not rule['enabled'] or self.engine.stop_event.is_set(): continue
            try:
                self.execute(rule)
            except (Error, OSError) as exc:
                self.engine.event(sid, '자동 복사 실패: ' + (str(exc) if isinstance(exc, Error) else '폴더 접근 권한을 확인하세요.'), 'warning')
