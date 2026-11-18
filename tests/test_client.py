import json
import unittest
from unittest.mock import Mock

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
            return {'cursor_index':cursor,'item':{'product_id':pid,'is_free':True,'title':str(pid)}}
        self.client.result=Mock(side_effect=[
            {'series_item':{'category':'소설','title':'test'},'list':[ep(2,8)],'has_prev':True,'has_next':True},
            {'list':[ep(3,7)],'has_prev':False},
            {'list':[ep(1,9)],'has_next':False}])
        result=self.client.catalog(1)
        self.assertEqual([1,2,3],[e['pid'] for e in result['episodes']])

    def test_partial_pagination_and_wrong_category_fail(self):
        self.client.result=Mock(side_effect=[
            {'series_item':{'category':'소설'},'list':[{'cursor_index':10,'item':{'product_id':1}}],
             'has_prev':True,'has_next':False}, {'list':[],'has_prev':False}])
        with self.assertRaises(SchemaError):self.client.catalog(1)
        self.client.result=Mock(return_value={'series_item':{'category':'웹툰'}})
        with self.assertRaisesRegex(Error,'소설'):self.client.catalog(1)

    def test_content_host_restriction(self):
        for url in ('http://localhost/test','https://example.org/chapter','https://kakao.com.example.org/data',
                    'https://user:password@kakao.com/data','https://kakao.com:444/data'):
            with self.assertRaises(SchemaError):self.client.content_json(url)

    def test_text_all_chunks_required(self):
        self.client.api=Mock(return_value={'viewerData':{'type':'TextViewerData',
            'atsServerUrl':'https://cdn.kakao.com','contentsList':[{'secureUrl':'/1'},{'secureUrl':'/2'}]}})
        self.client.content_json=Mock(side_effect=[{'contentInfo':{'paragraphList':[{'type':'TEXT','text':'first'}]}},Error('missing')])
        with self.assertRaises(Error):self.client.text(1,2)


if __name__=='__main__':
    unittest.main()
