import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kakao_novel_downloader.client import Error
from kakao_novel_downloader.copying import DEFAULT_RULE, copy_exclusive, validate
from kakao_novel_downloader.core import Engine, config
from kakao_novel_downloader.storage import digest


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.source.mkdir()
        self.engine = Engine(self.root / 'state.db')
        self.copier = self.engine.copier
        self.rule = dict(DEFAULT_RULE, source_path=str(self.source), destination_path=str(self.target))

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name='01500_시즌5 304화 각자의 게임(1) - 릴 [69188951].txt', text='본문\n둘째 줄'):
        p = self.source / name
        p.write_text(text, encoding='utf-8')
        return p

    def test_title_season_number_and_preview_never_writes(self):
        source = self.file()
        self.file('00100_시즌4 100화 이전 시즌.txt')
        plan = self.copier.preview(self.rule)
        row = next(r for r in plan['rows'] if r['status'] == 'copy')
        self.assertEqual(304, row['episode'])
        self.assertEqual('00304_각자의 게임(1) - 릴.txt', Path(row['target']).name)
        self.assertFalse(self.target.exists())
        result = self.copier.execute(self.rule)
        self.assertEqual(1, result['counts']['copied'])
        self.assertEqual(source.read_bytes(), Path(row['target']).read_bytes())
        self.assertTrue(source.exists())
        self.assertEqual(1, self.copier.execute(self.rule)['counts']['exists'])

    def test_offset_and_range_for_existing_other_downloader(self):
        self.file('1500_그날의 이야기.txt')
        self.file('1501_다음 이야기.txt')
        self.file('1499_이전 이야기.txt')
        rule = dict(self.rule, mode='offset', anchor_source=1500, anchor_episode=304, start_number=1500,
                    filename_template='{title} 시즌{season} {episode}화.txt', folder_template='시즌 {season}')
        rows = self.copier.plan(rule)
        copied = [r for r in rows if r['status']=='copy']
        self.assertEqual([304,305], [r['episode'] for r in copied])
        self.assertEqual('천재 궁수의 스트리밍 시즌5 304화.txt', Path(copied[0]['target']).name)
        self.assertEqual('시즌 5', Path(copied[0]['target']).parent.name)

    def test_existing_different_filename_same_episode_not_overwritten(self):
        source = self.file()
        self.target.mkdir()
        existing = self.target / '0304 기존 제목.txt'
        existing.write_text('기존 별도 내용')
        result = self.copier.execute(self.rule)
        self.assertEqual(1,result['counts']['conflict'])
        self.assertEqual('기존 별도 내용', existing.read_text())
        self.assertEqual(1,len(list(self.target.iterdir())))
        existing.write_bytes(source.read_bytes())
        self.assertEqual(1,self.copier.execute(self.rule)['counts']['exists'])

    def test_same_name_collision_protected_even_when_number_matching_off(self):
        self.file()
        self.target.mkdir()
        target=self.target/'00304_각자의 게임(1) - 릴.txt'
        target.write_bytes(b'keep')
        self.assertEqual(1,self.copier.execute(dict(self.rule,match_existing=False))['counts']['conflict'])
        self.assertEqual(b'keep',target.read_bytes())

    def test_multiple_sources_for_same_episode_all_held(self):
        self.file()
        self.file('01501_시즌5 304화 다른 제목.txt')
        result=self.copier.execute(self.rule)
        self.assertEqual(2,result['counts']['review'])
        self.assertFalse(self.target.exists())

    def test_different_season_existing_file_does_not_block(self):
        self.file()
        self.target.mkdir()
        (self.target/'시즌4 304화.txt').write_text('other')
        self.assertEqual(1,self.copier.execute(self.rule)['counts']['copied'])

    def test_invalid_patterns_and_paths(self):
        for changes in (dict(filename_template='{episode.__class__}.txt'),dict(filename_template='{episode:999999d}.txt'),
                        dict(filename_template='../x.txt'),dict(folder_template='../'),dict(destination_path='relative'),
                        dict(filename_template='{pid!r}.txt')):
            with self.assertRaises(Error): validate(dict(self.rule,**changes))
        self.file()
        self.assertEqual('review',self.copier.plan(dict(self.rule,destination_path=str(self.source)))[0]['status'])

    def test_symlink_escape_and_duplicate_destination_are_protected(self):
        self.file()
        self.target.mkdir()
        other=self.root/'outside';other.mkdir()
        (self.target/'season').symlink_to(other,target_is_directory=True)
        result=self.copier.execute(dict(self.rule,folder_template='season'))
        self.assertEqual(1,result['counts']['review'])
        self.assertFalse(list(other.iterdir()))

    def test_download_history_hash_guard_and_no_network_needed(self):
        source=self.file()
        ep=dict(pid=69188951,number=1500,title='시즌5 304화 각자의 게임(1) - 릴')
        self.engine.record(56505778,ep,'completed',path=str(source),sha256=digest(source))
        rule=dict(self.rule,source_path='')
        self.assertEqual(1,self.copier.execute(rule)['counts']['copied'])
        source.write_text('modified')
        self.assertEqual(1,self.copier.preview(rule)['counts']['review'])

    def test_saved_rule_restart_manual_background_and_removal_keep_files(self):
        self.file()
        saved=self.copier.save(self.rule)
        engine=Engine(self.engine.db_path)
        self.assertEqual(saved,engine.copier.get(saved['id']))
        engine.start_copy(saved['id']);engine.thread.join(5)
        self.assertFalse(engine.thread.is_alive())
        self.assertFalse(engine.active)
        self.assertEqual(1,engine.state['completed'])
        engine.copier.remove(saved['id'])
        self.assertTrue(list(self.target.glob('*.txt')))
        self.assertTrue(list(self.source.glob('*.txt')))

    def test_automatic_after_download_job_without_manual_copy(self):
        from test_core import FakeClient
        client=FakeClient();client.eps[1]['access']='free';client.eps[1]['title']='시즌5 304화 테스트'
        engine=Engine(self.engine.db_path,lambda *a,**kw:client)
        engine.analyze('123','fake');engine.register(123,2,2,False)
        engine.copier.save(dict(self.rule,sid=123,source_path='',enabled=True))
        engine.run(config({'download_path':str(self.root/'downloads')}),'fake')
        target=self.target/'00304_테스트.txt'
        self.assertTrue(target.exists())
        engine.run(config({'download_path':str(self.root/'downloads')}),'fake')
        self.assertEqual(1,len(list(self.target.iterdir())))
        self.assertEqual([],client.ticket_calls)

    def test_atomic_copy_race_and_changed_source_leave_existing_intact(self):
        source=self.file();self.target.mkdir();target=self.target/'out.txt';target.write_bytes(b'old')
        with self.assertRaises(FileExistsError):copy_exclusive(source,target,digest(source))
        self.assertEqual(b'old',target.read_bytes())
        with self.assertRaises(Error):copy_exclusive(source,self.target/'new.txt','not-the-hash')
        self.assertFalse((self.target/'new.txt').exists())
        self.assertFalse(list(self.target.glob('.kakao-copy-*')))

    def test_stop_does_not_create_copies(self):
        self.file();self.engine.stop_event.set()
        self.assertEqual(1,self.copier.execute(self.rule)['counts']['copy'])
        self.assertFalse(self.target.exists())


if __name__=='__main__':unittest.main()
