"""카카오 웹 응답을 명시적으로 검사하는 소설 전용 어댑터.

지원하지 않는 응답은 중단합니다. 유료 구매, 대기 시간 변경, DRM 해제는 하지 않습니다.
통신 형태 참고: basilro/kaka_pe_dl 및 murianwind/webtoon-manager.
공개 무료 회차의 현행 응답 구조를 확인했습니다. 로그인 대여 연동은 별도 검증이 필요합니다.
"""
import html
import json
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests


class Error(Exception):
    pass


class AuthError(Error):
    pass


class SchemaError(Error):
    pass


class TicketUncertain(Error):
    pass


KST = timezone(timedelta(hours=9))
BFF = 'https://bff-page.kakao.com'
PAGE = 'https://page.kakao.com'


def series_id(value):
    value = str(value or '').strip()
    if re.fullmatch(r'[1-9]\d{0,14}', value):
        return int(value)
    u = urlparse(value)
    if u.scheme != 'https' or u.hostname != 'page.kakao.com':
        raise Error('카카오페이지 작품 URL 또는 작품 번호를 입력하세요.')
    match = re.fullmatch(r'/content/([1-9]\d*)(?:/viewer/\d+)?/?', u.path)
    if not match:
        raise Error('작품 주소는 https://page.kakao.com/content/작품번호 형식입니다.')
    return int(match[1])


def parse_cookies(raw):
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        raise Error('Cookie-Editor에서 내보낸 JSON 배열을 입력하세요.') from None
    if not isinstance(rows, list) or not rows or len(rows) > 300:
        raise Error('쿠키 JSON 배열이 올바르지 않습니다.')
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise Error('쿠키 항목 형식이 올바르지 않습니다.')
        name, value = row.get('name'), row.get('value')
        domain = row.get('domain', '.kakao.com')
        if not isinstance(domain, str):
            raise Error('쿠키 도메인 형식이 올바르지 않습니다.')
        host = domain.lstrip('.').lower()
        if host != 'kakao.com' and not host.endswith('.kakao.com'):
            continue
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            raise Error('쿠키 이름과 값은 문자열이어야 합니다.')
        if any(c in name + value for c in '\r\n\x00'):
            raise Error('쿠키에 잘못된 문자가 있습니다.')
        clean.append(dict(name=name, value=value, domain=domain,
                          path=row.get('path') or '/'))
    if not any(c['name'] == '_kau' for c in clean):
        raise AuthError('로그인 쿠키 _kau가 없습니다. 카카오페이지 로그인 후 다시 내보내세요.')
    return clean


def availability(item):
    if item.get('is_free') is True:
        return 'free'
    purchase = (item.get('service_property') or {}).get('purchase_info') or {}
    kind = purchase.get('purchase_type')
    if kind == 'own':
        return 'owned'
    if kind in ('rent', 'rental'):
        expiry = purchase.get('rent_expire_dt')
        if expiry:
            try:
                dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                dt = dt if dt.tzinfo else dt.replace(tzinfo=KST)
            except (ValueError, TypeError):
                raise SchemaError('대여 만료 시각 형식을 확인할 수 없습니다.') from None
            if dt <= datetime.now(KST):
                return 'locked'
        return 'rented'
    return 'locked'


def normalize_episodes(rows):
    """페이지 탐색 커서가 아닌 공식 order_value 오름차순으로 정렬."""
    episodes, seen = [], set()
    for row in rows:
        try:
            item = row['item']
            pid, cursor = int(item['product_id']), int(row['cursor_index'])
            order = int(item['order_value'])
        except (KeyError, ValueError, TypeError):
            raise SchemaError('회차 ID 또는 공식 목록 순서가 없습니다. 처리를 중단합니다.') from None
        if pid in seen:
            continue
        seen.add(pid)
        episodes.append(dict(pid=pid, cursor=cursor, order=order, title=str(item.get('title') or pid),
                             access=availability(item)))
    if len({e['order'] for e in episodes}) != len(episodes):
        raise SchemaError('공식 목록 순서가 중복됩니다. 회차를 임의 정렬하지 않습니다.')
    episodes.sort(key=lambda e: e['order'])
    for number, ep in enumerate(episodes, 1):
        ep['number'] = number
    return episodes


