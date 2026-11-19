import json
import os
import secrets
from pathlib import Path

from flask import jsonify, render_template, session
from framework import F, Job
from plugin import PluginModuleBase

from .client import Error, Client, parse_cookies, series_id
from .core import DEFAULTS, Engine, config
from .storage import atomic_bytes


class ModuleBasic(PluginModuleBase):
    db_default = dict(DEFAULTS, db_version='1')

    def __init__(self, P):
        super().__init__(P, name='basic', first_menu='works', scheduler_desc='카카오소설 기다무 순차 다운로드')
        self.engine = Engine(Path(F.config['path_data']) / 'db' / 'kakao_novel_downloader.db')
        self.cookie_path = self.engine.db_path.parent / 'kakao_novel_downloader_cookie.json'

    def raw_config(self):
        values = {k: self.P.ModelSetting.get(k) or v for k, v in DEFAULTS.items()}
        values['download_path'] = values['download_path'] or str(Path(F.config['path_data']) / 'downloads' / 'kakao_novel')
        return values

    def read_cookie(self):
        try:
            return self.cookie_path.read_text(encoding='utf-8')
        except OSError:
            raise Error('설정에서 로그인 쿠키를 먼저 저장하세요.') from None

    def sync_schedule(self, cfg):
        name = self.get_scheduler_name()
        if F.scheduler.is_include(name):
            F.scheduler.remove_job(name)
        if cfg['basic_auto_start']:
            job = Job(self.P.package_name, name, str(cfg['basic_interval']),
                      self.scheduler_function, '카카오소설 기다무 순차 다운로드')
            F.scheduler.add_job_instance(job, run=False)

    def plugin_load(self):
        try:
            self.sync_schedule(config(self.raw_config()))
        except Exception as exc:
            self.P.logger.warning('카카오소설 스케줄 등록 실패: %s', type(exc).__name__)

    def plugin_unload(self):
        self.engine.stop()
        if F.scheduler.is_include(self.get_scheduler_name()):
            F.scheduler.remove_job(self.get_scheduler_name())

    def scheduler_function(self):
        with F.app.app_context():
            try:
                cfg = config(self.raw_config())
                if cfg['basic_auto_start']:
                    self.engine.start(cfg, self.read_cookie())
            except Error as exc:
                self.engine.event(None, str(exc), 'warning')

    def process_menu(self, sub, req):
        if sub not in ('works', 'add', 'copy', 'setting', 'history'):
            sub = 'works'
        if not session.get('kakao_novel_csrf'):
            session['kakao_novel_csrf'] = secrets.token_urlsafe(32)
        return render_template('kakao_novel_downloader_basic.html', page=sub,
            package_name=self.P.package_name, cfg=self.raw_config(),
            csrf=session['kakao_novel_csrf'], cookie_saved=self.cookie_path.is_file())

    def process_command(self, command, arg1=None, arg2=None, arg3=None, req=None):
        expected = session.get('kakao_novel_csrf', '')
        given = req.form.get('csrf_token', '') if req is not None else ''
        if req is None or req.method != 'POST' or not expected or not secrets.compare_digest(expected, given):
            return jsonify(ret='error', msg='페이지를 새로고침한 후 다시 시도하세요.'), 403
        try:
            if command == 'browse_path':
                path = Path(os.path.normpath(str(arg1 or '/').strip() or '/'))
                if not path.is_absolute():
                    raise Error('폴더 선택은 /로 시작하는 컨테이너 내부 경로를 입력하세요.')
                # 아직 생성되지 않은 저장 폴더는 접근 가능한 상위 폴더에서 탐색합니다.
                while not (path.is_dir() and os.access(str(path), os.R_OK | os.X_OK)):
                    if path == path.parent:
                        raise Error('열 수 있는 상위 폴더가 없습니다. FF 폴더 접근 권한을 확인하세요.')
                    path = path.parent
                return jsonify(ret='success', path=str(path))
            if command == 'copy_list':
                return jsonify(ret='success', rules=self.engine.copier.rules())
            if command == 'copy_save':
                with self.engine.operation():
                    rule = self.engine.copier.save(json.loads(arg1 or '{}'))
                return jsonify(ret='success', rule=rule, msg='복사 규칙을 저장했습니다.')
            if command == 'copy_preview':
                with self.engine.operation():
                    result = self.engine.copier.preview(json.loads(arg1 or '{}'))
                return jsonify(ret='success', **result)
            if command == 'copy_run':
                self.engine.start_copy(int(arg1))
                return jsonify(ret='success', msg='기존 TXT 복사를 시작했습니다. 아래 진행 상태를 확인하세요.')
            if command == 'copy_remove':
                with self.engine.operation():
                    self.engine.copier.remove(int(arg1))
                return jsonify(ret='success', msg='복사 규칙만 삭제했습니다. 원본과 복사본은 유지됩니다.')
            if command == 'status':
                return jsonify(ret='success', **self.engine.snapshot(),
                               scheduled=F.scheduler.is_include(self.get_scheduler_name()))
            if command == 'history':
                return jsonify(ret='success', rows=self.engine.history(int(arg1) if arg1 else None))
            if command == 'analyze':
                return jsonify(ret='success', data=self.engine.analyze(arg1, self.read_cookie()))
            if command == 'cached':
                return jsonify(ret='success', data=self.engine.cached(int(arg1)))
            if command == 'register':
                raw = json.loads(arg1 or '{}')
                self.engine.register(int(raw['sid']), int(raw['start_pid']), raw.get('end_pid'),
                                     raw.get('waitfree') is True)
                return jsonify(ret='success', msg='작품을 저장했습니다. 내 작품에서 실행하세요.')
            if command == 'run':
                self.engine.start(config(self.raw_config()), self.read_cookie(), int(arg1) if arg1 else None)
                return jsonify(ret='success', msg='다운로드를 시작했습니다.')
            if command == 'stop':
                self.engine.stop()
                return jsonify(ret='success', msg='중지를 요청했습니다. 진행 중인 네트워크 응답을 기다릴 수 있습니다.')
            if command == 'toggle':
                self.engine.toggle(int(arg1), arg2 == '1')
                return jsonify(ret='success', msg='자동 처리 대상 설정을 변경했습니다.')
            if command == 'remove':
                self.engine.remove(int(arg1))
                return jsonify(ret='success', msg='작품 목록에서 해제했습니다. 파일과 이력은 유지됩니다.')
            if command == 'acknowledge':
                self.engine.acknowledge(int(arg1), int(arg2))
                return jsonify(ret='success', msg='다음 실행에서 재확인합니다.')
            if command == 'epub':
                with self.engine.operation():
                    path = self.engine.export(int(arg1))
                return jsonify(ret='success', msg='EPUB 저장: ' + path if path else '검증된 TXT 파일이 없습니다.')
            if command == 'verify':
                with self.engine.operation():
                    cli = Client(self.read_cookie())
                    try:
                        cli.tickets(series_id(arg1))
                    finally:
                        cli.close()
                return jsonify(ret='success', msg='해당 작품의 로그인 이용권 조회에 성공했습니다.')
            if command == 'save':
                raw = json.loads(arg1 or '{}')
                cfg = config(raw)
                cookie = str(raw.get('cookie') or '').strip()
                if len(cookie) > 200000:
                    raise Error('쿠키 입력이 너무 큽니다.')
                clean = json.dumps(parse_cookies(cookie), ensure_ascii=False) if cookie else None
                with self.engine.operation():
                    if raw.get('clear_cookie') is True:
                        if self.cookie_path.exists():
                            self.cookie_path.unlink()
                    elif clean:
                        atomic_bytes(self.cookie_path, clean.encode('utf-8'), 0o600)
                    for key, value in cfg.items():
                        self.P.ModelSetting.set(key, str(value))
                    self.sync_schedule(cfg)
                return jsonify(ret='success', msg='설정과 스케줄을 저장했습니다.', cookie_saved=self.cookie_path.is_file())
            raise Error('지원하지 않는 명령입니다.')
        except Error as exc:
            return jsonify(ret='error', msg=str(exc))
        except (TypeError, KeyError, ValueError):
            return jsonify(ret='error', msg='입력 형식이 올바르지 않습니다.')
        except Exception as exc:
            # 쿠키, API 본문, 서명 URL이 예외 문자열에 들어갈 수 있어 종류만 기록.
            self.P.logger.error('카카오소설 명령 처리 실패 [%s]: %s', command, type(exc).__name__)
            return jsonify(ret='error', msg='처리 실패: ' + type(exc).__name__ + '. 저장 경로와 FF 로그를 확인하세요.')
