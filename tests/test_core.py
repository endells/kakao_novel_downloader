import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

from kakao_novel_downloader.client import Error, AuthError, SchemaError, TicketUncertain, normalize_episodes, extract_paragraphs, parse_cookies, series_id
from kakao_novel_downloader.core import Engine, config
from kakao_novel_downloader.storage import atomic_bytes


class FakeClient:
    def __init__(self, *_args, **_kwargs):
        self.eps = [dict(pid=i, number=i, cursor=100-i, title='회차 %s' % i, access='locked') for i in range(1, 5)]
        self.charged = True
        self.ticket_calls = []
        self.text_calls = []
        self.open_calls = []
        self.fail_text = set()
        self.uncertain = False
        self.rental_type = 'RT05'

    def catalog(self, sid):
        return dict(sid=sid, title='테스트 소설', meta={'category':'소설'}, episodes=copy.deepcopy(self.eps))

    def tickets(self, sid):
        return {'waitfree': {'charged_complete': self.charged}}

    def ready(self, pid):
        return {'single': {'is_done': self.eps[pid-1]['access'] != 'locked'},
                'available': {'ticket_rental_type': self.rental_type}}

    def rent_waitfree(self, pid):
        self.ticket_calls.append(pid)
        if self.uncertain:
            raise TicketUncertain('대여 결과 불확실')
        self.eps[pid-1]['access'] = 'rented'
        self.charged = False
        return {'ticket_uid': 'private-ticket-' + str(pid)}

    def open_rental(self, sid, pid, uid):
        self.open_calls.append(pid)

    def text(self, sid, pid):
        self.text_calls.append(pid)
        if pid in self.fail_text:
            raise Error('본문 일부 전송 실패')
        return '첫 문단 %s\n\n두 번째 & <문단>' % pid

    def close(self):
        pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = FakeClient()
        self.engine = Engine(self.root / 'db.sqlite', lambda *a, **kw: self.client)
        self.cfg = config({'download_path': str(self.root / 'output')})
        self.engine.analyze('123', 'fake')

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, start=2, end=None):
        self.engine.register(123, start, end, True)

    def run_once(self):
        with self.engine.operation():
            self.engine.run(self.cfg, 'fake')

    def work(self):
        return self.engine.snapshot()['works'][0]

    def test_start_inclusive_then_resume_next_after_recharge(self):
        self.register(2)
        self.run_once()
        self.assertEqual([2], self.client.ticket_calls)
        self.assertEqual([2], self.client.text_calls)
        self.assertEqual([2], self.client.open_calls)
        self.client.charged = True
        self.run_once()
        self.assertEqual([2, 3], self.client.ticket_calls)
        self.assertEqual([2, 3], self.client.text_calls)

    def test_waiting_does_not_skip_to_later_free_episode(self):
        self.register()
        self.client.charged = False
        self.client.eps[3]['access'] = 'free'
        self.run_once()
        self.assertEqual([], self.client.ticket_calls)
        self.assertEqual([], self.client.text_calls)
        self.assertEqual('waiting', self.work()['status'])

    def test_other_ticket_type_never_used(self):
        self.register()
        self.client.rental_type = 'RT01'
        self.run_once()
        self.assertEqual([], self.client.ticket_calls)
        self.assertEqual('ineligible', self.work()['status'])

    def test_waitfree_disabled_stops_at_locked(self):
        self.engine.register(123, 2, None, False)
        self.run_once()
        self.assertFalse(self.client.ticket_calls)
        self.assertEqual('locked', self.work()['status'])

    def test_known_free_and_owned_do_not_use_tickets(self):
        self.register(1, 2)
        self.client.eps[0]['access'] = 'free'
        self.client.eps[1]['access'] = 'owned'
        self.run_once()
        self.assertEqual([1, 2], self.client.text_calls)
        self.assertEqual([], self.client.ticket_calls)
        self.assertEqual('caught_up', self.work()['status'])

    def test_uncertain_ticket_persists_across_restart(self):
        self.register()
        self.client.uncertain = True
        self.run_once()
        self.engine = Engine(self.root / 'db.sqlite', lambda *a, **kw: self.client)
        self.client.uncertain = False
        self.run_once()
        self.assertEqual([2], self.client.ticket_calls)
        self.assertEqual('attention', self.work()['status'])

    def test_uncertain_but_now_rented_recovers_without_second_ticket(self):
        self.register()
        self.client.uncertain = True
        self.run_once()
        self.client.eps[1]['access'] = 'rented'
        self.client.charged = False
        self.run_once()
        self.assertEqual([2], self.client.ticket_calls)
        self.assertEqual('completed', self.engine.episode_record(123, 2)['status'])

    def test_partial_text_failure_never_marked_completed_or_recharged(self):
        self.register()
        self.client.fail_text.add(2)
        self.run_once()
        self.assertEqual('rented', self.engine.episode_record(123, 2)['status'])
        self.assertEqual([], list((self.root / 'output').rglob('*.txt')))
        self.client.fail_text.clear()
        self.run_once()
        self.assertEqual([2], self.client.ticket_calls)
        self.assertEqual('completed', self.engine.episode_record(123, 2)['status'])

    def test_open_schema_failure_fetches_text_without_second_ticket(self):
        self.register(2, 2)
        self.client.open_rental = Mock(side_effect=SchemaError('unexpected envelope'))
        self.client.fail_text.add(2)
        self.run_once()
        self.assertEqual('rented', self.engine.episode_record(123, 2)['status'])
        self.assertEqual([], list((self.root / 'output').rglob('*.txt')))
        self.engine = Engine(self.root / 'db.sqlite', lambda *a, **kw: self.client)
        self.client.fail_text.clear()
        self.run_once()
        self.assertEqual([2], self.client.ticket_calls)
        self.assertEqual('completed', self.engine.episode_record(123, 2)['status'])
        self.assertEqual('caught_up', self.work()['status'])

    def test_open_explicit_denial_does_not_fetch_text(self):
        self.register(2, 2)
        for exc in (AuthError('expired'), Error('denied')):
            self.client.open_rental = Mock(side_effect=exc)
            if isinstance(exc, AuthError):
                with self.assertRaises(AuthError):
                    self.run_once()
            else:
                self.run_once()
            self.assertEqual([], self.client.text_calls)
            self.assertEqual('rented', self.engine.episode_record(123, 2)['status'])
        self.assertEqual([2], self.client.ticket_calls)

    def test_missing_completed_file_never_spends_new_ticket_automatically(self):
        self.register()
        self.run_once()
        row = self.engine.episode_record(123, 2)
        Path(row['path']).unlink()
        self.client.eps[1]['access'] = 'locked'
        self.client.charged = True
        self.run_once()
        self.assertEqual([2], self.client.ticket_calls)
        self.assertEqual('missing', self.engine.episode_record(123, 2)['status'])

    def test_no_duplicate_after_restart(self):
        self.register(2, 2)
        self.run_once()
        self.engine = Engine(self.root / 'db.sqlite', lambda *a, **kw: self.client)
        self.run_once()
        self.assertEqual([2], self.client.text_calls)

    def test_new_episode_follows_latest_mode(self):
        self.register(4)
        self.client.eps[3]['access'] = 'free'
        self.run_once()
        self.client.eps.append(dict(pid=5,number=5,cursor=95,title='특별편',access='free'))
        self.run_once()
        self.assertEqual([4,5], self.client.text_calls)

    def test_stored_start_pid_survives_new_prior_episode(self):
        self.register(3, 3)
        self.client.eps.insert(0, dict(pid=99,number=1,cursor=999,title='프롤로그',access='free'))
        for i,ep in enumerate(self.client.eps,1):
            ep['number'] = i
            ep['access'] = 'free'
        self.run_once()
        self.assertEqual([3], self.client.text_calls)

    def test_range_invalid_and_removed_start_fail_closed(self):
        with self.assertRaises(Error):
            self.register(3, 2)
        self.register(3)
        self.client.eps = self.client.eps[:2]
        self.run_once()
        self.assertEqual([], self.client.ticket_calls)
        self.assertEqual('error', self.work()['status'])

    def test_pause_and_remove_keep_files(self):
        self.register(2, 2)
        self.engine.toggle(123, False)
        self.run_once()
        self.assertFalse(self.client.text_calls)
        self.engine.toggle(123, True)
        self.run_once()
        file = Path(self.engine.episode_record(123, 2)['path'])
        self.engine.remove(123)
        self.assertTrue(file.is_file())
        self.assertEqual('completed', self.engine.episode_record(123, 2)['status'])

    def test_cross_process_style_lock_and_same_instance_lock(self):
        second = Engine(self.engine.db_path)
        with self.engine.operation():
            with self.assertRaises(Error), self.engine.operation():
                pass
            with self.assertRaises(Error), second.operation():
                pass

    def test_background_starts_and_releases_lock(self):
        self.register(2, 2)
        self.engine.start(self.cfg, 'fake')
        self.engine.thread.join(5)
        self.assertFalse(self.engine.thread.is_alive())
        self.assertFalse(self.engine.active)
        self.assertEqual('done', self.engine.state['status'])

    def test_epub_is_valid_xml_and_ordered(self):
        self.register(1, 2)
        for e in self.client.eps:
            e['access'] = 'free'
        self.run_once()
        epub = next((self.root / 'output').rglob('*.epub'))
        with zipfile.ZipFile(epub) as z:
            self.assertEqual('mimetype', z.infolist()[0].filename)
            self.assertEqual(zipfile.ZIP_STORED, z.infolist()[0].compress_type)
            self.assertEqual(b'application/epub+zip', z.read('mimetype'))
            for name in z.namelist():
                if name.endswith(('.xml','.xhtml','.opf')):
                    ElementTree.fromstring(z.read(name))
            nav = z.read('OEBPS/nav.xhtml').decode()
            self.assertLess(nav.index('chapter-1'), nav.index('chapter-2'))


