"""작품별 순차 다운로드 엔진. 상태와 이용권 사용 의도를 SQLite에 보존합니다."""
import fcntl
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .client import Client, Error, AuthError, SchemaError, TicketUncertain, KST, series_id
from .storage import atomic_bytes, build_epub, digest, safe_name


DEFAULTS = {'download_path': '', 'basic_auto_start': 'False', 'basic_interval': '30',
            'max_per_title': '10', 'make_epub': 'True'}


def now():
    return datetime.now(KST).isoformat(timespec='seconds')


def config(raw):
    result = dict(DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS})
    for key in ('basic_auto_start', 'make_epub'):
        result[key] = result[key] is True or str(result[key]).lower() == 'true'
    for key, low, high in (('basic_interval', 10, 10080), ('max_per_title', 1, 500)):
        try:
            result[key] = int(result[key])
        except (TypeError, ValueError):
            raise Error('%s 항목은 정수로 입력하세요.' % key) from None
        if not low <= result[key] <= high:
            raise Error('%s 범위는 %d~%d입니다.' % (key, low, high))
    path = Path(str(result['download_path'])).expanduser()
    if not path.is_absolute():
        raise Error('FF 컨테이너 안에서 보이는 절대 저장 경로를 입력하세요.')
    result['download_path'] = str(path.resolve())
    return result


