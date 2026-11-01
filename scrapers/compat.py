"""Small Python 3.10 host for the attributed Amane source adapters."""
import asyncio
import re
import socket
import ssl
from .block import classify_block
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlparse


class SiteName:
    JAV321 = 'jav321'
    JAVBUS = 'javbus'
    FC2 = 'fc2'
    FC2CLUB = 'fc2club'
    FC2PPVDB = 'fc2ppvdb'
    FREEJAVBT = 'freejavbt'
    AIRAV = 'airav'
    XCITY = 'xcity'


class ActorGender:
    UNKNOWN = 'unknown'


@dataclass
class CrawlerProfile:
    name: str
    base_url: str
    cookies: dict = None
    headers: dict = None
    same_origin_referer: bool = False


@dataclass
class SearchQuery:
    number: str


class FetchOptions:
    pass


class MediaMetadata:
    def __init__(self, **values):
        self.values = values


def film_actors(names, gender=None):
    return list(dict.fromkeys(names))


def extract_text(html, *xpaths):
    return next((value for xpath in xpaths if (value := (html.xpath(xpath).get() or '').strip())), '')


def extract_all_texts(html, *xpaths):
    for xpath in xpaths:
        values = list(dict.fromkeys(v.strip() for v in html.xpath(xpath).getall() if v.strip()))
        if values:
            return values
    return []


def fold_number(value):
    folded = re.sub(r'[-\s]', '', value).casefold()
    return re.sub(r'^fc2(?:ppv)?(?=\d+$)', 'fc2', folded)


def number_variants(number):
    # Deliberately conservative: never drop a numeric prefix without identity review.
    article_id = fc2_article_id(number)
    return tuple(dict.fromkeys((number.strip(), 'FC2-'+article_id, 'FC2-PPV-'+article_id))) if article_id else (number.strip(),)


def fc2_article_id(number):
    match = re.fullmatch(r'FC2[-_ ]?(?:PPV[-_ ]?)?(\d+)', number, re.I)
    return match[1] if match else None


def fc2_number(article_id, ppv=False):
    return ('FC2-PPV-' if ppv else 'FC2-') + article_id


def is_same_number(left, right):
    return bool(left and right) and fold_number(left) == fold_number(right)


def leading_token(text):
    return text.strip().split()[0] if text.strip() else ''


def select_number_index(found, query):
    matches = [i for i, value in enumerate(found) if fold_number(value) == fold_number(query)]
    return matches[0] if len(matches) == 1 else None


class RequestError(ValueError):
    def __init__(self, message, http_status=None, reason=None):
        super().__init__(message)
        self.http_status = http_status
        self.reason = reason or ('not_found' if http_status == 404 else 'rate_limited' if http_status == 429 else 'server_error' if http_status and http_status >= 500 else 'http_error' if http_status else 'network')


def fetch_bytes(url, proxy='', headers=None, limit=8_000_000, with_status=False):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username:
        raise RequestError('来源资源必须使用 HTTPS')
    opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
    try:
        with opener.open(Request(url, headers=headers or {}), timeout=20) as response:
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise RequestError('来源响应超过大小限制')
            result = (payload, response.headers.get('Content-Type', ''))
            return (*result, response.status) if with_status else result
    except HTTPError as exc:
        reason = classify_block(exc.read(100_000).decode('utf-8', errors='replace'))
        raise RequestError('来源 HTTP ' + str(exc.code), exc.code, reason if reason != 'empty_response' else None) from None
    except (TimeoutError, socket.timeout):
        raise RequestError('来源请求超时', reason='timeout') from None
    except (URLError, OSError) as exc:
        underlying = exc.reason if isinstance(exc, URLError) else exc
        reason = 'timeout' if isinstance(underlying, (TimeoutError, socket.timeout)) else 'dns' if isinstance(underlying, socket.gaierror) else 'tls' if isinstance(underlying, ssl.SSLError) else 'network'
        raise RequestError('来源网络连接失败', reason=reason) from None


class HttpClient:
    def __init__(self, proxy=''):
        self.proxy = proxy

    async def get_html(self, url, cookies=None, headers=None):
        request_headers = {'User-Agent': 'Mozilla/5.0', **(headers or {})}
        if cookies:
            request_headers['Cookie'] = '; '.join(k + '=' + v for k, v in cookies.items())
        body, _ = await asyncio.to_thread(fetch_bytes, url, self.proxy, request_headers)
        text = body.decode('utf-8', errors='replace')
        reason = classify_block(text)
        if reason:
            raise RequestError('来源页面无法用于刮削', reason=reason)
        return text


class Crawler:
    @staticmethod
    def _parse_runtime(text):
        match = re.search(r'\d+', text or '')
        return int(match[0]) if match else None

    def __init__(self, client):
        profile = self.profile()
        self.client = client
        self.base_url = profile.base_url
        self.cookies = profile.cookies or {}
        self.headers = profile.headers or {}

    async def fetch(self, query):
        url = await self._search(query)
        return await self._scrape(url) if url else None
