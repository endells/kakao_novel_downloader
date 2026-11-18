import json
import unittest
from unittest.mock import Mock, patch

from kakao_novel_downloader.client import Client, Error, AuthError, TicketUncertain, SchemaError


COOKIE = '[{"name":"_kau","value":"test-only","domain":".kakao.com"}]'


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.client = Client(COOKIE, session=self.session)
        self.client._pace = lambda: None

    def response(self, body, status=200):
        self.session.request.return_value = Mock(status_code=status, content=json.dumps(body).encode())

    def test_auth_rate_and_bad_shape(self):
        self.response({'result_code': -100})
        with self.assertRaises(AuthError):self.client.tickets(1)
        self.response({},429)
        with self.assertRaisesRegex(Error,'429'):self.client.tickets(1)
        self.response({'result':{}})
        with self.assertRaises(SchemaError):self.client.tickets(1)

    def test_open_missing_result_code_identifies_endpoint(self):
        self.response({'result': {}})
        with self.assertRaisesRegex(SchemaError, 'open_page'):
            self.client.open_rental(1, 2, 'test-ticket')

    def test_viewer_without_result_code_is_valid_but_denial_is_not(self):
        path = '/api/gateway/api/v1/viewer/data'
        self.response({'viewerData': {'type': 'TextViewerData'}})
        self.assertIn('viewerData', self.client.api(path))
        self.response({'result_code': -100, 'viewerData': {}})
        with self.assertRaises(AuthError):
            self.client.api(path)

    def test_current_viewer_response_and_all_content_parts(self):
        # 2026-11-18 공개 무료 회차에서 확인한 구조. 본문/서명값은 가상 데이터.
        self.response({'viewer_data': {'ats_server_url': 'https://dn-img-page.kakao.com/sdownload/resource',
            'contents_list': [{'chapter_id': 0, 'content_id': 0, 'secure_url': '.json?part=cover'},
                              {'chapter_id': 1, 'content_id': 0, 'secure_url': '.json?token=a%2Fb%2Bc'}]}})
        self.client.content_json = Mock(side_effect=[
            {'contentInfo': {'paragraphList': [{'type': 'DIV', 'childParagraphList': [{'type': 'IMG'}]}]}},
            {'contentInfo': {'paragraphList': [{'type': 'DIV', 'childParagraphList': [
                {'type': 'P', 'childParagraphList': [{'type': 'TEXT', 'text': '테스트 첫 문단'}]},
                {'type': 'P', 'childParagraphList': [{'type': 'TEXT', 'text': '테스트 둘째 문단'}]}]}]}}])
        self.assertEqual('[삽화]\n\n테스트 첫 문단\n\n테스트 둘째 문단', self.client.text(1, 2))
        urls = [call.args[0] for call in self.client.content_json.call_args_list]
        self.assertEqual(['https://dn-img-page.kakao.com/sdownload/resource.json?part=cover',
                         'https://dn-img-page.kakao.com/sdownload/resource.json?token=a%2Fb%2Bc'], urls)

    def test_current_viewer_denial_not_ignored(self):
        for code in (-100, 123):
            self.response({'result_code': code, 'viewer_data': {'contents_list': []}})
            with self.assertRaises(Error):
                self.client.text(1, 2)

    def test_current_viewer_malformed_parts_and_image_only_fail(self):
        for viewer in ({'contents_list': []}, {'imageDownloadData': {'files': []}},
                       {'ats_server_url': 'https://cdn.kakao.com', 'contents_list': [None]},
                       {'ats_server_url': 'https://cdn.kakao.com', 'contents_list': [{}]}):
            self.response({'viewer_data': viewer})
            with self.assertRaises(SchemaError):
                self.client.text(1, 2)

    def test_current_viewer_incomplete_body_is_not_success(self):
        self.response({'viewer_data': {'ats_server_url': 'https://cdn.kakao.com',
            'contents_list': [{'secure_url': '/one'}, {'secure_url': '/two'}]}})
        self.client.content_json = Mock(side_effect=[
            {'contentInfo': {'paragraphList': [{'type': 'TEXT', 'text': 'test'}]}}, []])
        with self.assertRaisesRegex(SchemaError, '단락'):
            self.client.text(1, 2)

    def test_ticket_request_only_rt05_and_not_retried(self):
        self.response({'result_code':0,'result':{'ticket_uid':'test'}})
        self.client.rent_waitfree(7)
        args,kwargs=self.session.request.call_args
        self.assertEqual('POST',args[0])
        self.assertEqual({'product_id':7,'ticket_type':'RT05'},kwargs['data'])
        self.response({},500)
        self.session.request.reset_mock()
        with self.assertRaises(TicketUncertain):self.client.rent_waitfree(7)
        self.assertEqual(1,self.session.request.call_count)

    def test_pagination_collects_both_directions_before_sorting(self):
        def ep(pid,cursor):
            return {'cursor_index':cursor,'item':{'product_id':pid,'is_free':True,'title':str(pid),'order_value':pid}}
        self.client.result=Mock(side_effect=[
            {'series_item':{'category':'소설','title':'test'},'list':[ep(2,8)],'has_prev':True,'has_next':True},
            {'list':[ep(3,7)],'has_prev':False},
            {'list':[ep(1,9)],'has_next':False}])
        result=self.client.catalog(1)
        self.assertEqual([1,2,3],[e['pid'] for e in result['episodes']])
        for call in self.client.result.call_args_list:
            self.assertEqual('asc', call.kwargs['params']['sort_type'])

    def test_partial_pagination_and_wrong_category_fail(self):
        self.client.result=Mock(side_effect=[
            {'series_item':{'category':'소설'},'list':[{'cursor_index':10,'item':{'product_id':1,'order_value':1}}],
             'has_prev':True,'has_next':False}, {'list':[],'has_prev':False}])
        with self.assertRaises(SchemaError):self.client.catalog(1)
        self.client.result=Mock(return_value={'series_item':{'category':'웹툰'}})
        with self.assertRaisesRegex(Error,'소설'):self.client.catalog(1)

    def test_content_host_restriction(self):
        for url in ('http://localhost/test','https://example.org/chapter','https://kakao.com.example.org/data',
                    'https://user:password@kakao.com/data','https://kakao.com:444/data'):
            with self.assertRaises(SchemaError):self.client.content_json(url)

    def test_http_cdn_upgraded_to_https_without_changing_signature(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'{"contentInfo":{"paragraphList":[]}}']
        with patch('kakao_novel_downloader.client.requests.get') as get:
            get.return_value.__enter__.return_value = response
            self.client.content_json('http://dn-img-page.kakao.com/body?token=a%2Fb%2Bc')
            self.assertEqual('https://dn-img-page.kakao.com/body?token=a%2Fb%2Bc', get.call_args.args[0])
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertNotIn('Cookie', get.call_args.kwargs['headers'])

    def test_text_all_chunks_required(self):
        self.client.api=Mock(return_value={'viewerData':{'type':'TextViewerData',
            'atsServerUrl':'https://cdn.kakao.com','contentsList':[{'secureUrl':'/1'},{'secureUrl':'/2'}]}})
        self.client.content_json=Mock(side_effect=[{'contentInfo':{'paragraphList':[{'type':'TEXT','text':'first'}]}},Error('missing')])
        with self.assertRaises(Error):self.client.text(1,2)


if __name__=='__main__':
    unittest.main()
