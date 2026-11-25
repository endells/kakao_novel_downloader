"""FF 인터페이스를 최소 스텁으로 대체한 라우트/스케줄/템플릿 테스트."""
import importlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, request, session
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader


class Settings:
    def __init__(self):self.values={}
    def get(self,key):return self.values.get(key)
    def set(self,key,value):self.values[key]=value


class Scheduler:
    def __init__(self):self.jobs={}
    def is_include(self,name):return name in self.jobs
    def remove_job(self,name):self.jobs.pop(name,None)
    def add_job_instance(self,job,run=False):self.jobs[job.name]=job


class Base:
    def __init__(self,P,**kwargs):self.P=P
    def get_scheduler_name(self):return 'kakao_novel_basic'


class Job:
    def __init__(self,package,name,interval,fn,description):
        self.name,self.interval,self.fn=name,interval,fn


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Flask(__name__)
        self.app.secret_key='test-session-key'
        self.app.jinja_loader=ChoiceLoader([DictLoader({'base.html':'{% block content %}{% endblock %}'}),
            FileSystemLoader(str(Path(__file__).parents[1]/'templates'))])
        self.framework=types.ModuleType('framework')
        self.framework.F=types.SimpleNamespace(config={'path_data':self.tmp.name},scheduler=Scheduler(),app=self.app)
        self.framework.Job=Job
        plugin=types.ModuleType('plugin');plugin.PluginModuleBase=Base
        self.modules=patch.dict(sys.modules,{'framework':self.framework,'plugin':plugin})
        self.modules.start()
        sys.modules.pop('kakao_novel_downloader.mod_basic',None)
        module=importlib.import_module('kakao_novel_downloader.mod_basic')
        self.P=types.SimpleNamespace(ModelSetting=Settings(),package_name='kakao_novel_downloader',logger=logging.getLogger('test'))
        self.mod=module.ModuleBasic(self.P)

    def tearDown(self):
        self.modules.stop();sys.modules.pop('kakao_novel_downloader.mod_basic',None)
        self.tmp.cleanup()

    def call(self,command,arg1='',token='test-token',valid=True):
        with self.app.test_request_context('/',method='POST',data={'csrf_token':token}):
            session['kakao_novel_csrf']='test-token' if valid else 'other'
            return self.mod.process_command(command,arg1,None,None,request)

    def test_csrf_rejects_missing_or_wrong_token(self):
        result=self.call('status',token='')
        self.assertEqual(403,result[1])

    def test_folder_dialog_uses_existing_parent_without_saving(self):
        existing=Path(self.tmp.name)/'한글 폴더'
        existing.mkdir()
        result=self.call('browse_path',str(existing/'새 폴더'/'소설')).get_json()
        self.assertEqual('success',result['ret'])
        self.assertEqual(str(existing),result['path'])
        self.assertFalse((existing/'새 폴더').exists())
        self.assertNotIn('download_path',self.P.ModelSetting.values)
        self.assertEqual(str(existing),self.call('browse_path',str(existing)).get_json()['path'])
        self.assertEqual('error',self.call('browse_path','relative/path').get_json()['ret'])
        result=self.call('status',valid=False)
        self.assertEqual(403,result[1])

    def test_settings_save_secret_not_in_render_or_response(self):
        payload=dict(download_path=self.tmp.name+'/downloads',basic_auto_start=True,basic_interval=30,
            max_per_title=10,make_epub=True,cookie='[{"name":"_kau","value":"DO-NOT-EXPOSE"}]')
        result=self.call('save',json.dumps(payload)).get_json()
        self.assertEqual('success',result['ret'])
        self.assertNotIn('DO-NOT-EXPOSE',json.dumps(result))
        self.assertEqual(0o600,self.mod.cookie_path.stat().st_mode&0o777)
        self.assertTrue(self.framework.F.scheduler.is_include(self.mod.get_scheduler_name()))
        with self.app.test_request_context('/'):
            rendered=self.mod.process_menu('setting',request)
        self.assertNotIn('DO-NOT-EXPOSE',rendered)
        self.assertNotIn('cookie',self.P.ModelSetting.values)

    def test_schedule_disable_and_unknown_commands(self):
        cfg=dict(download_path=self.tmp.name,basic_auto_start=True)
        self.assertEqual('success',self.call('save',json.dumps(cfg)).get_json()['ret'])
        cfg['basic_auto_start']=False
        self.assertEqual('success',self.call('save',json.dumps(cfg)).get_json()['ret'])
        self.assertFalse(self.framework.F.scheduler.jobs)
        self.assertEqual('error',self.call('unexpected').get_json()['ret'])

    def test_copy_rules_preview_and_background_do_not_require_cookie(self):
        source=Path(self.tmp.name)/'source';source.mkdir()
        (source/'00120_시즌5 304화 예시.txt').write_text('example')
        rule=dict(source_path=str(source),destination_path=self.tmp.name+'/copy')
        preview=self.call('copy_preview',json.dumps(rule)).get_json()
        self.assertEqual(1,preview['counts']['copy'])
        self.assertFalse(Path(rule['destination_path']).exists())
        result=self.call('copy_save',json.dumps(rule)).get_json()
        self.assertEqual('success',result['ret'])
        self.assertFalse(self.mod.cookie_path.exists())
        rule_id=result['rule']['id']
        self.assertEqual('success',self.call('copy_run',str(rule_id)).get_json()['ret'])
        self.mod.engine.thread.join(5)
        self.assertTrue((Path(rule['destination_path'])/'00304_예시.txt').is_file())
        self.assertEqual(1,len(self.call('copy_list').get_json()['rules']))
        self.assertEqual('success',self.call('copy_remove',str(rule_id)).get_json()['ret'])
        self.assertEqual([],self.call('copy_list').get_json()['rules'])

    def test_schedule_logs_disabled_busy_and_missing_cookie(self):
        self.mod.scheduler_function()
        self.P.ModelSetting.set('basic_auto_start', 'True')
        self.mod.engine.active=True
        self.mod.scheduler_function()
        self.mod.engine.active=False
        self.mod.scheduler_function()
        data=self.call('logs','schedule').get_json()
        messages=' '.join(r['message'] for r in data['rows'])
        self.assertIn('자동 확인 꺼짐',messages)
        self.assertIn('다른 작업 진행 중',messages)
        self.assertIn('시작 실패',messages)
        self.assertEqual(3,messages.count('호출됨'))

    def test_scheduler_empty_run_has_start_and_end_logs(self):
        from test_core import FakeClient
        self.P.ModelSetting.set('basic_auto_start', 'True')
        self.mod.engine.client_factory=FakeClient
        with patch.object(self.mod,'read_cookie',return_value='fake'):
            self.mod.scheduler_function()
        self.mod.engine.thread.join(5)
        self.assertFalse(self.mod.engine.active)
        messages=' '.join(r['message'] for r in self.call('logs').get_json()['rows'])
        self.assertIn('[스케줄] 작업 시작',messages)
        self.assertIn('[스케줄] 작업 종료',messages)
        self.assertIn('실행 대상 작품 0개',messages)

    def test_all_templates_render_and_script_parses(self):
        for page in ('works','add','copy','setting','history','logs'):
            with self.app.test_request_context('/'):
                output=self.mod.process_menu(page,request)
            self.assertIn('카카오소설 다운로더',output)
            script=re.search(r'<script>(.*?)</script>',output,re.S)[1]
            path=Path(self.tmp.name)/('test-'+page+'.js')
            path.write_text(script,encoding='utf-8')
            # Node는 CI ubuntu에도 기본 설치되어 있습니다. 브라우저 런타임 검증과는 별개입니다.
            completed=subprocess.run(['node','--check',str(path)],capture_output=True,text=True)
            self.assertEqual(0,completed.returncode,completed.stderr)


