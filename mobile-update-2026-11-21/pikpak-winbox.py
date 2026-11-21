# -*- coding: utf-8 -*-
"""
PikPak 网盘源（Win-Box / CatVod type=3 Python 源）

设计目标：对齐 uz影视 的 panTools-PikPak 能力，但不依赖 uz 运行时：
  * 粘贴 PikPak 分享链接（mypikpak.com/s/... 或 pikpak.me/s/...）直接解析播放
  * 支持 PikPak 账号 Token：浏览「我的网盘」、取原画直链、公共模式失败时转存兜底
  * 支持按 TG 公开频道扫描 PikPak 分享链接（可选，需能访问 t.me）
  * 支持 HTTP 代理（PikPak 分享接口对大陆 IP 返回 PROHIBITED，实测必须走代理）

配置写在源的 ext（JSON）里，或同目录的 pikpak-config.json（优先级更高）：
{
  "proxy": "http://127.0.0.1:7890",
  "token": "Bearer eyJhbGciOi...",
  "channels": ["some_channel"],
  "use_token_restore": false
}

只使用标准库，方便在 Win-Box 自带 CPython 与本机 Python 上直接跑。
"""

import base64
import http.cookiejar
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import html as _html
except Exception:  # pragma: no cover
    _html = None

def _normalize_share_link(link):
    """把各种协议/域名/子域的分享链接统一成 https://mypikpak.com/s/<sid>[/<folder>][?pwd=...]。"""
    text = str(link or '').strip()
    match = _SHARE_ID_RE.search(text)
    if not match:
        return text
    sid, folder = match.group(1), (match.group(2) or '')
    url = 'https://mypikpak.com/s/' + sid + (('/' + folder) if folder else '')
    pwd = re.search(r'[?&](?:pwd|pass_code|passcode|code)=([^&\s]+)', text, re.I)
    if pwd:
        url += '?pwd=' + urllib.parse.quote(urllib.parse.unquote(pwd.group(1)))
    return url


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
# PikPak 分享链接的多种协议/域名写法（http(s)、pikpak://、mypikpak://、www/m 子域）
_SHARE_LINK_RE = (r'(?:(?:https?|pikpak|mypikpak)://)?'
                  r'(?:(?:www\.|m\.)?(?:mypikpak\.com|mypikpak\.net|pikpak\.me|pikpak\.com)/)?'
                  r's/[A-Za-z0-9_/-]+(?:\?[A-Za-z0-9_=&%-]+)?')
_SHARE_ID_RE = re.compile(
    r'(?:(?:https?|pikpak|mypikpak)://)?'
    r'(?:(?:www\.|m\.)?(?:mypikpak\.com|mypikpak\.net|pikpak\.me|pikpak\.com)/)?'
    r's/([A-Za-z0-9_-]+)(?:/([A-Za-z0-9_-]+))?', re.I)

API_BASE = 'https://api-drive.mypikpak.com'
USER_BASE = 'https://user.mypikpak.com'
CLIENT_ID = 'YUMx5nI8ZU8Ap8pm'
DEVICE_ID = '9e8c121ebc0b409e85cc72cb2d424b54'
CAPTCHA_SIGN = '1.00d38b84b3231b2ac78b41091c11e5ca'

VIDEO_EXT = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts',
    '.m2ts', '.mpg', '.mpeg', '.rmvb', '.rm', '.3gp', '.ogv', '.vob',
}

IMAGE_EXT = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif', '.svg', '.ico',
    '.heic', '.heif', '.tif', '.tiff', '.jfif', '.pjpeg', '.apng',
}

HELP_TEXT = """PikPak 网盘源 · 使用说明

一、直接播分享链接
在 Win-Box 顶部搜索框里粘贴 PikPak 分享链接（https://mypikpak.com/s/xxxx），
点搜索 → 打开结果 → 就能看到分享里的视频列表。

二、大陆网络必须配代理
PikPak 的分享接口对大陆 IP 返回「分享功能在当前地区不可用」。
到「配置 → 源 → 编辑该源 → ext」里填：
  {"proxy":"http://127.0.0.1:7890"}
（端口换成你自己的 HTTP 代理；只支持 http 代理，不支持 socks5）

三、看自己的网盘
浏览器登录 mypikpak.com → F12 → Network → 任意 api-drive.mypikpak.com 请求
→ 复制请求头里的 authorization 整条（以 Bearer 开头）→ 填到 ext：
  {"token":"Bearer eyJhbGciOi..."}
保存后首页会出现「我的网盘」分类。

四、大文件
公共模式拿不到原画时，可把 use_token_restore 设为 true：
  {"token":"Bearer ...","use_token_restore":true}
它会先把文件转存到你的网盘再取直链（会占用网盘空间）。

五、TG 频道扫描（可选）
  {"channels":["频道username1","频道username2"]}
首页会出现对应频道分类；搜索关键词时也会在这些频道里找。
仅支持公开频道，且本机要能访问 t.me。
"""


class PikPakError(Exception):
    pass


class RegionBlocked(PikPakError):
    pass


class AuthRequired(PikPakError):
    pass


def _b64e(text):
    raw = text.encode('utf-8') if isinstance(text, str) else text
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64d(text):
    pad = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad).decode('utf-8', 'replace')


def _pack(obj):
    return _b64e(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))


def _unpack(text):
    try:
        raw = str(text or '').strip()
        if raw.startswith('{'):
            obj = json.loads(raw)
        else:
            obj = json.loads(_b64d(raw))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _human_size(num):
    try:
        n = float(num or 0)
    except Exception:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%.1f %s' % (n, unit)) if unit != 'B' else ('%d B' % n)
        n /= 1024.0
    return ''


def _is_image(item):
    mime = str(item.get('mime_type') or '').lower()
    if mime.startswith('image/'):
        return True
    name = str(item.get('name') or '')
    ext = os.path.splitext(name)[1].lower()
    return ext in IMAGE_EXT


def _is_video(item):
    mime = str(item.get('mime_type') or '').lower()
    if mime.startswith('video/'):
        return True
    name = str(item.get('name') or '')
    ext = os.path.splitext(name)[1].lower()
    return ext in VIDEO_EXT


def _natural_key(name):
    # 只看文件名本身（去掉文件夹路径前缀），避免不同目录的相同前缀打乱整季顺序
    base = str(name or '').replace('\\', '/').rsplit('/', 1)[-1]
    return [int(x) if x.isdigit() else x.lower()
            for x in re.split(r'(\d+)', base)]