class Engine:
    def __init__(self, db_path, client_factory=Client):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.lock_file = None
        self.active = False
        self.state = {'status': 'idle', 'message': '대기 중', 'sid': None, 'episode': '',
                      'completed': 0, 'started': '', 'finished': ''}
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS works(
                    sid INTEGER PRIMARY KEY, title TEXT NOT NULL, start_pid INTEGER NOT NULL,
                    end_pid INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
                    waitfree INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'ready',
                    message TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL, folder TEXT);
                CREATE TABLE IF NOT EXISTS catalog(sid INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS episodes(
                    sid INTEGER NOT NULL, pid INTEGER NOT NULL, number INTEGER NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, path TEXT, sha256 TEXT,
                    ticket_uid TEXT, message TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
                    PRIMARY KEY(sid,pid));
                CREATE TABLE IF NOT EXISTS logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, sid INTEGER,
                    level TEXT NOT NULL, message TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def operation(self):
        # 자동/수동/다중 FF 프로세스가 같은 이용권을 동시에 사용하지 않도록 파일 잠금.
        with self.lock:
            if self.active:
                raise Error('다른 작업이 진행 중입니다. 완료 또는 중지 후 다시 시도하세요.')
            handle = open(str(self.db_path) + '.lock', 'a')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise Error('다른 FF 프로세스에서 작업 중입니다.') from None
            self.active = True
        try:
            yield
        finally:
            with self.lock:
                self.active = False
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def event(self, sid, message, level='info'):
        with self.connect() as db:
            db.execute('INSERT INTO logs(time,sid,level,message) VALUES(?,?,?,?)',
                       (now(), sid, level, message))
            db.execute('DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0)-3000 FROM logs)')

    def set_work(self, sid, status, message):
        with self.connect() as db:
            db.execute('UPDATE works SET status=?,message=?,updated=? WHERE sid=?',
                       (status, message, now(), sid))

    def snapshot(self):
        with self.lock:
            state = dict(self.state, active=self.active)
        with self.connect() as db:
            works = [dict(r) for r in db.execute('SELECT * FROM works ORDER BY title,sid')]
            for work in works:
                work['completed'] = db.execute("SELECT COUNT(*) FROM episodes WHERE sid=? AND status='completed'",
                                               (work['sid'],)).fetchone()[0]
            logs = [dict(r) for r in db.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 100')]
        return dict(state=state, works=works, logs=logs)

    def history(self, sid=None):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT sid,pid,number,title,status,path,message,updated FROM episodes '
                'WHERE (? IS NULL OR sid=?) ORDER BY updated DESC LIMIT 300', (sid, sid))]

    def _catalog(self, cli, sid):
        data = cli.catalog(sid)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO catalog(sid,data) VALUES(?,?)',
                       (sid, json.dumps(data, ensure_ascii=False)))
        return data

    def cached(self, sid):
        with self.connect() as db:
            row = db.execute('SELECT data FROM catalog WHERE sid=?', (sid,)).fetchone()
        if row is None:
            raise Error('먼저 작품 분석을 실행하세요.')
        return json.loads(row['data'])

    def analyze(self, value, cookie):
        sid = series_id(value)
        with self.operation():
            cli = self.client_factory(cookie)
            try:
                data = self._catalog(cli, sid)
                tickets = cli.tickets(sid)
                wf = tickets.get('waitfree') or {}
                data['waitfree_charged'] = wf.get('charged_complete') is True
                return data
            finally:
                cli.close()

    def register(self, sid, start_pid, end_pid=None, waitfree=True):
        with self.operation():
            data = self.cached(int(sid))
            positions = {ep['pid']: i for i, ep in enumerate(data['episodes'])}
            start_pid = int(start_pid)
            end_pid = int(end_pid) if end_pid else None
            if start_pid not in positions or (end_pid and end_pid not in positions):
                raise Error('분석 목록에서 시작/종료 회차를 선택하세요.')
            if end_pid and positions[end_pid] < positions[start_pid]:
                raise Error('종료 회차가 시작 회차보다 앞에 있습니다.')
            with self.connect() as db:
                db.execute('''INSERT INTO works(sid,title,start_pid,end_pid,waitfree,updated)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(sid) DO UPDATE SET
                    title=excluded.title,start_pid=excluded.start_pid,end_pid=excluded.end_pid,
                    waitfree=excluded.waitfree,status='ready',message='',updated=excluded.updated''',
                    (int(sid), data['title'], start_pid, end_pid, int(bool(waitfree)), now()))
        self.event(sid, '작품 등록/범위 변경 — 시작 회차 ID %s' % start_pid)

    def toggle(self, sid, enabled):
        with self.operation(), self.connect() as db:
            db.execute('UPDATE works SET enabled=?,updated=? WHERE sid=?', (int(enabled), now(), int(sid)))

    def remove(self, sid):
        with self.operation(), self.connect() as db:
            db.execute('DELETE FROM works WHERE sid=?', (int(sid),))
        # 파일·다운로드 이력은 유지. 다시 등록해도 중복 수신하지 않음.

    def episode_record(self, sid, pid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM episodes WHERE sid=? AND pid=?', (sid, pid)).fetchone()
        return dict(row) if row else None

    def record(self, sid, ep, status, message='', **fields):
        allowed = {'path', 'sha256', 'ticket_uid'}
        if fields.keys() - allowed:
            raise ValueError('unsupported record field')
        with self.connect() as db:
            db.execute('''INSERT INTO episodes(sid,pid,number,title,status,message,updated)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(sid,pid) DO UPDATE SET number=excluded.number,
                title=excluded.title,status=excluded.status,message=excluded.message,updated=excluded.updated''',
                (sid, ep['pid'], ep['number'], ep['title'], status, message, now()))
            for key, value in fields.items():
                db.execute('UPDATE episodes SET ' + key + '=? WHERE sid=? AND pid=?', (value, sid, ep['pid']))

    def acknowledge(self, sid, pid):
        with self.operation(), self.connect() as db:
            db.execute("UPDATE episodes SET status='retry',message='',updated=? "
                       "WHERE sid=? AND pid=? AND status IN ('ticket_pending','ticket_uncertain','missing')",
                       (now(), int(sid), int(pid)))
        self.event(sid, '사용자가 회차 %s의 대여/파일 상태 확인 후 재시도를 허용했습니다.' % pid, 'warning')

    @staticmethod
    def valid_file(row):
        try:
            return bool(row and row.get('sha256') and row.get('path')
                        and Path(row['path']).is_file() and digest(row['path']) == row['sha256'])
        except OSError:
            return False

    def start(self, cfg, cookie, only_sid=None):
        # 스레드 생성 전에 작업 잠금을 확보해 동시에 들어온 시작 명령도 막음.
        guard = self.operation()
        guard.__enter__()
        try:
            self.stop_event.clear()
            with self.lock:
                self.state.update(status='running', message='작업 시작', completed=0,
                                  started=now(), finished='', sid=None, episode='')
            self.thread = threading.Thread(target=self._background,
                args=(guard, dict(cfg), cookie, only_sid), name='kakao-novel', daemon=True)
            self.thread.start()
        except Exception:
            guard.__exit__(None, None, None)
            raise

    def stop(self):
        self.stop_event.set()

    def _background(self, guard, cfg, cookie, only_sid):
        try:
            self.run(cfg, cookie, only_sid)
        except Exception as exc:
            msg = str(exc) if isinstance(exc, Error) else '내부 오류: ' + type(exc).__name__
            self.event(None, msg, 'error')
            with self.lock:
                self.state.update(status='error', message=msg)
        finally:
            with self.lock:
                if self.state['status'] != 'error':
                    self.state.update(status='stopped' if self.stop_event.is_set() else 'done',
                                      message='중지됨' if self.stop_event.is_set() else '이번 실행 종료 — 작품별 상태를 확인하세요.')
                self.state['finished'] = now()
            guard.__exit__(None, None, None)

    def run(self, cfg, cookie, only_sid=None):
        cli = self.client_factory(cookie, stop=self.stop_event)
        try:
            with self.connect() as db:
                works = [dict(r) for r in db.execute('SELECT * FROM works WHERE '
                    '(? IS NULL AND enabled=1) OR sid=? ORDER BY sid', (only_sid, only_sid))]
            for work in works:
                if self.stop_event.is_set():
                    break
                sid = work['sid']
                with self.lock:
                    self.state.update(sid=sid, episode='', message=work['title'] + ' 확인 중')
                try:
                    self.process_work(cli, work, cfg)
                except AuthError:
                    self.set_work(sid, 'auth', '쿠키 갱신이 필요합니다.')
                    raise
                except (Error, OSError) as exc:
                    msg = str(exc) if isinstance(exc, Error) else '저장 경로 오류: 권한과 디스크 공간을 확인하세요.'
                    self.set_work(sid, 'error', msg)
                    self.event(sid, msg, 'error')
        finally:
            cli.close()

    def process_work(self, cli, work, cfg):
        sid = work['sid']
        catalog = self._catalog(cli, sid)
        eps = catalog['episodes']
        pids = [ep['pid'] for ep in eps]
        if work['start_pid'] not in pids or (work['end_pid'] and work['end_pid'] not in pids):
            raise Error('설정한 시작/종료 회차가 목록에서 사라졌습니다. 범위를 다시 지정하세요.')
        start = pids.index(work['start_pid'])
        end = pids.index(work['end_pid']) + 1 if work['end_pid'] else len(pids)
        if start >= end:
            raise Error('설정된 회차 순서가 바뀌었습니다. 범위를 다시 확인하세요.')
        root = Path(cfg['download_path']).resolve()
        # 제목 변경에도 동일 작품 폴더 유지. 저장 경로를 바꾸면 새 루트에 저장.
        folder_name = work['folder'] or (safe_name(catalog['title']) + ' [%s]' % sid)
        folder = root / folder_name
        if folder.resolve().parent != root:
            raise Error('작품 폴더가 저장 경로 밖을 가리킵니다.')
        folder.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('UPDATE works SET title=?,folder=? WHERE sid=?', (catalog['title'], folder_name, sid))
        # 실제 저장 쓰기 가능 여부를 이용권 사용 전에 확인.
        atomic_bytes(folder / 'info.json', json.dumps(dict(series_id=sid, title=catalog['title'],
            source='https://page.kakao.com/content/%s' % sid), ensure_ascii=False, indent=2).encode('utf-8'))
        count, tickets_used = 0, 0
        self.set_work(sid, 'running', '시작 회차부터 순차 처리 중')
        for ep in eps[start:end]:
            if self.stop_event.is_set():
                self.set_work(sid, 'paused', '중지 요청 — 다음 실행에서 이어받습니다.')
                break
            row = self.episode_record(sid, ep['pid'])
            if row and row['status'] == 'completed' and self.valid_file(row):
                # 새 회차 삽입으로 목록 순번이 달라져도 EPUB은 현재 공식 순서로 정렬.
                self.record(sid, ep, 'completed')
                continue
            if count >= cfg['max_per_title']:
                self.set_work(sid, 'limit', '작품당 실행 한도 도달 — 다음 실행에서 이어받습니다.')
                break
            with self.lock:
                self.state['episode'] = ep['title']
            if ep['access'] == 'locked':
                ready = cli.ready(ep['pid'])
                already = (ready.get('single') or {}).get('is_done') is True
                if not already:
                    if row and row['status'] in ('ticket_pending', 'ticket_uncertain', 'rented', 'completed', 'missing'):
                        status = 'missing' if row['status'] in ('completed', 'missing') else 'ticket_uncertain'
                        self.record(sid, ep, status, '대여 또는 파일 상태를 확인하세요. 이용권을 다시 사용하지 않았습니다.')
                        self.set_work(sid, 'attention', '회차 확인 필요: ' + ep['title'])
                        break
                    if not work['waitfree']:
                        self.set_work(sid, 'locked', '잠금 회차에서 대기 중 (기다무 사용 꺼짐): ' + ep['title'])
                        break
                    if tickets_used >= 1:
                        self.set_work(sid, 'waiting', '이번 실행 기다무 1장 사용 완료 — 충전 후 이어받습니다.')
                        break
                    ticket = cli.tickets(sid)
                    charged = (ticket.get('waitfree') or {}).get('charged_complete')
                    if charged is not True:
                        self.set_work(sid, 'waiting', '기다무 충전 대기 — 다음 점검에서 다시 확인합니다.')
                        break
                    rental_type = (ready.get('available') or {}).get('ticket_rental_type')
                    if rental_type != 'RT05':
                        self.set_work(sid, 'ineligible', '이 회차의 기다무 사용 가능 여부가 확인되지 않습니다: ' + ep['title'])
                        break
                    self.record(sid, ep, 'ticket_pending', '기다무 사용 요청 기록')
                    try:
                        used = cli.rent_waitfree(ep['pid'])
                    except TicketUncertain as exc:
                        self.record(sid, ep, 'ticket_uncertain', str(exc))
                        self.set_work(sid, 'attention', str(exc))
                        break
                    tickets_used += 1
                    uid = used.get('ticket_uid')
                    if not uid:
                        self.record(sid, ep, 'ticket_uncertain', '기다무 응답에 대여 확인값이 없습니다.')
                        self.set_work(sid, 'attention', '대여 내역 확인 필요')
                        break
                    self.record(sid, ep, 'rented', ticket_uid=str(uid))
                    self.event(sid, '기다무 사용: ' + ep['title'])
            # 완료/대여 기록의 파일이 없더라도 접근권이 있으면 이용권 없이 복구 가능.
            row = self.episode_record(sid, ep['pid'])
            try:
                if row and row.get('ticket_uid') and row['status'] == 'rented':
                    try:
                        cli.open_rental(sid, ep['pid'], row['ticket_uid'])
                    except SchemaError:
                        # 열람 알림의 응답 형식만 불명확한 경우 본문 API로 접근권 확인.
                        # 인증 실패/명시적 거부/통신 오류는 그대로 중단한다.
                        self.event(sid, '대여 후 열람 응답 형식 확인 불가 (open_page) — 본문 접근 확인: ' + ep['title'], 'warning')
                text = cli.text(sid, ep['pid'])
                # PID 포함으로 동명 외전·제목 변경 충돌 방지.
                path = folder / ('%05d_%s [%s].txt' % (ep['number'], safe_name(ep['title']), ep['pid']))
                if row and row.get('path') and Path(row['path']).resolve().parent == folder.resolve():
                    path = Path(row['path'])
                atomic_bytes(path, text.encode('utf-8'))
                self.record(sid, ep, 'completed', path=str(path), sha256=digest(path))
            except (Error, OSError):
                # 사용된 대여권 기록은 유지해야 재시도 때 두 번 차감하지 않음.
                status = row['status'] if row and row['status'] in ('rented','ticket_pending','ticket_uncertain') else 'failed'
                self.record(sid, ep, status, '본문 저장 실패 — 다음 실행에서 접근권을 재확인합니다.')
                raise
            count += 1
            with self.lock:
                self.state['completed'] += 1
            self.event(sid, '저장 완료: ' + ep['title'])
        else:
            self.set_work(sid, 'caught_up', '지정 범위 수집 완료' if work['end_pid'] else '현재 최신 회차까지 완료 — 새 회차를 계속 확인합니다.')
        if cfg['make_epub']:
            self.export(sid, folder, catalog['title'])

    def export(self, sid, folder=None, title=None):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM episodes WHERE sid=? AND status='completed'", (sid,))]
            work = db.execute('SELECT * FROM works WHERE sid=?', (sid,)).fetchone()
        valid = [r for r in rows if self.valid_file(r)]
        if not valid:
            return None
        folder = folder or Path(valid[0]['path']).parent
        title = title or (work['title'] if work else str(sid))
        return build_epub(folder, title, sid, valid)