class Client:
    def __init__(self, cookie_json, session=None, stop=None):
        self.session = session or requests.Session()
        self.stop = stop
        self.device = ''
        self.session.headers.update({'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json', 'Origin': PAGE, 'Referer': PAGE + '/',
            'Accept-Language': 'ko-KR,ko;q=0.9'})
        for cookie in parse_cookies(cookie_json):
            self.session.cookies.set(**cookie)
            if cookie['name'] == '_kpdid':
                self.device = cookie['value']
        self.last_request = 0

    def close(self):
        self.session.close()

    def _pace(self):
        delay = max(0, 0.5 - (time.monotonic() - self.last_request))
        if self.stop is not None:
            if self.stop.wait(delay):
                raise Error('중지 요청으로 작업을 종료했습니다.')
        elif delay:
            time.sleep(delay)
        self.last_request = time.monotonic()

    def api(self, path, params=None, data=None):
        self._pace()
        try:
            r = self.session.request('POST' if data is not None else 'GET', BFF + path,
                params=params, data=data, timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            raise Error('네트워크 연결 실패 또는 응답 시간 초과입니다.') from None
        if r.status_code in (401, 403):
            raise AuthError('로그인 만료 또는 접근 거부입니다. 쿠키와 계정 상태를 확인하세요.')
        if r.status_code == 429:
            raise Error('요청 제한(429)입니다. 실행 간격을 늘린 후 다시 시도하세요.')
        if r.status_code != 200:
            raise Error('카카오 API HTTP 오류: %s' % r.status_code)
        try:
            body = json.loads(r.content)
        except (ValueError, TypeError):
            raise SchemaError('카카오 API JSON 응답 확인 실패 (%s).' % path) from None
        if (isinstance(body, dict) and 'result_code' not in body
                and path == '/api/gateway/api/v1/viewer/data'
                and any(isinstance(body.get(key), dict) for key in ('viewer_data', 'viewerData'))):
            return body
        if not isinstance(body, dict) or 'result_code' not in body:
            raise SchemaError('카카오 API 응답 확인 실패 (%s): result_code가 없거나 객체 형식이 아닙니다.' % path)
        code = body['result_code']
        if code == -100:
            raise AuthError('카카오 인증이 만료되었습니다. 로그인 쿠키를 갱신하세요.')
        if code != 0:
            # 원문 응답에는 토큰/개인정보가 들어갈 수 있어 로그로 돌려주지 않음.
            raise Error('카카오 API 처리 거부 (코드 %s)' % code)
        return body

    def result(self, path, **kwargs):
        result = self.api(path, **kwargs).get('result')
        if not isinstance(result, dict):
            raise SchemaError('API result 객체가 없습니다.')
        return result

    def catalog(self, sid):
        path = '/api/gateway/api/v2/content/product/list'
        def fetch(cursor, direction, size=25):
            return self.result(path, params=dict(series_id=sid, cursor_index=cursor,
                cursor_direction=direction, window_size=size, sort_type='asc'))
        anchor = fetch(0, 'ANCHOR', 6)
        meta = anchor.get('series_item') or {}
        if meta.get('category') not in ('웹소설', '소설'):
            raise Error('소설 작품으로 확인되지 않았습니다. 웹툰·만화는 지원하지 않습니다.')
        rows = anchor.get('list')
        if not isinstance(rows, list) or not rows:
            raise SchemaError('회차 목록이 비어 있습니다.')
        all_rows = list(rows)
        for direction, flag, index in (('PREV', 'has_prev', 0), ('NEXT', 'has_next', -1)):
            more, cursor = anchor.get(flag), rows[index].get('cursor_index')
            seen = set()
            for _ in range(1000):
                if more is False:
                    break
                if more is not True or cursor is None or cursor in seen:
                    raise SchemaError('회차 목록 페이지 연결이 불완전합니다. 부분 목록은 사용하지 않습니다.')
                seen.add(cursor)
                page = fetch(cursor, direction)
                chunk = page.get('list')
                if not isinstance(chunk, list) or not chunk:
                    raise SchemaError('회차 목록을 끝까지 읽지 못했습니다.')
                all_rows.extend(chunk)
                more, cursor = page.get(flag), chunk[index].get('cursor_index')
            else:
                raise SchemaError('회차 목록 최대 조회 범위를 초과했습니다.')
        return dict(sid=int(sid), title=str(meta.get('title') or sid),
                    meta=meta, episodes=normalize_episodes(all_rows))

    def tickets(self, sid):
        return self.result('/api/gateway/api/v1/ticket/my',
                           params=dict(series_id=sid, include_waitfree='true'))

    def ready(self, pid):
        return self.result('/api/gateway/api/v1/ticket/ready_to_use',
                           params=dict(product_id=pid, include_series='true'))

    def rent_waitfree(self, pid):
        # 변경 요청 자동 재시도 금지. 응답 유실 시 중복 사용을 막기 위해 확인 상태로 멈춤.
        try:
            return self.result('/api/gateway/api/v1/ticket/use',
                               data=dict(product_id=pid, ticket_type='RT05'))
        except Error:
            raise TicketUncertain('기다무 사용 결과를 확정할 수 없습니다. 카카오에서 대여 내역을 확인하세요.') from None

    def open_rental(self, sid, pid, uid):
        self.api('/api/gateway/api/v5/inven/open_page', data=dict(
            seriesId=sid, productId=pid, transactionId=self.device, ticket_uid=uid))

    def content_json(self, url):
        u = urlparse(url)
        # API가 http CDN 주소를 주기도 한다. 서명 경로/쿼리를 유지해 HTTPS로 요청.
        if u.scheme == 'http' and u.port is None:
            u = u._replace(scheme='https')
            url = u.geturl()
        host = u.hostname or ''
        allowed = ('kakao.com', 'kakaoentcdn.com', 'kakaocdn.net', 'kakaopage.com')
        if (u.scheme != 'https' or u.username or u.password or u.port not in (None, 443)
                or not any(host == x or host.endswith('.' + x) for x in allowed)):
            raise SchemaError('허용된 카카오 콘텐츠 서버 주소가 아닙니다.')
        self._pace()
        try:
            # API 쿠키를 CDN으로 전송하지 않음. 서명 URL만 사용.
            with requests.get(url, headers={'Referer': PAGE + '/'}, timeout=(10, 30),
                              allow_redirects=False, stream=True) as r:
                if r.status_code != 200:
                    raise Error('본문 서버 HTTP 오류: %s' % r.status_code)
                chunks, count = [], 0
                for part in r.iter_content(65536):
                    count += len(part)
                    if count > 20 * 1024 * 1024:
                        raise Error('회차 본문 응답이 20MB를 초과했습니다.')
                    chunks.append(part)
                return json.loads(b''.join(chunks))
        except requests.RequestException:
            raise Error('회차 본문 전송이 중단되었습니다.') from None
        except ValueError:
            raise SchemaError('본문이 지원하는 JSON 형식이 아닙니다.') from None

    def text(self, sid, pid):
        body = self.api('/api/gateway/api/v1/viewer/data',
                        params=dict(series_id=sid, product_id=pid))
        # 현재 API는 snake_case이며 type이 없다. 기존 camelCase 응답도 지원한다.
        viewer = body.get('viewer_data', body.get('viewerData'))
        if not isinstance(viewer, dict):
            raise SchemaError('본문 API에 viewer_data / viewerData 객체가 없습니다.')
        if viewer.get('type') not in (None, 'TextViewerData') or 'imageDownloadData' in viewer:
            raise SchemaError('텍스트 뷰어 형식이 아닙니다. 이미지·암호화 콘텐츠는 처리하지 않습니다.')
        contents = viewer.get('contents_list', viewer.get('contentsList'))
        server = viewer.get('ats_server_url', viewer.get('atsServerUrl'))
        if not isinstance(contents, list) or not contents or not isinstance(server, str):
            raise SchemaError('소설 본문 목록(contents_list) 또는 서버 주소(ats_server_url)가 없습니다.')
        paragraphs = []
        for part in contents:
            secure = part.get('secure_url', part.get('secureUrl')) if isinstance(part, dict) else None
            if not isinstance(secure, str) or not secure:
                raise SchemaError('일부 본문 주소가 없습니다. 불완전한 회차는 저장하지 않습니다.')
            # secure_url은 상대 경로뿐 아니라 '.json?...' 같은 접미사일 수 있다.
            # 서버가 준 서명 URL을 경로 정규화하거나 임의의 '/'를 삽입하지 않는다.
            url = secure if secure.startswith(('https://', 'http://')) else server + secure
            data = self.content_json(url)
            info = data.get('contentInfo') if isinstance(data, dict) else None
            nodes = info.get('paragraphList') if isinstance(info, dict) else None
            if not isinstance(nodes, list):
                raise SchemaError('소설 단락 목록이 없습니다.')
            paragraphs.extend(extract_paragraphs(nodes))
        if not any(p.strip() and p != '[삽화]' for p in paragraphs):
            raise SchemaError('저장할 소설 본문이 없습니다.')
        return '\n\n'.join(paragraphs)


def extract_paragraphs(nodes):
    """중첩 강조/링크 안의 TEXT도 보존. 삽화는 위치 표시만 지원."""
    def inline(node):
        kind = str(node.get('type', '')).upper()
        if kind == 'TEXT':
            return html.unescape(str(node.get('text') or ''))
        if kind in ('IMG', 'IMAGE'):
            return '[삽화]'
        if kind == 'BR':
            return '\n'
        text = ''.join(inline(n) for n in node.get('childParagraphList') or [])
        if kind in ('P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'LI'):
            return text.rstrip() + '\n\n'
        return text
    return [line for line in (inline(node).strip() for node in nodes) if line]