class Spider(object):
    def __init__(self):
        self.ext = {}
        self.proxy = ''
        self.token = ''
        self.channels = []
        self.timeout = 20
        self.max_files = 300
        self.max_depth = 3
        self.max_folders = 40
        self.detail_budget = 15
        self.use_token_restore = False
        self.remember_recent = True
        self.tg_pages = 2
        self.http = None
        self.jar = http.cookiejar.CookieJar()
        self._cap = {}
        self._cap_lock = threading.Lock()
        self._share_cache = {}
        self._files_cache = {}
        self._state_path = ''
        self._state = {'recent': [], 'created': [], 'csv': []}

    # ------------------------------------------------------------------ 生命周期
    def getName(self):
        return 'PikPak 网盘'

    def init(self, extend=''):
        self.ext = self._parse_ext(extend)
        self.proxy = str(self.ext.get('proxy') or self.ext.get('代理') or '').strip()
        if self.proxy and '://' not in self.proxy:
            self.proxy = 'http://' + self.proxy
        token = (self.ext.get('token') or self.ext.get('PikPakToken')
                 or self.ext.get('pikpak_token') or self.ext.get('pikpak') or '')
        token = str(token).strip()
        if token and not token.lower().startswith('bearer '):
            token = 'Bearer ' + token
        self.token = token
        self.channels = self._parse_channels(self.ext.get('channels') or self.ext.get('频道'))
        self.timeout = self._int(self.ext.get('timeout'), 20, 5, 120)
        self.max_files = self._int(self.ext.get('max_files'), 300, 20, 5000)
        self.max_depth = self._int(self.ext.get('max_depth'), 3, 1, 20)
        self.max_folders = self._int(self.ext.get('max_folders'), 40, 5, 500)
        self.detail_budget = self._int(self.ext.get('detail_budget'), 15, 5, 120)
        self.tg_pages = self._int(self.ext.get('tg_pages'), 2, 1, 10)
        self.tg_cache_ttl = self._int(self.ext.get('tg_cache_ttl'), 300, 30, 3600)
        self.use_token_restore = bool(self.ext.get('use_token_restore'))
        self.auto_quality = self.ext.get('auto_quality', True) is not False
        self.sheet_url = str(self.ext.get('sheet_url') or '').strip()
        self.sheet_gid = str(self.ext.get('sheet_gid') or '0').strip()
        self.sheet_cache_ttl = self._int(self.ext.get('sheet_cache_ttl'), 600, 60, 86400)
        self.remember_recent = self.ext.get('remember_recent', True) is not False
        # Win-Box 自带的 drpyS 媒体代理（多线程 + Range），默认可用来加速 PikPak 直链
        # 实测代理速度不稳定（有时比直连慢一半），默认关闭；需要时在 ext 里开启
        self.media_proxy = self.ext.get('media_proxy', False) is True
        self.media_proxy_url = str(self.ext.get('media_proxy_url')
                                   or 'http://127.0.0.1:5575/proxy')
        self.media_proxy_thread = self._int(self.ext.get('media_proxy_thread', 4), 4, 1, 16)
        self._mp_alive = None
        self._mp_check_at = 0.0
        self._build_http()
        self._load_state()
        if self.token and self._jwt_expired(self.token):
            print('[pikpak] token 已过期，请重新获取')

    def destroy(self):
        self._save_state()

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _int(value, default, low, high):
        try:
            return max(low, min(high, int(value)))
        except Exception:
            return default

    def _parse_ext(self, extend):
        ext = {}
        if isinstance(extend, dict):
            ext = dict(extend)
        elif isinstance(extend, str) and extend.strip():
            text = extend.strip()
            if text.startswith('{'):
                try:
                    obj = json.loads(text)
                    if isinstance(obj, dict):
                        ext = obj
                except Exception:
                    ext = {}
        side = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pikpak-config.json')
        if os.path.isfile(side):
            try:
                with open(side, 'r', encoding='utf-8-sig') as fh:
                    obj = json.load(fh)
                if isinstance(obj, dict):
                    ext = dict(ext)
                    ext.update(obj)
            except Exception as exc:
                print('[pikpak] 读取 pikpak-config.json 失败: %s' % exc)
        return ext

    @staticmethod
    def _parse_channels(value):
        if isinstance(value, str):
            parts = re.split(r'[,，&|\s]+', value)
        elif isinstance(value, (list, tuple)):
            parts = [str(x) for x in value]
        else:
            parts = []
        out = []
        for part in parts:
            name = part.strip()
            if not name:
                continue
            name = re.sub(r'^https?://t\.me/(s/)?', '', name, flags=re.I)
            name = name.lstrip('@').strip('/')
            if name and name not in out:
                out.append(name)
        return out

    @staticmethod
    def _jwt_expired(token):
        try:
            payload = token.split(' ', 1)[-1].split('.')[1]
            payload += '=' * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload).decode('utf-8', 'replace'))
            return int(data.get('exp') or 0) < int(time.time())
        except Exception:
            return False

    # ------------------------------------------------------------------ 网络
    def _build_http(self):
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({'http': self.proxy, 'https': self.proxy}))
        try:
            ctx = ssl._create_unverified_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        except Exception:
            pass
        self.http = urllib.request.build_opener(*handlers)
        self.http.addheaders = [('User-Agent', UA), ('Accept-Language', 'zh-CN,zh;q=0.9')]

    def _req(self, url, method='GET', headers=None, body=None, timeout=None):
        if self.http is None:
            self._build_http()
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            if value is None:
                continue
            req.add_header(key, str(value))
        try:
            with self.http.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read(), resp.geturl()
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read()
            except Exception:
                data = b''
            return exc.code, dict(exc.headers or {}), data, url
        except Exception as exc:
            raise PikPakError('网络请求失败：%s（%s）' % (exc, url.split('?')[0]))

    def _json_req(self, url, method='GET', headers=None, body=None, timeout=None):
        status, hdrs, data, final = self._req(url, method, headers, body, timeout)
        try:
            obj = json.loads(data.decode('utf-8', 'replace'))
        except Exception:
            obj = None
        return status, obj, data

    def _captcha(self, action):
        with self._cap_lock:
            return self._captcha_locked(action)

    def _captcha_locked(self, action):
        now = time.time()
        hit = self._cap.get(action)
        if hit and now - hit[1] < 280:
            return hit[0]
        payload = {
            'client_id': CLIENT_ID,
            'action': action,
            'device_id': DEVICE_ID,
            'captcha_token': '',
            'meta': {
                'captcha_sign': CAPTCHA_SIGN,
                'client_version': 'undefined',
                'package_name': 'drive.mypikpak.com',
                'user_id': '',
                # 这个签名与时间戳是绑定的一组；换成当前时间会报 invalid captcha_sign
                'timestamp': '1758527222654',
            },
        }
        status, obj, raw = self._json_req(
            USER_BASE + '/v1/shield/captcha/init', 'POST',
            {'Content-Type': 'application/json', 'x-client-id': CLIENT_ID,
             'x-device-id': DEVICE_ID},
            json.dumps(payload).encode('utf-8'))
        token = str((obj or {}).get('captcha_token') or '')
        if not token:
            text = raw.decode('utf-8', 'replace')[:200]
            if status == 200 and '"captcha_token"' not in text:
                raise PikPakError('获取 PikPak 验证令牌失败：%s' % text)
            raise PikPakError('获取 PikPak 验证令牌失败（HTTP %s）：%s' % (status, text))
        self._cap[action] = (token, now)
        return token

    def _api(self, path, params=None, method='GET', body=None, action=None, auth=False):
        url = API_BASE + path
        if params:
            url += '?' + urllib.parse.urlencode(params, doseq=True)
        headers = {'accept': 'application/json', 'x-client-id': CLIENT_ID,
                   'x-device-id': DEVICE_ID}
        if auth:
            if not self.token:
                raise AuthRequired('该操作需要 PikPak Token，请在源的 ext 里填 token')
            headers['authorization'] = self.token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['content-type'] = 'application/json'

        actions = [action or ('%s:%s' % (method, path))]
        alt = 'GET:/drive/v1/share/file_info'
        if alt not in actions:
            actions.append(alt)

        last = None
        for idx, act in enumerate(actions):
            hdrs = dict(headers)
            try:
                hdrs['x-captcha-token'] = self._captcha(act)
            except PikPakError as exc:
                last = exc
                continue
            status, obj, raw = self._json_req(url, method, hdrs, data)
            if status == 200 and obj is not None and not obj.get('error'):
                return obj
            text = raw.decode('utf-8', 'replace')
            last = PikPakError(self._api_error(status, obj, text))
            # 验证令牌类错误换一组 action 再试一次
            if idx + 1 < len(actions) and ('captcha' in text.lower() or status in (400, 403)):
                self._cap.pop(act, None)
                continue
            break
        raise last or PikPakError('PikPak 接口调用失败')

    @staticmethod
    def _api_error(status, obj, text):
        code = str((obj or {}).get('error_code') or '')
        desc = str((obj or {}).get('error_description') or (obj or {}).get('error') or '')
        raw = (desc or text or '')[:260]
        if 'PROHIBITED' in text or '当前地区' in raw or '地区不可用' in raw:
            return ('PikPak 分享接口在当前地区不可用（PROHIBITED）。'
                    '请配置 proxy 走代理后重试，例如 {"proxy":"http://127.0.0.1:7890"}')
        if status == 401 or code == '16' or 'unauthorized' in raw.lower():
            return 'PikPak 授权失败（HTTP %s）：%s。请重新复制 authorization' % (status, raw)
        return 'PikPak 接口错误（HTTP %s%s）：%s' % (status, (', code ' + code) if code else '', raw)

    # ------------------------------------------------------------------ 状态
    def _load_state(self):
        base = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
        path = os.path.join(base, 'pikpak-state.json')
        legacy = os.path.join(tempfile.gettempdir(), 'winbox-pikpak-state.json')
        # 老版本把记录写在临时目录，导致换进程后读不到；这里做一次性迁移
        try:
            if not os.path.isfile(path) and os.path.isfile(legacy):
                with open(legacy, 'r', encoding='utf-8-sig') as fh:
                    old_obj = json.load(fh)
                if isinstance(old_obj, dict) and (old_obj.get('recent') or old_obj.get('created')):
                    with open(path, 'w', encoding='utf-8') as fh:
                        json.dump(old_obj, fh, ensure_ascii=False)
                    print('[pikpak] 已迁移 %d 条解析记录' % len(old_obj.get('recent') or []))
        except Exception as exc:
            print('[pikpak] 迁移解析记录失败：%s' % str(exc)[:80])
        try:
            if os.access(os.path.dirname(path), os.W_OK):
                self._state_path = path
            else:
                self._state_path = legacy
            if os.path.isfile(self._state_path):
                with open(self._state_path, 'r', encoding='utf-8-sig') as fh:
                    obj = json.load(fh)
                if isinstance(obj, dict):
                    self._state['recent'] = obj.get('recent') if isinstance(obj.get('recent'), list) else []
                    self._state['created'] = obj.get('created') if isinstance(obj.get('created'), list) else []
                    self._state['csv'] = obj.get('csv') if isinstance(obj.get('csv'), list) else []
        except Exception:
            pass

    def _save_state(self):
        if not self._state_path:
            return
        try:
            tmp = self._state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self._state, fh, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def _forget_recent(self, indexes=None):
        recent = self._state.get('recent') or []
        if indexes is None:
            count = len(recent)
            self._state['recent'] = []
        else:
            drop = set(int(i) for i in indexes)
            kept = [item for i, item in enumerate(recent) if i not in drop]
            count = len(recent) - len(kept)
            self._state['recent'] = kept
        self._save_state()
        return count

    def _remember(self, url, name):
        if not self.remember_recent or not url:
            return
        recent = [x for x in self._state.get('recent', []) if isinstance(x, dict) and x.get('u') != url]
        # 已有真实名称就别被「PikPak 分享 xxxx」覆盖
        for x in self._state.get('recent', []):
            if isinstance(x, dict) and x.get('u') == url:
                old_name = str(x.get('n') or '').strip()
                if old_name and not old_name.startswith('PikPak 分享') and old_name.lower() != 'pikpak':
                    if not name or str(name).startswith('PikPak 分享'):
                        name = old_name
                break
        recent.insert(0, {'u': url, 'n': name or '', 't': int(time.time())})
        self._state['recent'] = recent[:120]
        self._save_state()

    # ------------------------------------------------------------------ 分享解析
    @staticmethod
    def _parse_share(url):
        text = str(url or '').strip()
        match = _SHARE_ID_RE.search(text)
        if not match:
            return '', ''
        return match.group(1), (match.group(2) or '')

    @staticmethod
    def _share_pwd(text):
        raw = str(text or '')
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
            for key in ('pwd', 'pass_code', 'passcode', 'code'):
                if query.get(key):
                    return str(query[key][0]).strip()
        except Exception:
            pass
        match = re.search(r'(?:提取码|密码|passcode|pass_code)\s*[:：=]?\s*([A-Za-z0-9]{4,8})', raw, re.I)
        return match.group(1) if match else ''

    def _open_share(self, url):
        sid, pid = self._parse_share(url)
        if not sid:
            raise PikPakError('不是有效的 PikPak 分享链接：%s' % url)
        hit = self._share_cache.get(url)
        if hit and time.time() - hit[3] < 600 and hit[0] == sid:
            return hit[0], hit[1], hit[2]
        pwd = self._share_pwd(url)
        if pwd:
            # 带提取码：先用 /drive/v1/share 换取 pass_code_token，再走 detail。
            obj = self._api('/drive/v1/share',
                            {'share_id': sid, 'pass_code': pwd,
                             'thumbnail_size': 'SIZE_LARGE', 'limit': 100},
                            action='GET:/drive/v1/share')
            state = str(obj.get('share_status') or '')
            if state and state.upper() != 'OK':
                text = str(obj.get('share_status_text') or state)
                raise PikPakError('提取码校验失败（%s）：请检查提取码是否正确' % text)
            pct = str(obj.get('pass_code_token') or '')
            if not pct:
                raise PikPakError('提取码校验成功但没拿到 pass_code_token，请稍后重试')
            files = [f for f in (obj.get('files') or []) if isinstance(f, dict)]
            if not obj.get('next_page_token'):
                self._files_cache[(sid, pid or '', pct)] = (time.time(), files[:self.max_files])
            self._share_cache[url] = (sid, pid, pct, time.time())
            return sid, pid, pct
        # 大多数公开分享不需要先访问分享页；直接调 detail 能省 1.5~2s。
        # 需要提取码/风控的分享再回退去拿 passcode cookie。
        try:
            obj = self._share_page(sid, pid, '')
            files = [f for f in (obj.get('files') or []) if isinstance(f, dict)]
            if not obj.get('next_page_token'):
                self._files_cache[(sid, pid or '', '')] = (time.time(), files[:self.max_files])
            self._share_cache[url] = (sid, pid, '', time.time())
            return sid, pid, ''
        except RegionBlocked:
            raise
        except PikPakError as exc:
            print('[pikpak] 直连 detail 失败，回退分享页取 cookie：%s' % str(exc)[:120])
        try:
            self.jar.clear()
        except Exception:
            pass
        self._req(url, headers={'Accept': 'text/html,application/xhtml+xml'})
        pct = ''
        for cookie in self.jar:
            if 'passcode_token' in cookie.name:
                pct = urllib.parse.unquote(cookie.value or '')
                if cookie.name.endswith('_' + sid):
                    break
        self._share_cache[url] = (sid, pid, pct, time.time())
        return sid, pid, pct

    def _share_page(self, sid, pid, pct, page_token=''):
        params = {
            'limit': 100,
            'thumbnail_size': 'SIZE_LARGE',
            'order': '3',
            'folders_first': 'true',
            'share_id': sid,
            'parent_id': pid,
            'pass_code_token': pct,
        }
        if page_token:
            params['page_token'] = page_token
        obj = self._api('/drive/v1/share/detail', params,
                        action='GET:/drive/v1/share/detail')
        state = str(obj.get('share_status') or '')
        if state and state.upper() != 'OK':
            text = str(obj.get('share_status_text') or state)
            if 'PROHIBITED' in state.upper() or '地区' in text:
                raise RegionBlocked('PikPak 分享接口在当前地区不可用（%s）。'
                                    '请配置 proxy 走代理后重试，例如 '
                                    '{"proxy":"http://127.0.0.1:7890"}' % text)
            if 'PASS_CODE' in state.upper() or 'PASSCODE' in state.upper():
                raise PikPakError('这个分享需要提取码（%s）：请在链接后加 ?pwd=xxxx '
                                  '再搜，例如 https://mypikpak.com/s/xxx?pwd=abcd' % text)
            raise PikPakError('PikPak 分享不可用：%s' % text)
        return obj

    def _share_files(self, sid, pid, pct, deadline=None):
        key = (sid, pid or '', pct or '')
        hit = self._files_cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        out = []
        token = ''
        for _ in range(10):
            if deadline and time.time() > deadline:
                break
            obj = self._share_page(sid, pid, pct, token)
            files = obj.get('files') or []
            out.extend([f for f in files if isinstance(f, dict)])
            token = str(obj.get('next_page_token') or '')
            if not token or len(out) >= self.max_files:
                break
        out = out[:self.max_files]
        self._files_cache[key] = (time.time(), out)
        return out

    def _walk_share(self, sid, pid, pct, prefix='', depth=0, out=None, seen=None,
                    base_pid='', anc=None, deadline=None, state=None):
        out = [] if out is None else out
        seen = set() if seen is None else seen
        seen_videos = set()
        anc = list(anc or [])
        if state is None:
            state = {'folders': 0, 'truncated': False}
        if deadline is None:
            deadline = time.time() + self.detail_budget
        queue = [(pid, prefix, anc, depth)]
        workers = 6
        while queue:
            if time.time() > deadline or state['folders'] >= self.max_folders or len(out) >= self.max_files:
                state['truncated'] = True
                break
            batch = []
            while queue and len(batch) < workers:
                item = queue.pop(0)
                key = item[0] or 'root'
                if key in seen:
                    continue
                seen.add(key)
                batch.append(item)
            if not batch:
                continue
            if len(batch) == 1:
                results = [(batch[0], self._share_files(sid, batch[0][0], pct, deadline))]
            else:
                results = []
                with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                    futures = {pool.submit(self._share_files, sid, b[0], pct, deadline): b for b in batch}
                    for fut in as_completed(futures):
                        try:
                            results.append((futures[fut], fut.result()))
                        except Exception as exc:
                            print('[pikpak] 子目录读取失败：%s' % str(exc)[:100])
            # 按队列顺序处理结果，保证同一次展开的集顺序稳定（并发返回顺序不稳定）
            results.sort(key=lambda pair: batch.index(pair[0]))
            for (fid, pref, ancs, dep), files in results:
                for item in files:
                    if len(out) >= self.max_files or time.time() > deadline:
                        state['truncated'] = True
                        break
                    name = str(item.get('name') or '').strip() or '未命名'
                    if item.get('kind') == 'drive#folder':
                        state['folders'] += 1
                        folder_id = str(item.get('id') or '')
                        if dep < self.max_depth and state['folders'] < self.max_folders:
                            queue.append((folder_id, pref + name + '/', ancs + ([folder_id] if folder_id else []), dep + 1))
                    elif _is_video(item):
                        video_fid = str(item.get('id') or '')
                        if video_fid and video_fid in seen_videos:
                            continue
                        if video_fid:
                            seen_videos.add(video_fid)
                        out.append({
                            'name': pref + name,
                            'fid': video_fid,
                            'size': item.get('size') or 0,
                            'thumb': item.get('thumbnail_link') or '',
                            'anc': list(ancs),
                        })
                if state['truncated']:
                    break
        return out

    def _share_files_safe(self, sid, target, pct, deadline=None):
        """按目录 id 取文件；该 id 失效（404）时回退到分享根目录。"""
        try:
            return self._share_files(sid, target, pct, deadline)
        except PikPakError as exc:
            if target and '不存在' in str(exc):
                print('[pikpak] 链接里的目录不可用，回退到分享根目录')
                return self._share_files(sid, '', pct, deadline)
            raise

    def _share_root_name(self, url, budget=12):
        """取分享根目录在 PikPak 里的真实名称（用于搜索结果标题）。"""
        try:
            sid, pid, pct = self._open_share(url)
            # URL 带 /<folderid> 时，先用 file_info 取这个目录本身的名字，
            # 否则会误取成它里面第一个子目录的名字
            if pid:
                try:
                    obj = self._api('/drive/v1/share/file_info',
                                    {'share_id': sid, 'file_id': pid, 'pass_code_token': pct},
                                    action='GET:/drive/v1/share/file_info')
                    info = obj.get('file_info') if isinstance(obj, dict) else None
                    nm = str((info or {}).get('name') or '').strip()
                    if nm and nm.lower() != 'pikpak':
                        return nm
                except Exception:
                    pass
            files = self._share_files_safe(sid, pid, pct, time.time() + budget)
            # 有些分享根目录只有一个叫 "PikPak" 的壳文件夹，往里再走一层
            if len(files) == 1 and files[0].get('kind') == 'drive#folder' and str(files[0].get('name') or '').strip().lower() == 'pikpak':
                inner_fid = str(files[0].get('id') or '')
                if inner_fid:
                    files = self._share_files(sid, inner_fid, pct, time.time() + budget)
            # 优先用根目录的文件夹名
            for item in files:
                if item.get('kind') != 'drive#folder':
                    continue
                name = str(item.get('name') or '').strip()
                if name and name.lower() != 'pikpak':
                    return name
            # 没有文件夹（多文件分享）：用第一个文件名，并标注总数量
            names = []
            for item in files:
                if not _is_video(item):
                    continue
                name = str(item.get('name') or '').strip()
                if name and name.lower() != 'pikpak':
                    names.append(name)
            if not names:
                for item in files:
                    name = str(item.get('name') or '').strip()
                    if name and name.lower() != 'pikpak':
                        names.append(name)
            if names:
                first = names[0]
                return first if len(names) == 1 else ('%s 等%d个' % (first, len(names)))
        except Exception as exc:
            print('[pikpak] 取分享名称失败：%s' % str(exc)[:80])
        return ''

    def _share_medias(self, sid, fid, pct):
        obj = self._api('/drive/v1/share/file_info',
                        {'share_id': sid, 'file_id': fid, 'pass_code_token': pct},
                        action='GET:/drive/v1/share/file_info')
        state = str(obj.get('share_status') or '')
        if state and state.upper() != 'OK':
            text = str(obj.get('share_status_text') or state)
            if 'PROHIBITED' in state.upper() or '地区' in text:
                raise RegionBlocked('PikPak 分享接口在当前地区不可用（%s）。'
                                    '请配置 proxy 走代理后重试。' % text)
            raise PikPakError('PikPak 分享不可用：%s' % text)
        info = obj.get('file_info') or {}
        medias = []
        direct = str(info.get('web_content_link') or '')
        if direct:
            medias.append({'name': '原画', 'url': direct})
        for media in info.get('medias') or []:
            link = (media or {}).get('link') or {}
            url = str(link.get('url') or '')
            if url:
                medias.append({'name': str(media.get('media_name') or '原画'), 'url': url})
        return medias

    # ------------------------------------------------------------------ 账号 Token 兜底
    def _restore_share(self, sid, fid, pct, name='', anc=None):
        if not self.token:
            raise AuthRequired('未配置 PikPak Token')
        body = {
            'share_id': sid,
            'pass_code_token': pct,
            'file_ids': [fid],
            'params': {'trace_file_ids': fid},
            'ancestor_ids': [x for x in (anc or []) if x],
        }
        obj = self._api('/drive/v1/share/restore', method='POST', body=body,
                        action='POST:/drive/v1/share/restore', auth=True)
        if str(obj.get('share_status') or '') not in ('OK', ''):
            raise PikPakError('转存失败：%s' % (obj.get('share_status_text') or obj.get('share_status')))
        # 通过最近事件找到刚转存的文件
        for _ in range(4):
            events = self._api('/drive/v1/events',
                               {'thumbnail_size': 'SIZE_MEDIUM', 'limit': 100},
                               action='GET:/drive/v1/events', auth=True)
            for event in events.get('events') or []:
                file_id = str(event.get('file_id') or '')
                if not file_id:
                    continue
                if name and str(event.get('file_name') or '') not in ('', name):
                    continue
                try:
                    info = self._drive_file(file_id)
                except PikPakError:
                    continue
                url = self._drive_link(info)
                if url:
                    self._state.setdefault('created', [])
                    if file_id not in self._state['created']:
                        self._state['created'] = (self._state['created'] + [file_id])[-200:]
                        self._save_state()
                    return url
            time.sleep(1.2)
        raise PikPakError('已转存但未取到直链，请稍后重试')

    def _drive_file(self, fid):
        return self._api('/drive/v1/files/%s' % fid, {'usage': 'FETCH'},
                         action='GET:/drive/v1/files/%s' % fid, auth=True)

    @staticmethod
    def _drive_link(info):
        if not isinstance(info, dict):
            return ''
        url = str(info.get('web_content_link') or '')
        if url:
            return url
        links = info.get('links') or {}
        for key in ('application/octet-stream', 'video/mp4', 'video/x-matroska'):
            item = links.get(key) or {}
            if isinstance(item, dict) and item.get('url'):
                return str(item['url'])
        for item in links.values():
            if isinstance(item, dict) and item.get('url'):
                return str(item['url'])
        return ''

    # ------------------------------------------------------------------ 我的网盘
    def _drive_files(self, parent_id='', page_token=''):
        params = {'limit': 100, 'parent_id': parent_id, 'thumbnail_size': 'SIZE_LARGE',
                  'order': '3', 'folders_first': 'true'}
        if page_token:
            params['page_token'] = page_token
        return self._api('/drive/v1/files', params, action='GET:/drive/v1/files', auth=True)

    def _drive_list(self, parent_id=''):
        out = []
        token = ''
        for _ in range(6):
            obj = self._drive_files(parent_id, token)
            out.extend([f for f in (obj.get('files') or []) if isinstance(f, dict)])
            token = str(obj.get('next_page_token') or '')
            if not token or len(out) >= self.max_files:
                break
        return out[:self.max_files]

    def _drive_recent(self, limit=200):
        obj = self._api('/drive/v1/events', {'thumbnail_size': 'SIZE_MEDIUM', 'limit': limit},
                        action='GET:/drive/v1/events', auth=True)
        return [e for e in (obj.get('events') or []) if isinstance(e, dict)]

    # ------------------------------------------------------------------ TG 频道
    def _cache_path(self):
        base = getattr(self, '_state_path', '') or ''
        if not base:
            return ''
        return os.path.join(os.path.dirname(base), 'pikpak-tgcache.json')

    def _tg_cache_load(self):
        path = self._cache_path()
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8-sig') as fh:
                obj = json.load(fh)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _tg_cache_save(self, cache):
        path = self._cache_path()
        if not path:
            return
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(cache, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    def _tg_scan(self, channel, keyword='', pages=None, before=''):
        # before 非空时只抓「这一页」（从游标继续）；否则抓 pages 页（搜索/聚合用）
        pages = pages or self.tg_pages
        if before:
            pages = 1
        # 私密/受限频道：t.me/s/<channel> 会 302 到 t.me/<channel>（非 /s/ 预览页），拿不到公开消息。
        # 直接按「频道 username」试预览，若是公开频道 Telegram 会 200 停在 /s/ 并给出消息列表。
        cache = self._tg_cache_load()
        cache_key = '%s|%s|%d|%s' % (channel, keyword or '', pages, before)
        hit = cache.get(cache_key)
        if hit and isinstance(hit, dict) and (time.time() - hit.get('t', 0) < self.tg_cache_ttl):
            return [dict(x) for x in (hit.get('items') or [])]

        first = 'https://t.me/s/%s' % urllib.parse.quote(channel)
        if keyword:
            first += '?q=' + urllib.parse.quote(keyword)
        if before:
            first += ('&' if '?' in first else '?') + 'before=' + urllib.parse.quote(str(before))

        # 串行链式翻页：t.me/s 的下一页 URL 藏在上一页的 rel="prev" 里，没法预取。
        # 第一页顺带判断是否可预览（302 到非 /s/ = 无公开预览）。
        pages_text = []
        url = first
        for _ in range(pages):
            try:
                st, hd, dt, fn = self._req(url, headers={'Accept': 'text/html'})
            except PikPakError:
                break
            if st != 200 or not dt:
                break
            txt = dt.decode('utf-8', 'replace')
            if not pages_text and '/s/' not in str(fn or '') and 'tgme_widget_message_wrap' not in txt:
                return []  # 没有公开预览
            pages_text.append(txt)
            match = re.search(r'<link[^>]+rel="prev"[^>]+href="([^"]+)"', txt, re.I)
            if not match:
                break
            nxt = self._unescape(match.group(1))
            url = nxt if nxt.startswith('http') else 'https://t.me' + nxt
        items = []
        for t in pages_text:
            items.extend(self._tg_parse(t, channel))
        seen = set()
        out = []
        for item in items:
            if item['u'] in seen:
                continue
            seen.add(item['u'])
            out.append(item)
        cache[cache_key] = {'t': time.time(), 'items': out}
        # 记录本页 HTML 里的 prev(before) 作为下一页游标
        for txt in pages_text:
            mnext = re.search(r'<link[^>]+rel="prev"[^>]+href="([^"]+)"', txt, re.I)
            if mnext:
                nxt_url = self._unescape(mnext.group(1))
                mbefore = re.search(r'before=([^&]+)', nxt_url)
                if mbefore:
                    cursor_key = 'cursor|' + channel + '|' + str(keyword or '')
                    cursor_val = {'t': time.time(), 'before': urllib.parse.unquote(mbefore.group(1))}
                    cache[cursor_key] = cursor_val
                    if not hasattr(self, '_tg_cursors'):
                        self._tg_cursors = {}
                    self._tg_cursors[cursor_key] = cursor_val
                break
        self._tg_cache_save(cache)
        return out

    @staticmethod
    def _unescape(text):
        if _html is not None:
            try:
                return _html.unescape(text)
            except Exception:
                pass
        return (text.replace('&amp;', '&').replace('&#39;', "'")
                .replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>'))

    def _tg_parse(self, text, channel):
        out = []
        chunks = re.split(r'<div class="tgme_widget_message_wrap', text)[1:]
        for chunk in chunks:
            links = []
            for match in re.finditer(r'https?://(?:mypikpak\.com|mypikpak\.net|pikpak\.me)/s/[A-Za-z0-9_/-]+', chunk):
                link = self._unescape(match.group(0)).rstrip('.,;)')
                if link not in links:
                    links.append(link)
            if not links:
                # t.me/iv?url=https%3A%2F%2Fmypikpak.com... 这类跳转链接
                for match in re.finditer(r't\.me/iv\?url=([^"&\s]+)', chunk):
                    raw = urllib.parse.unquote(self._unescape(match.group(1)))
                    if 'mypikpak.com/s/' in raw or 'pikpak.me/s/' in raw:
                        links.append(raw)
            if not links:
                continue
            plain = re.sub(r'<br\s*/?>', ' ', chunk, flags=re.I)
            plain = re.sub(r'<[^>]+>', ' ', plain)
            plain = re.sub(r'\s+', ' ', self._unescape(plain)).strip()
            name = plain
            for link in links:
                name = name.replace(link, ' ')
            name = re.sub(r'\s+', ' ', name).strip(' -|')
            if not name:
                name = 'PikPak 分享 ' + self._parse_share(links[0])[0][:8]
            pic = ''
            photo = re.search(r"background-image:url\('([^']+)'\)", chunk)
            if photo:
                pic = self._unescape(photo.group(1))
            when = ''
            dt = re.search(r'datetime="([^"]+)"', chunk)
            if dt:
                when = dt.group(1)
            out.append({'u': links[0], 'n': name[:80], 'p': pic, 't': when, 'ch': channel})
        return out

    # ------------------------------------------------------------------ CatVod 接口
    def homeContent(self, filter):
        classes = []
        classes.append({'type_id': 'recent', 'type_name': '外部解析'})
        classes.append({'type_id': 'csv', 'type_name': 'CSV 导入'})
        if self.sheet_url:
            classes.append({'type_id': 'sheet', 'type_name': '表格资源'})
        if self.token:
            classes.append({'type_id': 'mine', 'type_name': '我的网盘'})
        for channel in self.channels:
            classes.append({'type_id': 'tg.' + channel, 'type_name': 'TG·' + channel})
        classes.append({'type_id': 'help', 'type_name': '使用说明'})
        return {'class': classes, 'filters': {}, 'list': []}

    def homeVideoContent(self):
        return {'list': []}

    def categoryContent(self, tid, pg, filter, extend):
        tid = str(tid or '')
        page = self._int(pg, 1, 1, 9999)
        try:
            if tid.startswith('n.'):
                return self._share_browse(_unpack(tid[2:]), page)
            if tid == 'help':
                items = self._help_item()
                return self._page(items, page)
            if tid == 'sheet':
                items = self._sheet_items()
                if not items:
                    return self._page([self._info_item('表格没有可解析的链接',
                                                       '检查 ext 里 sheet_url 是否公开可读、sheet_gid 是否正确。')], page)
                return self._page(items, page)
            if tid == 'recent':
                recent = self._state.get('recent') or []
                head = [{
                    'vod_id': 'recent.manage',
                    'vod_name': '🗑 管理解析记录（单条删除 / 全选）',
                    'vod_pic': '', 'vod_tag': 'folder', 'vod_remarks': '%d 条' % len(recent),
                    'vod_content': '进入后可以单条删除，或一键全选删除全部记录。',
                }]
                items = head + self._recent_items()
                return self._page(items, page)
            if tid == 'csv':
                items = self._csv_items(force_import=True)
                return self._page(items, page)
            if tid == 'recent.manage':
                recent = self._state.get('recent') or []
                items = [{
                    'vod_id': 'recent.del.all',
                    'vod_name': '☑ 全选删除（全部 %d 条）' % len(recent),
                    'vod_pic': '', 'vod_remarks': '点此确认全部删除',
                    'vod_content': '确认后清空「外部解析」里的全部记录（不影响资源站/网盘）。',
                }]
                for i, entry in enumerate(recent):
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get('n') or '') or ('PikPak 分享 ' + str(entry.get('u') or '')[:8])
                    items.append({
                        'vod_id': 'recent.del.%d' % i,
                        'vod_name': '🗑 删除：' + name,
                        'vod_pic': '', 'vod_remarks': '单条删除',
                        'vod_content': '分享链接：%s' % str(entry.get('u') or ''),
                    })
                return self._page(items, page)
            if tid == 'mine':
                if not self.token:
                    raise AuthRequired('未配置 PikPak Token')
                items = self._drive_items(self._drive_list(''), 'mine')
                if not items:
                    items = [self._info_item('网盘根目录没有视频', 'PikPak 根目录下没有可播放的视频或文件夹。')]
                return self._page(items, page)
            if tid.startswith('d.'):
                folder = tid[2:]
                obj = self._api('/drive/v1/files/%s' % folder, {'usage': 'FETCH'},
                                action='GET:/drive/v1/files/%s' % folder, auth=True)
                name = str(obj.get('name') or '文件夹')
                return self._page(self._drive_items(self._drive_list(folder), name), page)
            if tid.startswith('tg.'):
                channel = tid[3:]
                items = self._tg_items_paged(channel, page)
                if not items:
                    try:
                        st, hdrs, raw, final = self._req('https://t.me/s/%s' % urllib.parse.quote(channel),
                                                        headers={'Accept': 'text/html'})
                        previewable = '/s/' in str(final or '') or b'tgme_widget_message_wrap' in raw
                    except Exception:
                        previewable = True
                    if not previewable:
                        items = [self._info_item(
                            '频道没有公开预览，无法读取',
                            '频道 @%s 的 t.me/s/ 预览被跳转（通常是私密频道或关闭了公开历史），'
                            '源只能读公开预览页，拿不到任何消息。\n'
                            '请换一个公开频道，或直接在搜索框粘贴分享链接。' % channel)]
                    else:
                        items = [self._info_item(
                            '这个频道最近没有 PikPak 链接',
                            '频道：%s\n可以换一个频道，或直接用搜索框粘贴分享链接。' % channel)]
                # 分页已在 _tg_items_paged 内按 before 游标做好，这里不能再切一次
                return {'page': page, 'pagecount': max(2, page + 1), 'limit': 20,
                        'total': len(items), 'list': items}
        except PikPakError as exc:
            return self._page([self._error_item(str(exc))], page)
        return self._page([], page)

    def detailContent(self, ids):
        vid = ''
        if isinstance(ids, (list, tuple)) and ids:
            vid = str(ids[0] or '')
        elif isinstance(ids, str):
            vid = ids.strip()
            if vid.startswith('['):
                try:
                    arr = json.loads(vid)
                    vid = str(arr[0] or '') if arr else ''
                except Exception:
                    pass
        try:
            if vid == 'help':
                return {'list': [self._help_detail()]}
            if vid == 'csv.reimport':
                got = self._import_csv()
                if got is None:
                    return {'list': [self._info_item('没有导入新的 CSV', '已保留上次导入的列表。')]}
                return {'list': [self._info_item('已导入 %d 条 PikPak 链接' % len(got),
                                                 '返回「CSV 导入」即可看到这些分享。')]}
            if vid == 'csv.clear':
                self._state['csv'] = []
                self._save_state()
                return {'list': [self._info_item('已清空 CSV 导入', '「CSV 导入」列表已清空。')]}
            if vid == 'recent.del.all':
                n = self._forget_recent(None)
                return {'list': [self._info_item('已全选删除 %d 条记录' % n,
                                                 '「外部解析」记录已清空。')]}
            if vid.startswith('recent.del.'):
                idx = int(vid.rsplit('.', 1)[-1] or -1)
                n = self._forget_recent([idx])
                return {'list': [self._info_item('已删除 %d 条记录' % n,
                                                 '「外部解析」记录已更新。')]}
            if vid.startswith('s.'):
                return {'list': [self._share_detail(_b64d(vid[2:]))]}
            if vid.startswith('fld.'):
                return {'list': [self._folder_detail(_unpack(vid[4:]))]}
            if vid.startswith('v.'):
                return {'list': [self._share_file_detail(_unpack(vid[2:]))]}
            if vid.startswith('i.'):
                return {'list': [self._share_image_detail(_unpack(vid[2:]))]}
            if vid.startswith('d.'):
                return {'list': [self._drive_folder_detail(vid[2:])]}
            if vid.startswith('f.'):
                return {'list': [self._drive_file_detail(vid[2:])]}
        except PikPakError as exc:
            return {'list': [self._error_detail(str(exc))]}
        except Exception as exc:
            return {'list': [self._error_detail('解析失败：%s' % exc)]}
        return {'list': []}

    def searchContent(self, key, quick, pg='1'):
        text = str(key or '').strip()
        # PikPak App 的「分享码」写法：分享码：xxxx → 当分享链接处理
        code = re.search(r'分享码\s*[:：]?\s*([A-Za-z0-9_-]{12,32})', text)
        if code:
            text = 'https://mypikpak.com/s/' + code.group(1) + '\n' + text
        if not text:
            return {'list': [], 'page': 1, 'pagecount': 1, 'limit': 0, 'total': 0}
        page = self._int(pg, 1, 1, 9999)
        # 支持一次粘贴多条（比如在浏览器里 Ctrl+A / Ctrl+C 整页复制）
        all_links = re.findall(_SHARE_LINK_RE, text, re.I)
        uniq = []
        extra_pwd = self._share_pwd(text)
        for link in all_links:
            link = _normalize_share_link(link)
            if extra_pwd and 'pwd=' not in link:
                link += ('&' if '?' in link else '?') + 'pwd=' + extra_pwd
            if link not in uniq:
                uniq.append(link)
        if len(uniq) > 1:
            items = []
            for link in uniq[:40]:
                name = self._share_root_name(link, budget=5) or ('PikPak 分享 ' + self._parse_share(link)[0][:8])
                items.append({'vod_id': 'n.' + _pack({'u': link, 'fid': ''}),
                              'vod_name': name, 'vod_pic': '', 'vod_tag': 'folder',
                              'vod_remarks': '分享文件夹', 'vod_content': '分享链接：%s' % link})
            return self._page(items, page)
        find = re.search(_SHARE_LINK_RE, text, re.I)
        if not find:
            find = re.search(r'(?:mypikpak\.com|mypikpak\.net|pikpak\.me)/s/[A-Za-z0-9_/-]+', text)
        if find:
            url = _normalize_share_link(find.group(0))
            pwd = self._share_pwd(text)
            if pwd:
                sep = '&' if '?' in url else '?'
                if 'pwd=' not in url and 'pass_code=' not in url:
                    url = url + sep + 'pwd=' + urllib.parse.quote(pwd)
            real_name = self._share_root_name(url)
            title = real_name or ('PikPak 分享 ' + self._parse_share(url)[0][:8])
            item = {'vod_id': 'n.' + _pack({'u': url, 'fid': ''}),
                    'vod_name': title,
                    'vod_pic': '', 'vod_tag': 'folder', 'vod_remarks': '分享文件夹',
                    'vod_content': '分享链接：%s%s' % (url, ('\n提取码：' + pwd) if pwd else '')}
            return self._page([item], page)

        items = []
        errors = []
        for channel in self.channels:
            try:
                for it in self._tg_scan(channel, text):
                    items.append(self._tg_item(it))
            except PikPakError as exc:
                errors.append('%s：%s' % (channel, exc))
        if self.token:
            try:
                kw = text.lower()
                for item in self._drive_list(''):
                    if kw in str(item.get('name') or '').lower():
                        items.append(self._drive_item(item))
                for event in self._drive_recent():
                    name = str(event.get('file_name') or '')
                    if name and kw in name.lower() and _is_video({'name': name}):
                        items.append({
                            'vod_id': 'f.' + str(event.get('file_id') or ''),
                            'vod_name': name, 'vod_pic': event.get('thumbnail_link') or '',
                            'vod_remarks': '我的网盘·最近', 'vod_content': '来自最近上传/转存',
                        })
            except PikPakError as exc:
                errors.append('我的网盘：%s' % exc)
        if items:
            dedup = []
            seen = set()
            for item in items:
                if item['vod_id'] in seen:
                    continue
                seen.add(item['vod_id'])
                dedup.append(item)
            return self._page(dedup, page)
        # 聚合搜索时不要塞「提示卡片」，否则每个关键词都会多出一条无关结果；
        # 使用说明放在首页分类里。
        if errors:
            print('[pikpak] 搜索未命中：%s' % '；'.join(errors[:3]))
        return self._page([], page)

    def playerContent(self, flag, id, vipFlags):
        vid = str(id or '')
        if len(vid) > 2 and vid[1] == '.':
            data = _unpack(vid[2:])
        else:
            data = _unpack(vid)
        try:
            if data.get('t') == 's':
                return self._play_share(data, str(flag or ''))
            if data.get('t') == 'd':
                return self._play_drive(data)
            if vid.startswith('v.'):
                return self._play_share(_unpack(vid[2:]), str(flag or ''))
            if vid.startswith('s.'):
                return self._play_share(_unpack(vid[2:]), str(flag or ''))
            if vid.startswith('f.'):
                return self._play_drive(_unpack(vid[2:]))
        except PikPakError as exc:
            return {'parse': 0, 'playUrl': '', 'url': '', 'header': self._play_headers(),
                    'msg': str(exc)}
        except Exception as exc:
            return {'parse': 0, 'playUrl': '', 'url': '', 'header': self._play_headers(),
                    'msg': '播放失败：%s' % exc}
        return {'parse': 0, 'playUrl': '', 'url': '', 'header': self._play_headers(),
                'msg': '无法识别的 PikPak 播放标识'}

    # ------------------------------------------------------------------ 列表构造
    def _page(self, items, page):
        size = 40
        total = len(items)
        start = (page - 1) * size
        chunk = items[start:start + size]
        pages = max(1, (total + size - 1) // size)
        return {'page': page, 'pagecount': pages, 'limit': size,
                'total': total, 'list': chunk}

    def _fill_recent_names(self, budget=10.0, workers=8, limit=60):
        """给旧记录补上 PikPak 里的真实名称（并发 + 总超时），补到就写回。"""
        recent = self._state.get('recent') or []
        todo = []
        for entry in recent:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get('u') or '')
            name = str(entry.get('n') or '')
            if not url:
                continue
            if name and not name.startswith('PikPak 分享') and name.strip().lower() != 'pikpak':
                continue
            todo.append(entry)
            if len(todo) >= limit:
                break
        if not todo:
            return
        deadline = time.time() + budget
        changed = False
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._share_root_name, str(e.get('u') or ''), 5): e for e in todo}
                for fut in as_completed(futures, timeout=budget + 1):
                    if time.time() > deadline:
                        break
                    entry = futures[fut]
                    try:
                        resolved = fut.result()
                    except Exception:
                        resolved = ''
                    if resolved and resolved != entry.get('n'):
                        entry['n'] = resolved
                        changed = True
        except Exception as exc:
            print('[pikpak] 补名称超时/失败：%s' % str(exc)[:80])
        if changed:
            self._save_state()

    def _import_csv(self):
        """弹系统文件选择框（由主进程实现），把 CSV 里的 PikPak 链接读进来。"""
        try:
            req = urllib.request.Request('http://127.0.0.1:9978/import-csv', headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=300) as resp:
                obj = json.loads(resp.read().decode('utf-8', 'ignore'))
        except Exception as exc:
            print('[pikpak] 导入 CSV 失败：%s' % str(exc)[:100])
            return None
        if not obj.get('ok'):
            if obj.get('error') != 'canceled':
                print('[pikpak] 导入 CSV 返回错误：%s' % str(obj.get('error'))[:80])
            return None
        rows = []
        for it in obj.get('items') or []:
            url = str(it.get('u') or '').strip()
            if not url:
                continue
            rows.append({'u': url, 'n': str(it.get('n') or '')[:120]})
        self._state['csv'] = rows[:500]
        self._save_state()
        return rows

    def _fill_csv_names(self, budget=8.0, workers=6, limit=24):
        """CSV 里只有链接时，按 PikPak 真实名称补齐（并发 + 总超时），补到就写回。"""
        rows = self._state.get('csv') or []
        todo = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get('u') or '')
            name = str(row.get('n') or '').strip()
            if not url:
                continue
            if name and not name.startswith('PikPak 分享') and name.lower() != 'pikpak':
                continue
            todo.append(row)
            if len(todo) >= limit:
                break
        if not todo:
            return
        deadline = time.time() + budget
        changed = False
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._share_root_name, str(r.get('u') or ''), 6): r for r in todo}
                for fut in as_completed(futures, timeout=budget + 1):
                    if time.time() > deadline:
                        break
                    row = futures[fut]
                    try:
                        resolved = fut.result()
                    except Exception:
                        resolved = ''
                    if resolved and resolved != row.get('n'):
                        row['n'] = resolved
                        changed = True
        except Exception as exc:
            print('[pikpak] CSV 补名称超时/失败：%s' % str(exc)[:80])
        if changed:
            self._save_state()

    def _csv_items(self, force_import=False):
        try:
            self._fill_csv_names()
        except Exception:
            pass
        rows = self._state.get('csv') or []
        if force_import and not rows:
            rows = self._import_csv() or []
        items = [{
            'vod_id': 'csv.reimport',
            'vod_name': '📂 重新导入 CSV（选择文件）',
            'vod_pic': '', 'vod_remarks': '当前 %d 条' % len(rows),
            'vod_content': '选择一个包含 PikPak 分享链接的 CSV/文本文件，会自动提取其中的链接。',
        }]
        if not rows:
            items.append({'vod_id': 'help', 'vod_name': '还没有导入任何 CSV',
                          'vod_pic': '', 'vod_remarks': '点上面那行导入',
                          'vod_content': 'CSV 里只要有 mypikpak.com/s/... 这种链接就能识别；'
                                         '同行的其他列会被当作标题。'})
            return items
        items.append({'vod_id': 'csv.clear', 'vod_name': '🗑 清空导入（%d 条）' % len(rows),
                      'vod_pic': '', 'vod_remarks': '点此清空',
                      'vod_content': '清空后可以重新导入。'})
        for row in rows:
            url = str(row.get('u') or '')
            name = str(row.get('n') or '').strip() or ('PikPak 分享 ' + self._parse_share(url)[0][:8])
            items.append({'vod_id': 'n.' + _pack({'u': url, 'fid': ''}),
                          'vod_name': name, 'vod_pic': '', 'vod_tag': 'folder',
                          'vod_remarks': 'CSV', 'vod_content': '分享链接：%s' % url})
        return items

    def _recent_items(self):
        try:
            self._fill_recent_names()
        except Exception:
            pass
        out = []
        for entry in self._state.get('recent', [])[:120]:
            url = str(entry.get('u') or '')
            if not url:
                continue
            name = str(entry.get('n') or '').strip()
            if not name or name.lower() == 'pikpak':
                name = 'PikPak 分享 ' + self._parse_share(url)[0][:8]
            out.append({'vod_id': 'n.' + _pack({'u': url, 'fid': ''}), 'vod_name': name, 'vod_pic': '',
                        'vod_tag': 'folder', 'vod_remarks': '最近解析', 'vod_content': '分享链接：%s' % url})
        return out

    def _help_item(self):
        return [{'vod_id': 'help', 'vod_name': 'PikPak 使用说明', 'vod_pic': '',
                 'vod_remarks': '先看这里', 'vod_content': '代理、Token、TG 频道怎么配'}]

    def _help_detail(self):
        return {'vod_id': 'help', 'vod_name': 'PikPak 使用说明', 'vod_pic': '',
                'vod_content': HELP_TEXT, 'vod_remarks': '点此阅读',
                'vod_play_from': '', 'vod_play_url': ''}

    def _info_item(self, name, message):
        return {'vod_id': 'help', 'vod_name': name, 'vod_pic': '',
                'vod_remarks': '点开看说明', 'vod_content': message}

    def _error_item(self, message, name='PikPak 提示'):
        return {'vod_id': 'help', 'vod_name': name, 'vod_pic': '',
                'vod_remarks': '点开看原因', 'vod_content': message}

    def _error_detail(self, message):
        return {'vod_id': 'help', 'vod_name': 'PikPak 解析失败', 'vod_pic': '',
                'vod_content': message, 'vod_remarks': '看说明',
                'vod_play_from': '', 'vod_play_url': ''}

    def _tg_item(self, it):
        return {'vod_id': 'n.' + _pack({'u': it['u'], 'fid': ''}), 'vod_name': it.get('n') or 'PikPak 分享',
                'vod_pic': it.get('p') or '', 'vod_tag': 'folder',
                'vod_remarks': 'TG·' + str(it.get('ch') or ''),
                'vod_content': '分享链接：%s' % it['u']}

    def _tg_items(self, channel):
        return [self._tg_item(it) for it in self._tg_scan(channel)]

    def _tg_items_paged(self, channel, page):
        """按 pg 深翻：每页独立缓存 items 与下一页 before 游标，跨实例可用。"""
        page = max(1, int(page or 1))
        cache = self._tg_cache_load()
        ckey = 'page|%s|%d' % (channel, page)
        hit = cache.get(ckey)
        if hit and isinstance(hit, dict) and (time.time() - hit.get('t', 0) < self.tg_cache_ttl):
            return [self._tg_item(x) for x in (hit.get('items') or [])]
        before = ''
        if page > 1:
            prev = cache.get('page|%s|%d' % (channel, page - 1))
            if prev and isinstance(prev, dict):
                before = str(prev.get('next') or '')
            if not before:
                return [self._info_item('没有更多了 / 游标过期',
                                        '回到第 1 页重新进入，再往下翻即可。')]
        url = 'https://t.me/s/%s' % urllib.parse.quote(channel)
        if before:
            url += '?before=' + urllib.parse.quote(str(before))
        try:
            st, hd, dt, fn = self._req(url, headers={'Accept': 'text/html'})
        except Exception as exc:
            return [self._error_item('抓取失败：%s' % str(exc)[:80])]
        if st != 200 or not dt:
            return [self._info_item('没有更多了', '这个频道已经翻到底，回到第 1 页可刷新。')]
        text = dt.decode('utf-8', 'replace')
        items = self._tg_parse(text, channel)
        nxt = ''
        mnext = re.search(r'<link[^>]+rel="prev"[^>]+href="([^"]+)"', text, re.I)
        if mnext:
            mbefore = re.search(r'before=([^&]+)', self._unescape(mnext.group(1)))
            if mbefore:
                nxt = urllib.parse.unquote(mbefore.group(1))
        cache[ckey] = {'t': time.time(), 'items': items, 'next': nxt}
        self._tg_cache_save(cache)
        if not items and page > 1:
            return [self._info_item('没有更多了', '这个频道已经翻到底，回到第 1 页可刷新。')]
        return [self._tg_item(it) for it in items]

    def _drive_item(self, item):
        name = str(item.get('name') or '未命名')
        fid = str(item.get('id') or '')
        kind = str(item.get('kind') or '')
        if kind == 'drive#folder':
            return {'vod_id': 'd.' + fid, 'vod_name': name, 'vod_pic': item.get('thumbnail_link') or '',
                    'vod_tag': 'folder', 'vod_remarks': '文件夹', 'vod_content': 'PikPak 文件夹'}
        return {'vod_id': 'f.' + fid, 'vod_name': name, 'vod_pic': item.get('thumbnail_link') or '',
                'vod_remarks': _human_size(item.get('size')), 'vod_content': 'PikPak 网盘文件'}

    def _drive_items(self, files, title=''):
        out = []
        for item in files:
            if item.get('kind') == 'drive#folder':
                out.append(self._drive_item(item))
            elif _is_video(item):
                out.append(self._drive_item(item))
        return out

    # ------------------------------------------------------------------ 详情构造
    def _share_detail(self, url):
        sid, pid, pct = self._open_share(url)
        deadline = time.time() + self.detail_budget
        root = self._share_files(sid, pid, pct, deadline)
        state = {'folders': 0, 'truncated': False}
        videos = self._walk_share(sid, pid, pct, base_pid=pid, deadline=deadline, state=state)
        if not videos:
            if state.get('truncated'):
                raise PikPakError('这个分享的目录层级较深，自动展开在 %d 秒内没找到视频。'
                                  '请在网页里进入具体的分类/剧集文件夹，复制地址栏的完整链接'
                                  '（带 /<folderid> 和 ?pwd=）再搜；或在 ext 里调大 '
                                  'detail_budget / max_depth / max_folders。' % self.detail_budget)
            raise PikPakError('这个分享里没有视频文件（可能是空分享/需要提取码/已被取消）')
        videos.sort(key=lambda x: _natural_key(x['name']))
        title = ''
        if len(root) == 1 and root[0].get('kind') == 'drive#folder':
            title = str(root[0].get('name') or '')
        if not title or title.lower() == 'pikpak':
            title = 'PikPak 分享 ' + sid[:8]
        pic = ''
        for item in root:
            if item.get('thumbnail_link'):
                pic = str(item['thumbnail_link'])
                break
        flags = ['原画']
        try:
            medias = self._share_medias(sid, videos[0]['fid'], pct)
            names = []
            for media in medias:
                name = media.get('name') or '原画'
                if name not in names:
                    names.append(name)
            if names:
                flags = names
        except PikPakError:
            pass
        episodes = []
        # 同名文件（多个文件夹里都叫 1.mp4）时用相对路径，避免看起来一模一样
        bases = [v['name'].replace('\\', '/').rsplit('/', 1)[-1] for v in videos]
        dup = len(set(bases)) != len(bases)
        for video, base in zip(videos, bases):
            chain = ([pid] if pid else []) + list(video.get('anc') or [])
            payload = _pack({'t': 's', 'u': url, 'sid': sid, 'anc': chain,
                             'fid': video['fid'], 'sz': video.get('size'), 'n': video['name']})
            label = video['name'].replace('\\', '/') if dup else base
            label = label.replace('$', '＄').replace('#', '＃')
            episodes.append('%s$%s' % (label, payload))
        play_url = '$$$'.join('#'.join(episodes) for _ in flags)
        total = sum(int(v.get('size') or 0) for v in videos)
        content = ('类型：PikPak 分享\n视频：%d 个\n大小：%s\n链接：%s\n'
                   '提示：播放取的是 PikPak 直链，链接有时效；卡住就重新点一次播放。'
                   % (len(videos), _human_size(total), url))
        if self.proxy:
            content += '\n代理：已配置'
        if state.get('truncated') or len(videos) >= self.max_files:
            content += ('\n注意：分享目录较大，本次只列出前 %d 个视频（上限 %d，'
                        '可在 ext 里调大 max_files / max_depth / detail_budget）。'
                        % (len(videos), self.max_files))
        self._remember(url, title)
        return {'vod_id': 's.' + _b64e(url), 'vod_name': title, 'vod_pic': pic,
                'vod_content': content, 'vod_remarks': '%d 个视频' % len(videos),
                'vod_play_from': '$$$'.join(flags), 'vod_play_url': play_url}

    def _folder_detail(self, data):
        """把一个文件夹（含子目录）做成一个「节目」：视频列表可直接连播。"""
        url = str(data.get('u') or '')
        fid = str(data.get('fid') or '')
        folder_name = str(data.get('n') or '').strip() or '文件夹'
        if not url:
            raise PikPakError('文件夹参数缺失，请重新打开分享')
        sid, pid, pct = self._open_share(url)
        fid = fid or pid  # 可能为空 = 分享根目录
        deadline = time.time() + self.detail_budget
        state = {'folders': 0, 'truncated': False}
        videos = self._walk_share(sid, fid, pct, base_pid=fid, deadline=deadline, state=state)
        if not videos:
            imgs = []
            try:
                imgs = [f for f in self._share_files(sid, fid, pct) if _is_image(f)]
            except Exception:
                pass
            if imgs:
                return {'vod_id': 'fld.' + _pack({'u': url, 'fid': fid, 'n': folder_name}),
                        'vod_name': folder_name, 'vod_pic': str(imgs[0].get('thumbnail_link') or ''),
                        'vod_remarks': '%d 张图片' % len(imgs), 'vod_play_from': '',
                        'vod_content': '这个文件夹里是 %d 张图片，没有视频。\n'
                                       '回到上一层，点具体图片会自动打开连续浏览。' % len(imgs),
                        'vod_play_url': ''}
            return {'vod_id': 'fld.' + _pack({'u': url, 'fid': fid, 'n': folder_name}),
                    'vod_name': folder_name, 'vod_pic': '',
                    'vod_remarks': '没有视频', 'vod_play_from': '',
                    'vod_content': '这个目录（含子目录）里没有视频，只有图片或空目录。\n'
                                   '图片请回到列表里点具体图片，会自动打开连续浏览。',
                    'vod_play_url': ''}
        videos.sort(key=lambda x: _natural_key(x['name']))
        flags = ['原画']
        try:
            names = []
            for media in self._share_medias(sid, videos[0]['fid'], pct):
                name = media.get('name') or '原画'
                if name not in names:
                    names.append(name)
            if names:
                flags = names
        except PikPakError:
            pass
        bases = [v['name'].replace('\\', '/').rsplit('/', 1)[-1] for v in videos]
        dup = len(set(bases)) != len(bases)
        episodes = []
        for video, base in zip(videos, bases):
            chain = ([fid] if fid else []) + list(video.get('anc') or [])
            payload = _pack({'t': 's', 'u': url, 'sid': sid, 'anc': chain,
                             'fid': video['fid'], 'sz': video.get('size'), 'n': video['name']})
            label = video['name'].replace('\\', '/') if dup else base
            label = label.replace('$', '＄').replace('#', '＃')
            episodes.append('%s$%s' % (label, payload))
        play_url = '$$$'.join('#'.join(episodes) for _ in flags)
        total = sum(int(v.get('size') or 0) for v in videos)
        content = ('类型：PikPak 文件夹\n视频：%d 个（含子目录）\n大小：%s\n'
                   '支持连播：一集播完自动下一集。\n' % (len(videos), _human_size(total)))
        if state.get('truncated'):
            content += ('\n注意：目录较大，本次只列了前 %d 个视频；要更多请进入更下层目录。'
                        % len(videos))
        return {'vod_id': 'fld.' + _pack({'u': url, 'fid': fid, 'n': folder_name}),
                'vod_name': folder_name, 'vod_pic': videos[0].get('thumb') or '',
                'vod_content': content, 'vod_remarks': '%d 个视频' % len(videos),
                'vod_play_from': '$$$'.join(flags), 'vod_play_url': play_url}

    def _share_browse(self, data, page):
        url = str(data.get('u') or '')
        fid = str(data.get('fid') or '')
        if not url:
            return self._page([self._error_item('分享参数缺失，请重新粘贴链接搜索')], page)
        sid, pid, pct = self._open_share(url)
        target = fid or pid
        files = self._share_files_safe(sid, target, pct)
        # 根目录如果只有一个叫 "PikPak" 的壳文件夹，直接钻进去（不吞掉真正的子文件夹）
        if not fid:
            hops = 0
            while hops < 2:
                folders = [x for x in files if x.get('kind') == 'drive#folder']
                others = [x for x in files if x.get('kind') != 'drive#folder']
                if len(folders) == 1 and not others and str(folders[0].get('name') or '').strip().lower() == 'pikpak':
                    inner = str(folders[0].get('id') or '')
                    if not inner:
                        break
                    target = inner
                    files = self._share_files(sid, target, pct)
                    hops += 1
                    continue
                break
        self._remember(url, 'PikPak 分享 ' + sid[:8])
        items = []
        # 顶部固定一条「播放整个文件夹」：把本目录（含子目录）的视频做成一个可连播的节目
        self._last_folder_target = target
        items.append({
            'vod_id': 'fld.' + _pack({'u': url, 'fid': target or '',
                                      'n': ('本文件夹' if fid else '整个分享')}),
            'vod_name': '▶ 播放整个文件夹（连播）',
            'vod_pic': '', 'vod_remarks': '含子目录',
            'vod_content': '把当前目录及其子目录里的视频按顺序排成剧集，播完自动下一集。',
        })
        for it in files:
            _pre = (str(it.get('_parent') or '') + ' / ') if it.get('_parent') else ''
            if it.get('kind') == 'drive#folder':
                # 子文件夹保持独立入口（可继续往下进），连播放到文件夹内部的「播放整个文件夹」
                items.append({'vod_id': 'n.' + _pack({'u': url, 'fid': str(it.get('id') or '')}),
                              'vod_name': '📁 ' + _pre + str(it.get('name') or '文件夹'),
                              'vod_pic': it.get('thumbnail_link') or '',
                              'vod_tag': 'folder', 'vod_remarks': '文件夹',
                              'vod_content': 'PikPak 分享子文件夹'})
            elif _is_video(it):
                items.append({'vod_id': 'v.' + _pack({'u': url, 'fid': str(it.get('id') or ''),
                                                       'n': str(it.get('name') or ''),
                                                       'sz': int(it.get('size') or 0),
                                                       'pic': it.get('thumbnail_link') or '',
                                                       'anc': [str(it.get('id') or '')]}),
                              'vod_name': str(it.get('name') or '视频'),
                              'vod_pic': it.get('thumbnail_link') or '',
                              'vod_remarks': _human_size(it.get('size')),
                              'vod_content': 'PikPak 分享文件'})
            elif _is_image(it):
                ext = os.path.splitext(str(it.get('name') or ''))[1].lower().lstrip('.') or '图片'
                thumb = str(it.get('thumbnail_link') or '')
                items.append({'vod_id': 'i.' + _pack({'u': url, 'fid': str(it.get('id') or ''),
                                                      'n': str(it.get('name') or ''),
                                                      'sz': int(it.get('size') or 0),
                                                      'pdir': str(it.get('_parent_id') or target or ''),
                                                      'pic': thumb}),
                              'vod_name': str(it.get('name') or '图片'),
                              'vod_pic': ('http://127.0.0.1:9978/img?u=' +
                                          urllib.parse.quote(thumb, safe='') if thumb else ''),
                              'vod_remarks': '图片·' + ext.upper(),
                              'vod_content': 'PikPak 图片文件，点开查看大图'})
        if not items:
            # 没视频时给出文件类型统计，便于判断是空目录 / 图片包 / 压缩包 / 快捷方式
            import collections as _collections
            exts = _collections.Counter()
            for f in files:
                if f.get('kind') == 'drive#folder':
                    exts['文件夹'] += 1
                else:
                    name = str(f.get('name') or '')
                    ext = os.path.splitext(name)[1].lower() or '(无扩展名)'
                    exts[ext] += 1
            summary = '、'.join('%s×%d' % (k, v) for k, v in exts.most_common(10)) or '空目录'
            return self._page([self._info_item(
                '这个文件夹里没有视频',
                '目录内容：%s。源只列出可播放的视频文件；'
                '.url 快捷方式 / 图片 / 压缩包等需要下载到电脑本地打开。' % summary)], page)
        return self._page(items, page)

    def _open_gallery(self, url, pdir, want_name):
        """把同目录的图片打包发给 Win-Box 的内置画廊（新窗口连续浏览）。"""
        def work():
            try:
                sid, pid, pct = self._open_share(url)
                files = self._share_files(sid, pdir or pid, pct, time.time() + 15)
                imgs = [f for f in files if _is_image(f)]
                imgs.sort(key=lambda x: _natural_key(str(x.get('name') or '')))
                imgs = imgs[:80]
                results = {}

                def fetch(item):
                    fid = str(item.get('id') or '')
                    # 分享里的 download 直链要鉴权（直接 GET 会 403），缩略图才可匿名访问
                    link = str(item.get('thumbnail_link') or '')
                    return fid, link

                with ThreadPoolExecutor(max_workers=6) as pool:
                    for fid, link in pool.map(fetch, imgs):
                        results[fid] = link
                out = []
                for item in imgs:
                    link = results.get(str(item.get('id') or '')) or ''
                    if not link:
                        continue
                    thumb = str(item.get('thumbnail_link') or link)
                    out.append({
                        'n': str(item.get('name') or ''),
                        'u': 'http://127.0.0.1:9978/img?u=' + urllib.parse.quote(link, safe=''),
                        't': 'http://127.0.0.1:9978/img?u=' + urllib.parse.quote(thumb, safe=''),
                    })
                if not out:
                    return
                start = 0
                for i, x in enumerate(out):
                    if x['n'] == want_name:
                        start = i
                        break
                body = json.dumps({'title': want_name or '', 'imgs': out, 'start': start}).encode('utf-8')
                req = urllib.request.Request('http://127.0.0.1:9978/galleryopen', data=body,
                                             headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as exc:
                print('[pikpak] 打开图片浏览器失败：%s' % str(exc)[:100])

        threading.Thread(target=work, daemon=True).start()

    def _share_image_detail(self, data):
        url = str(data.get('u') or '')
        fid = str(data.get('fid') or '')
        name = str(data.get('n') or '') or 'PikPak 图片'
        size = int(data.get('sz') or 0)
        ext = os.path.splitext(name)[1].lower().lstrip('.') or 'img'
        sid, pid, pct = self._open_share(url)
        direct = ''
        try:
            for m in self._share_medias(sid, fid, pct):
                cand = str(m.get('url') or '')
                if cand:
                    direct = cand
                    break
        except PikPakError:
            pass
        thumb = str(data.get('pic') or '')
        self._open_gallery(url, str(data.get('pdir') or ''), name)
        # 统一走本机中继：/img 会带 UA 并把 PikPak 直链转成浏览器能显示的字节
        show = thumb or direct
        proxy = ('http://127.0.0.1:9978/img?u=' + urllib.parse.quote(show, safe='')) if show else ''
        content = ('类型：PikPak 图片\n格式：%s\n大小：%s\n名称：%s\n'
                   % (ext.upper(), _human_size(size), name))
        if proxy:
            content += '\n图片地址（可直接复制到浏览器打开）：\n' + proxy
            if ext in ('heic', 'heif', 'tif', 'tiff'):
                content += ('\n\n提示：%s 格式浏览器通常不能直接显示，'
                            '可把上面地址复制到系统图片查看器/转换工具打开。' % ext.upper())
        else:
            content += '\n暂时取不到图片直链（可能已失效或缺提取码）。'
        return {'vod_id': 'i.' + _pack({'u': url, 'fid': fid, 'n': name, 'sz': size, 'pic': thumb}),
                'vod_name': name, 'vod_pic': proxy,
                'vod_content': content, 'vod_remarks': '图片·' + ext.upper(),
                'vod_play_from': '', 'vod_play_url': ''}

    def _share_file_detail(self, data):
        url = str(data.get('u') or '')
        fid = str(data.get('fid') or '')
        name = str(data.get('n') or '') or 'PikPak 视频'
        size = int(data.get('sz') or 0)
        sid, pid, pct = self._open_share(url)
        flags = ['原画']
        try:
            names = []
            for m in self._share_medias(sid, fid, pct):
                n = m.get('name') or '原画'
                if n not in names:
                    names.append(n)
            if names:
                flags = names
        except PikPakError:
            pass
        payload = _pack({'t': 's', 'u': url, 'sid': sid, 'anc': [fid] if fid else [],
                         'fid': fid, 'sz': size, 'n': name})
        label = name.replace('$', '＄').replace('#', '＃')
        # 每个画质一条线路（$$$ 分隔），每条线路内只有 1 集；
        # 之前把同一 payload 在同一线路内重复 N 次，导致播放器把「原画/480P」当成了两个不同的集。
        return {'vod_id': 'v.' + _pack({'u': url, 'fid': fid, 'n': name, 'sz': size,
                                        'pic': data.get('pic') or '', 'anc': [fid]}),
                'vod_name': name, 'vod_pic': data.get('pic') or '',
                'vod_content': 'PikPak 分享文件\n大小：%s\n链接：%s' % (_human_size(size), url),
                'vod_remarks': _human_size(size),
                'vod_play_from': '$$$'.join(flags),
                'vod_play_url': '$$$'.join('%s$%s' % (label, payload) for _ in flags)}

    def _drive_folder_detail(self, folder):
        if not self.token:
            raise AuthRequired('未配置 PikPak Token')
        obj = self._api('/drive/v1/files/%s' % folder, {'usage': 'FETCH'},
                        action='GET:/drive/v1/files/%s' % folder, auth=True)
        name = str(obj.get('name') or 'PikPak 文件夹')
        files = self._drive_list(folder)
        videos = [f for f in files if _is_video(f) and f.get('kind') != 'drive#folder']
        episodes = []
        for video in videos[:self.max_files]:
            payload = _pack({'t': 'd', 'fid': str(video.get('id') or ''),
                             'sz': video.get('size'), 'n': video.get('name')})
            episodes.append('%s$%s' % (str(video.get('name') or '视频').replace('$', '＄').replace('#', '＃'),
                                       payload))
        return {'vod_id': 'd.' + folder, 'vod_name': name, 'vod_pic': '',
                'vod_content': 'PikPak 文件夹：%s\n视频：%d 个' % (name, len(videos)),
                'vod_remarks': '%d 个视频' % len(videos),
                'vod_play_from': 'PikPak' if episodes else '',
                'vod_play_url': '#'.join(episodes)}

    def _drive_file_detail(self, fid):
        if not self.token:
            raise AuthRequired('未配置 PikPak Token')
        obj = self._drive_file(fid)
        name = str(obj.get('name') or 'PikPak 文件')
        payload = _pack({'t': 'd', 'fid': fid, 'sz': obj.get('size'), 'n': name})
        return {'vod_id': 'f.' + fid, 'vod_name': name, 'vod_pic': obj.get('thumbnail_link') or '',
                'vod_content': 'PikPak 网盘文件\n大小：%s' % _human_size(obj.get('size')),
                'vod_remarks': _human_size(obj.get('size')),
                'vod_play_from': 'PikPak', 'vod_play_url': '%s$%s' % (name, payload)}

    # ------------------------------------------------------------------ 播放
    def _sheet_items(self):
        """读公开 Google Sheet（gviz CSV），每行取「描述 + PikPak 链接」生成条目；带磁盘缓存。"""
        if not self.sheet_url:
            return []
        sid_match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', self.sheet_url)
        if not sid_match:
            return []
        sid = sid_match.group(1)
        cache = self._tg_cache_load()
        key = 'sheet|%s|%s' % (sid, self.sheet_gid)
        hit = cache.get(key)
        if hit and isinstance(hit, dict) and (time.time() - hit.get('t', 0) < self.sheet_cache_ttl):
            return [dict(x) for x in (hit.get('items') or [])]
        csv_url = 'https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&gid=%s' % (sid, self.sheet_gid)
        try:
            import csv as _csv
            import io
            status, hdrs, data, final = self._req(csv_url, headers={'Accept': 'text/csv'})
            if status != 200 or not data:
                return []
            text = data.decode('utf-8-sig', 'replace')
            rows = list(_csv.reader(io.StringIO(text)))
        except Exception as exc:
            print('[pikpak] 表格读取失败：%s' % str(exc)[:160])
            return []
        items = []
        for row in rows:
            link, desc = '', ''
            for cell in row:
                cell = str(cell or '').strip()
                if not cell:
                    continue
                found = re.search(r'https?://(?:mypikpak\.com|mypikpak\.net|pikpak\.me)/s/[A-Za-z0-9_-]+', cell)
                if found and not link:
                    link = found.group(0)
                elif not desc:
                    desc = cell
            if not link:
                continue
            sid2, _ = self._parse_share(link)
            if not sid2:
                continue
            items.append({'vod_id': 'n.' + _pack({'u': link, 'fid': ''}),
                          'vod_name': desc[:80] or ('PikPak ' + sid2[:8]),
                          'vod_pic': '', 'vod_tag': 'folder', 'vod_remarks': '表格资源',
                          'vod_content': '分享链接：%s' % link})
        cache[key] = {'t': time.time(), 'items': items}
        self._tg_cache_save(cache)
        return items

    def _winbox_proxy(self):
        base = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
        for _ in range(6):
            path = os.path.join(base, 'proxy.json')
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8-sig') as fh:
                        obj = json.load(fh)
                    settings = obj.get('settings')
                    if isinstance(settings, str):
                        settings = json.loads(settings)
                    if isinstance(settings, dict):
                        return bool(settings.get('enabled')), str(settings.get('url') or '')
                except Exception:
                    return False, ''
            parent = os.path.dirname(base)
            if parent == base:
                break
            base = parent
        return False, ''

    def _play_headers(self):
        return {'User-Agent': UA, 'Referer': 'https://mypikpak.com/'}

    def _media_proxy_ok(self):
        now = time.time()
        if self._mp_alive is not None and now - self._mp_check_at < 30:
            return self._mp_alive
        ok = False
        try:
            parsed = urllib.parse.urlparse(self.media_proxy_url)
            host = parsed.hostname or '127.0.0.1'
            port = parsed.port or 5575
            sock = socket.create_connection((host, port), timeout=0.6)
            sock.close()
            ok = True
        except Exception:
            ok = False
        self._mp_alive = ok
        self._mp_check_at = now
        return ok

    def _via_media_proxy(self, url):
        """把直链套一层本机媒体代理（多线程分段，拖动更快）；服务没起就原样返回。"""
        link = str(url or '')
        if not link or not self.media_proxy:
            return link
        if not self._media_proxy_ok():
            return link
        return '%s?url=%s&thread=%d' % (self.media_proxy_url,
                                        urllib.parse.quote(link, safe=''),
                                        self.media_proxy_thread)

    def _play_share(self, data, flag):
        url = str(data.get('u') or '')
        if not url:
            raise PikPakError('播放参数缺失，请重新打开详情页')
        sid, pid, pct = self._open_share(url)
        fid = str(data.get('fid') or '')
        if not fid:
            raise PikPakError('播放参数缺少文件 ID')
        try:
            medias = self._share_medias(sid, fid, pct)
        except PikPakError:
            medias = []
        if not medias and self.token and self.use_token_restore:
            direct = self._restore_share(sid, fid, pct, str(data.get('n') or ''),
                                         data.get('anc') or [])
            return {'parse': 0, 'playUrl': '', 'url': self._via_media_proxy(direct),
                    'header': self._play_headers()}
        if not medias:
            if self.token:
                raise PikPakError('公共模式没有取到直链。可把 ext 里的 use_token_restore 设为 true，'
                                  '让源转存到你自己的网盘后再取流')
            raise PikPakError('公共模式没有取到直链。大文件通常需要配置 PikPak Token 并开启 use_token_restore')
        picked = self._pick_media(medias, flag)
        result = {'parse': 0, 'playUrl': '', 'url': self._via_media_proxy(picked),
                  'header': self._play_headers()}
        if flag == '原画':
            original = next((m for m in medias if '原画' in str(m.get('name') or '')), None)
            if original and picked and picked != original.get('url'):
                result['msg'] = '原画线路较慢，已自动切到更快的转码线路（可在 ext 里设 prefer=quality 固定原画）'
        return result

    def _play_drive(self, data):
        if not self.token:
            raise AuthRequired('未配置 PikPak Token')
        fid = str(data.get('fid') or '')
        if not fid:
            raise PikPakError('播放参数缺少文件 ID')
        info = self._drive_file(fid)
        url = self._drive_link(info)
        if not url:
            raise PikPakError('PikPak 没有返回可播放直链（可能是受保护文件或会员限制）')
        return {'parse': 0, 'playUrl': '', 'url': self._via_media_proxy(url),
                'header': self._play_headers()}

    @staticmethod
    def _quality_rank(name):
        match = re.search(r'(\d{3,4})\s*[pP]', str(name or ''))
        if match:
            return int(match.group(1))
        if '原画' in str(name or ''):
            return 9999
        return 0

    def _pick_media(self, medias, flag):
        if not medias:
            return ''
        flag = str(flag or '')
        prefer = str(self.ext.get('prefer') or 'auto').lower()
        proxy_on, _ = self._winbox_proxy()

        exact = None
        for media in medias:
            if flag and flag == media.get('name'):
                exact = media
                break

        # 用户明确选了非原画，或 prefer=quality，或没开代理：尊重选择
        if exact and (prefer == 'quality' or flag != '原画' or not proxy_on or not self.auto_quality):
            return exact['url']

        if prefer == 'quality':
            for media in medias:
                if '原画' in str(media.get('name') or ''):
                    return media['url']
            return medias[0]['url']

        if proxy_on or prefer == 'speed':
            # 代理开着时，a10b/转码线路通常比 z01a 原画快得多，选最高非原画档
            candidates = [m for m in medias if '原画' not in str(m.get('name') or '')]
            if candidates:
                best = max(candidates, key=lambda m: self._quality_rank(m.get('name')))
                return best['url']
            return medias[0]['url']

        # 没开代理：原画反而比 720P 直连快
        if exact:
            return exact['url']
        for media in medias:
            if '原画' in str(media.get('name') or ''):
                return media['url']
        return medias[0]['url']