class HelpersTests(unittest.TestCase):
    def test_official_order_not_lexical_title(self):
        def row(pid, cursor, title):
            return dict(cursor_index=cursor,item=dict(product_id=pid,title=title,is_free=True))
        rows=[row(10,1,'10화'),row(2,9,'2화'),row(1,10,'1화')]
        result=normalize_episodes(rows + [rows[0]])
        self.assertEqual([1,2,10],[r['pid'] for r in result])
        with self.assertRaises(Error):
            normalize_episodes([row(1,1,'1'),row(2,1,'2')])

    def test_inline_text_and_image_marker(self):
        result=extract_paragraphs([{'type':'P','childParagraphList':[
            {'type':'TEXT','text':'첫 &amp; '},
            {'type':'B','childParagraphList':[{'type':'TEXT','text':'강조'}]},
            {'type':'IMG'}]}])
        self.assertEqual(['첫 & 강조[삽화]'],result)

    def test_cookie_domains_filtered_and_bad_input_rejected(self):
        clean=parse_cookies(json.dumps([{'name':'_kau','value':'example','domain':'.kakao.com'},
            {'name':'secret','value':'do-not-send','domain':'example.com'}]))
        self.assertEqual(1,len(clean))
        for raw in ('{}','[]','broken','[{"name":"_kau","value":"a\\nb"}]'):
            with self.assertRaises(Error):parse_cookies(raw)

    def test_only_kakao_series_urls(self):
        self.assertEqual(123,series_id('https://page.kakao.com/content/123/viewer/456'))
        for value in ('https://example.com/content/123','http://127.0.0.1','abc123','-1'):
            with self.assertRaises(Error):series_id(value)

    def test_private_cookie_file_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'cookie.json'
            atomic_bytes(p,b'[]',0o600)
            self.assertEqual(0o600,p.stat().st_mode & 0o777)

    def test_config_limits(self):
        with self.assertRaises(Error):config({'download_path':'relative'})
        with self.assertRaises(Error):config({'download_path':'/tmp/books','basic_interval':'0'})


if __name__=='__main__':
    unittest.main()