class CookieRefreshTests(unittest.TestCase):
    tearDown = AdapterTests.tearDown
    call = AdapterTests.call

    def setUp(self):
        AdapterTests.setUp(self)
        self.cookie = '[{"name":"_kau","value":"old-secret","domain":".kakao.com"}]'
        self.mod.cookie_path.write_text(self.cookie)

    def test_refresh_reloads_attributes_and_never_logs_values(self):
        import requests
        cli = self.mod.make_client(self.cookie)
        cli.session.cookies.set('_kau', 'new-secret', domain='.kakao.com', path='/',
                                secure=True, expires=2100000000)
        cli.persist_cookies()
        cli.close()
        saved = self.mod.read_cookie()
        self.assertIn('new-secret', saved)
        self.assertEqual(0o600, self.mod.cookie_path.stat().st_mode & 0o777)
        next_client = self.mod.make_client(saved)
        c = next(c for c in next_client.session.cookies if c.name == '_kau')
        self.assertEqual(('new-secret', True, 2100000000), (c.value,c.secure,c.expires))
        next_client.close()
        rows = self.call('logs').get_json()['rows']
        self.assertEqual(1, len(rows))
        self.assertNotIn('secret', json.dumps(rows))

    def test_deletion_and_unrelated_domain_not_persisted(self):
        cli = self.mod.make_client(self.cookie)
        cli.session.cookies.clear('.kakao.com', '/', '_kau')
        cli.session.cookies.set('other', 'private', domain='example.com')
        cli.close()
        self.assertEqual([], json.loads(self.mod.read_cookie()))

    def test_manual_replacement_or_removal_not_overwritten(self):
        for replacement in ('new manual cookie', None):
            self.mod.cookie_path.write_text(self.cookie)
            cli = self.mod.make_client(self.cookie)
            cli.session.cookies.set('_kau', 'refreshed', domain='.kakao.com')
            if replacement is None:
                self.mod.cookie_path.unlink()
            else:
                self.mod.cookie_path.write_text(replacement)
            cli.close()
            if replacement is None:
                self.assertFalse(self.mod.cookie_path.exists())
            else:
                self.assertEqual(replacement, self.mod.read_cookie())

    def test_save_failure_preserves_old_cookie_and_retries(self):
        cli = self.mod.make_client(self.cookie)
        cli.session.cookies.set('_kau', 'refreshed', domain='.kakao.com')
        with patch('kakao_novel_downloader.mod_basic.atomic_bytes', side_effect=OSError('secret')):
            cli.persist_cookies()
            self.assertEqual(self.cookie, self.mod.read_cookie())
        cli.close()
        self.assertIn('refreshed', self.mod.read_cookie())
        messages = json.dumps(self.call('logs').get_json(), ensure_ascii=False)
        self.assertIn('자동 저장 실패', messages)
        self.assertNotIn('secret', messages)

    def test_response_error_still_persists_refresh(self):
        from unittest.mock import Mock
        from kakao_novel_downloader.client import AuthError
        cli = self.mod.make_client(self.cookie)
        cli._pace = lambda: None
        def response(*args, **kwargs):
            cli.session.cookies.set('_kau','refreshed',domain='.kakao.com')
            return Mock(status_code=401)
        with patch.object(cli.session,'request',side_effect=response):
            with self.assertRaises(AuthError):
                cli.api('/test')
        self.assertIn('refreshed', self.mod.read_cookie())
        cli.close()

    def test_expiry_secure_and_host_only_roundtrip(self):
        import requests
        cli = self.mod.make_client(self.cookie)
        c = requests.cookies.create_cookie('test', 'value', domain='page.kakao.com',
            path='/api', secure=True, expires=2100000000, rest={'HttpOnly':None,'SameSite':'Lax'})
        c.domain_specified=False
        cli.session.cookies.set_cookie(c)
        cli.session.cookies.set('expired','discard',domain='.kakao.com',expires=1)
        cli.close()
        next_client=self.mod.make_client(self.mod.read_cookie())
        c=next(c for c in next_client.session.cookies if c.name=='test')
        self.assertFalse(c.domain_specified)
        self.assertTrue(c.secure)
        self.assertEqual('/api',c.path)
        self.assertEqual('Lax',c._rest['SameSite'])
        self.assertNotIn('expired',[c.name for c in next_client.session.cookies])
        next_client.close()


if __name__=='__main__':unittest.main()
