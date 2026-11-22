# -*- coding: utf-8 -*-
"""
磁力（BT）粘贴播放源 —— 走 Win-Box 内置的 aria2c 引擎 + /bt 边下边播。

用法：切到「磁力」源，在搜索框粘贴 magnet:?...，点开即播。
    · mp4/m4v/webm → 内置播放器直接播（/bt 中继 + piece 门控）；
    · mkv/avi/HEVC 等 → 交给配置页「磁力外部播放器」（PotPlayer/VLC/mpv 自动探测）。
只做「把 magnet 交回播放器」这一件事，真正的下载/取流由 Win-Box 主进程完成。
"""
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
try:
    import html as _html
except Exception:
    _html = None

_MAGNET_RE = re.compile(r'[?&]dn=([^&]+)', re.I)
_BTIH_RE = re.compile(r'btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})', re.I)


def _b64e(text):
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii').rstrip('=')


def _b64d(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4)).decode('utf-8', 'replace')


# ------------------------------------------------------------------ torrent 解析
def _bdecode(data, pos=0):
    """极简 bencode 解码：返回 (value, next_pos)，key/bytes 保持 bytes。"""
    c = data[pos:pos + 1]
    if c == b'i':
        end = data.index(b'e', pos + 1)
        return int(data[pos + 1:end]), end + 1
    if c == b'l':
        pos += 1
        out = []
        while data[pos:pos + 1] != b'e':
            value, pos = _bdecode(data, pos)
            out.append(value)
        return out, pos + 1
    if c == b'd':
        pos += 1
        out = {}
        while data[pos:pos + 1] != b'e':
            key, pos = _bdecode(data, pos)
            value, pos = _bdecode(data, pos)
            out[key] = value
        return out, pos + 1
    match = re.match(rb'(\d+):', data[pos:])
    if not match:
        raise ValueError('torrent 格式错误（bencode 位置 %d）' % pos)
    size = int(match.group(1))
    start = pos + match.end()
    return data[start:start + size], start + size


def _text_of(value):
    if isinstance(value, bytes):
        for enc in ('utf-8', 'gbk', 'latin-1'):
            try:
                return value.decode(enc).strip()
            except UnicodeDecodeError:
                pass
    return str(value or '').strip()


def _torrent_meta(raw):
    """从 .torrent 原始字节提取 (infoHash, 名称, tracker 列表)。"""
    if not raw or raw[:1] != b'd':
        raise ValueError('不是有效的 .torrent 文件')
    meta, _ = _bdecode(raw)
    if not isinstance(meta, dict) or b'info' not in meta:
        raise ValueError('torrent 缺少 info 字段')
    # infoHash = SHA1(info 字段的原始 bencode 字节)
    pos, info_raw = 1, None
    while pos < len(raw) and raw[pos:pos + 1] != b'e':
        key, pos = _bdecode(raw, pos)
        value_start = pos
        _, pos = _bdecode(raw, pos)
        if key == b'info':
            info_raw = raw[value_start:pos]
            break
    if not info_raw:
        raise ValueError('torrent 的 info 字段无法定位')
    info_hash = hashlib.sha1(info_raw).hexdigest()
    info = meta.get(b'info') or {}
    name = _text_of(info.get(b'name')) if isinstance(info, dict) else ''
    trackers = []
    announce = _text_of(meta.get(b'announce'))
    if announce:
        trackers.append(announce)
    for tier in (meta.get(b'announce-list') or []):
        if not isinstance(tier, (list, tuple)):
            continue
        for item in tier:
            tracker = _text_of(item)
            if tracker and tracker not in trackers:
                trackers.append(tracker)
    return info_hash, name, trackers


def _torrent_to_magnet(raw):
    info_hash, name, trackers = _torrent_meta(raw)
    parts = ['xt=urn:btih:' + info_hash]
    if name:
        parts.append('dn=' + urllib.parse.quote(name))
    for tracker in trackers[:12]:
        parts.append('tr=' + urllib.parse.quote(tracker, safe=''))
    return 'magnet:?' + '&'.join(parts)


def _b32_to_hex(b32):
    """base32 的 btih（32 字符）→ 40 位十六进制；失败返回空串。"""
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    bits = ''
    for ch in str(b32 or '').upper():
        v = alphabet.find(ch)
        if v < 0:
            return ''
        bits += format(v, '05b')
    out = ''
    for i in range(0, 160 - 3, 4):
        out += format(int(bits[i:i + 4], 2), 'x')
    return out if len(out) == 40 else ''


def _bt_cache_dirs():
    """可能存放 aria2 元数据（<infohash>.torrent）的目录。

    - 桌面：<data>/imports/py/<uuid>/magnet.py → 上溯 3 层是 <data>，缓存是 <data>/winbox-cache/bt
    - 手机：源被加载到应用私有目录，缓存通常是同级的 bt/ 或 app_flutter/bt
    只做「存在即用」，全都不存在就当没有，不影响其它逻辑。
    """
    roots = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        here = ''
    cur = here
    for _ in range(5):
        if not cur:
            break
        roots.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    outs = []
    for r in roots:
        for sub in ('bt', 'winbox-cache/bt', 'files/bt', 'app_flutter/bt', 'cache/bt'):
            p = os.path.join(r, *sub.split('/'))
            if p not in outs:
                outs.append(p)
    return outs


_VIDEO_EXTS = {'mp4', 'm4v', 'mkv', 'avi', 'ts', 'm2ts', 'mov', 'webm', 'flv', 'wmv', 'mpg', 'mpeg', 'rmvb'}


def _ext_of(name):
    base = str(name or '').split('/')[-1].split('\\')[-1]
    dot = base.rfind('.')
    return base[dot + 1:].lower() if dot > 0 else ''


def _is_video_file(name):
    return _ext_of(name) in _VIDEO_EXTS


def _stem(name):
    base = str(name or '').split('/')[-1].split('\\')[-1]
    ext = _ext_of(base)
    return base[:len(base) - len(ext) - 1] if ext else base


def _natural_key(name):
    """EP2 排在 EP10 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(name or ''))]


def _app_resolve(magnet, wait_ms=1500):
    """让客户端只解析元数据（不播放），返回 {'name','files'} 或 None。

    客户端有个 /bt/resolve 接口：丢磁链进去，它拿元数据、落 <infohash>.torrent 再回文件清单。
    wait_ms=0 表示只下单不等，让它后台跑。
    """
    base = os.environ.get('WINBOX_PLAYER_BASE') or 'http://127.0.0.1:9978'
    url = base + '/bt/resolve?' + urllib.parse.urlencode({'magnet': magnet, 'wait': int(wait_ms)})
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'winbox-magnet/1'})
        # 本机直连，绕开系统代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=max(3.0, wait_ms / 1000.0 + 2.5)) as resp:
            obj = json.loads(resp.read().decode('utf-8', 'replace'))
        if isinstance(obj, dict) and obj.get('ok') and obj.get('ready'):
            return {'name': obj.get('name') or '',
                    'files': [(str(f.get('path') or ''), int(f.get('length') or 0))
                              for f in (obj.get('files') or []) if isinstance(f, dict)]}
    except Exception:
        return None
    return None


def _humansize(num):
    try:
        n = float(num)
    except Exception:
        return '-'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return ('%d %s' % (n, unit)) if unit in ('B', 'KB') else ('%.2f %s' % (n, unit))
        n /= 1024.0
    return '%.2f TB' % n


def _torrent_files(raw):
    """从种子字节取出文件清单：[(路径, 字节)]。单文件种子返回一条。"""
    meta, _ = _bdecode(raw)
    if not isinstance(meta, dict):
        return []
    info = meta.get(b'info') or {}
    if not isinstance(info, dict):
        return []
    name = _text_of(info.get(b'name'))
    out = []
    files = info.get(b'files')
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            try:
                length = int(item.get(b'length') or 0)
            except Exception:
                length = 0
            parts = [_text_of(x) for x in (item.get(b'path') or [])]
            rel = '/'.join([p for p in parts if p])
            out.append((rel or name, length))
    else:
        try:
            length = int(info.get(b'length') or 0)
        except Exception:
            length = 0
        if length:
            out.append((name, length))
    return out


def _cached_torrent_path(info_hash):
    """在同一次播放之后，aria2 会把 <infohash>.torrent 落到 bt 缓存里。"""
    ih = str(info_hash or '').lower()
    if len(ih) == 32:
        ih = _b32_to_hex(ih)
    if len(ih) != 40:
        return ''
    for d in _bt_cache_dirs():
        for name in (ih, ih.upper()):
            p = os.path.join(d, name + '.torrent')
            if os.path.isfile(p):
                return p
    return ''


def _cached_torrent_name(info_hash):
    """用它反查真实片名：裸磁链没有 dn=，这是唯一可靠且不依赖外网的来源。"""
    path = _cached_torrent_path(info_hash)
    if not path:
        return ''
    try:
        with open(path, 'rb') as fh:
            _hash, title, _tr = _torrent_meta(fh.read())
        return (title or '').strip()
    except Exception:
        return ''


def _cached_torrent_files(info_hash):
    path = _cached_torrent_path(info_hash)
    if not path:
        return []
    try:
        with open(path, 'rb') as fh:
            return _torrent_files(fh.read())
    except Exception:
        return []


def _read_torrent(text):
    """输入 -> torrent 原始字节：支持本地路径 / file:// / http(s) URL。"""
    text = str(text or '').strip().strip('"').strip("'")
    if re.match(r'^https?://', text, re.I):
        req = urllib.request.Request(text, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read()
    if text.lower().startswith('file:///'):
        text = urllib.parse.unquote(text[8:])
    if not os.path.isfile(text):
        raise ValueError('找不到 torrent 文件：%s' % text)
    with open(text, 'rb') as fh:
        return fh.read()


# ------------------------------------------------------------------ 网络抓取（TG 频道 / 资源站）
def _unescape(text):
    if _html is not None:
        try:
            return _html.unescape(text)
        except Exception:
            pass
    return (text.replace('&amp;', '&').replace('&#39;', "'")
            .replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>'))


def _magnets_in(text):
    """从文本/HTML 抓 magnet:?xt= 链接（兼容 ? 被转义成 &#63; / &quest;）。"""
    out = []
    pattern = (r'magnet:(?:\?|&#63;|&quest;|%3F)xt=urn:btih:'
               r'[0-9a-fA-F]{40}|magnet:(?:\?|&#63;|&quest;|%3F)xt=urn:btih:[A-Za-z2-7]{32}')
    for match in re.finditer(pattern, text, re.I):
        url = _unescape(match.group(0)).replace('&#63;', '?').replace('%3F', '?').replace('&quest;', '?')
        tail = re.match(r'[^"\s<>]+', text[match.end():])
        if tail:
            url += _unescape(tail.group(0))
        url = url.rstrip('.,;)')
        if url not in out:
            out.append(url)
    return out


def _title_near(text, pos, fallback=''):
    seg = _unescape(text[max(0, pos - 400):pos])
    seg = re.sub(r'<br\\s*/?>', ' ', seg, flags=re.I)
    seg = re.sub(r'<[^>]+>', ' ', seg)
    seg = re.sub(r'\\s+', ' ', seg).strip()
    return seg[-70:].strip() or fallback


class Spider(object):
    def __init__(self):
        self.ext = {}
        self.proxy = ''
        self.channels = []
        self.site_urls = []
        self.tg_pages = 2
        self.timeout = 20
        self._cache = {}
        self._opener = None
        self._state = {'recent': []}
        self._state_path = ''
        self.remember_recent = True

    def init(self, ext):
        # runner 可能传 JSON 字符串（常驻模式 rest[0] 是 str），也可能是 dict
        if isinstance(ext, str):
            try:
                ext = json.loads(ext)
            except Exception:
                ext = {}
        self.ext = ext or {}
        self.proxy = str(self.ext.get('proxy') or '').strip()
        self.channels = self._parse_list(self.ext.get('channels'))
        self.site_urls = self._parse_list(self.ext.get('site_urls') or self.ext.get('site_url'))
        self.tg_pages = max(1, int(self.ext.get('tg_pages') or 2))
        self.timeout = int(self.ext.get('timeout') or 20)
        self.remember_recent = str(self.ext.get('remember', '1')).strip().lower() not in ('0', 'false', 'no', 'off')
        self._load_state()

    @staticmethod
    def _parse_list(value):
        if isinstance(value, str):
            parts = re.split(r'[,，&|\\s]+', value)
        elif isinstance(value, (list, tuple)):
            parts = [str(x) for x in value]
        else:
            parts = []
        out = []
        for part in parts:
            name = str(part).strip()
            if not name:
                continue
            name = re.sub(r'^https?://t\\.me/(s/)?', '', name, flags=re.I).lstrip('@').strip('/')
            if name and name not in out:
                out.append(name)
        return out

    def _build_opener(self):
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({'http': self.proxy, 'https': self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._opener = urllib.request.build_opener(*handlers)

    def _fetch(self, url):
        if self._opener is None:
            self._build_opener()
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36',
            'Accept': 'text/html,text/plain,*/*'})
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def _cached(self, key, ttl, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = fn()
        except Exception as exc:
            print('[magnet] 抓取失败 %s: %s' % (key, str(exc)[:120]))
            return []
        self._cache[key] = (time.time(), value)
        return value

    def _tg_scan(self, channel):
        def load():
            out = []
            url = 'https://t.me/s/%s' % urllib.parse.quote(channel)
            for _ in range(self.tg_pages):
                text = self._fetch(url).decode('utf-8', 'replace')
                if 'tgme_widget_message_wrap' not in text and '/s/' not in url:
                    break
                for mg in _magnets_in(text):
                    out.append({'u': mg, 'n': _title_near(text, text.find(mg[:60])), 'ch': channel})
                match = re.search(r'<link[^>]+rel="prev"[^>]+href="([^"]+)"', text, re.I)
                if not match:
                    break
                nxt = _unescape(match.group(1))
                url = nxt if nxt.startswith('http') else 'https://t.me' + nxt
            seen, uniq = set(), []
            for item in out:
                if item['u'] in seen:
                    continue
                seen.add(item['u'])
                uniq.append(item)
            return uniq
        return self._cached('tg.' + channel, 300, load)

    def _site_scan(self, url):
        def load():
            text = self._fetch(url).decode('utf-8', 'replace')
            out = []
            for mg in _magnets_in(text):
                out.append({'u': mg, 'n': _title_near(text, text.find(mg[:60])), 'ch': '资源站'})
            seen, uniq = set(), []
            for item in out:
                if item['u'] in seen:
                    continue
                seen.add(item['u'])
                uniq.append(item)
            return uniq
        return self._cached('site.' + url, 300, load)

    # ------------------------------------------------------------------ 记录（与 PikPak 源对齐）
    def _state_base(self):
        try:
            return os.path.dirname(os.path.abspath(__file__))
        except Exception:
            return os.getcwd()

    def _load_state(self):
        path = os.path.join(self._state_base(), 'magnet-state.json')
        legacy = os.path.join(tempfile.gettempdir(), 'winbox-magnet-state.json')
        try:
            writable = os.access(os.path.dirname(path), os.W_OK)
        except Exception:
            writable = False
        self._state_path = path if writable else legacy
        try:
            if os.path.isfile(self._state_path):
                with open(self._state_path, 'r', encoding='utf-8-sig') as fh:
                    obj = json.load(fh)
                if isinstance(obj, dict):
                    recent = obj.get('recent')
                    self._state['recent'] = recent if isinstance(recent, list) else []
        except Exception as exc:
            print('[magnet] 读取解析记录失败：%s' % str(exc)[:80])

    def _save_state(self):
        if not self._state_path:
            return
        try:
            with open(self._state_path, 'w', encoding='utf-8') as fh:
                json.dump(self._state, fh, ensure_ascii=False)
        except Exception as exc:
            print('[magnet] 保存解析记录失败：%s' % str(exc)[:80])

    @staticmethod
    def _is_placeholder_name(name):
        text = str(name or '').strip()
        return (not text) or text == '磁力链接' or text.startswith('磁力 ')

    def _remember(self, magnet, name=None):
        """记一条解析记录：同一磁链只保留一条，占位名不覆盖真实名。"""
        if not self.remember_recent or not magnet:
            return
        recent = [x for x in (self._state.get('recent') or []) if isinstance(x, dict)]
        old_name = ''
        for item in recent:
            if item.get('u') == magnet:
                old_name = str(item.get('n') or '')
                break
        new_name = str(name or '').strip() or self._name(magnet)
        if self._is_placeholder_name(new_name) and old_name and not self._is_placeholder_name(old_name):
            new_name = old_name
        kept = [x for x in recent if x.get('u') != magnet]
        kept.insert(0, {'u': magnet, 'n': new_name, 't': int(time.time())})
        self._state['recent'] = kept[:60]
        self._save_state()

    def _forget_recent(self, indexes=None):
        recent = [x for x in (self._state.get('recent') or []) if isinstance(x, dict)]
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

    @staticmethod
    def _info_item(name, content, remarks='', vid='help'):
        return {'vod_id': vid, 'vod_name': name, 'vod_pic': '',
                'vod_remarks': remarks, 'vod_content': content}

    def _recent_items(self):
        out = []
        dirty = False
        for entry in (self._state.get('recent') or []):
            if not isinstance(entry, dict):
                continue
            magnet = str(entry.get('u') or '')
            if not magnet:
                continue
            name = str(entry.get('n') or '')
            # 上次存的是占位名：现在缓存里可能已经有真实片名了，顺手升级并落盘
            if self._is_placeholder_name(name):
                better = self._name(magnet)
                if better and not self._is_placeholder_name(better):
                    name = better
                    entry['n'] = better
                    dirty = True
            if not name:
                name = self._name(magnet)
            ts = int(entry.get('t') or 0)
            when = time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else ''
            out.append({
                'vod_id': 'n.' + _b64e(magnet),
                'vod_name': name[:80],
                'vod_pic': '',
                'vod_remarks': (when + ' · 磁力') if when else '磁力',
                'vod_content': '磁力链接：\n' + magnet,
            })
        if dirty:
            self._save_state()
        return out

    @staticmethod
    def _cache_torrent(raw):
        """种子落本地缓存（<infohash>.torrent），供下次取片名、免重复解析。"""
        try:
            info_hash, _name, _tr = _torrent_meta(raw)
        except Exception:
            return ''
        if len(info_hash) != 40:
            return ''
        for d in _bt_cache_dirs():
            if not os.path.isdir(d):
                continue
            try:
                path = os.path.join(d, info_hash.lower() + '.torrent')
                if os.path.isfile(path):
                    return path
                with open(path, 'wb') as fh:
                    fh.write(raw)
                return path
            except Exception:
                continue
        return ''

    # ------------------------------------------------------------------ CatVod
    def homeContent(self, filter):
        classes = [{'type_id': 'recent', 'type_name': '最近解析'},
                   {'type_id': 'help', 'type_name': '使用说明'}]
        for ch in self.channels:
            classes.insert(0, {'type_id': 'tg.' + ch, 'type_name': 'TG·' + ch})
        for i, u in enumerate(self.site_urls):
            classes.insert(0, {'type_id': 'site.%d' % i, 'type_name': '资源站%d' % (i + 1)})
        return {
            'class': classes,
            'filters': {},
            'list': [{
                'vod_id': 'help',
                'vod_name': '磁力（BT）播放',
                'vod_pic': '',
                'vod_remarks': '磁力 / 种子',
                'vod_content': '搜索框粘贴 magnet:?... 或 .torrent（本地路径 / file:// / http(s) URL）'
                               '即可边下边播。\n种子会解析出 infoHash 与 tracker 后转磁力，'
                               'mp4/webm 内置直接播；mkv/HEVC 走「磁力外部播放器」。',
            }],
        }

    # ------------------------------------------------------------------ CatVod 分类/搜索
    def _page(self, items, page):
        size = 40
        start = (int(page or 1) - 1) * size
        return {'page': int(page or 1), 'pagecount': max(1, (len(items) + size - 1) // size),
                'limit': size, 'total': len(items), 'list': items[start:start + size]}

    def _link_item(self, it, source):
        magnet = str(it.get('u') or '')
        # 优先用 magnet 里的 dn=（资源名），再退回附近文字 / infoHash
        name = self._name(magnet)
        if name.startswith('磁力 '):
            near = str(it.get('n') or '').strip()
            name = near if near and len(near) > 3 else name
        return {'vod_id': 'm.' + _b64e(magnet),
                'vod_name': name[:80],
                'vod_pic': '', 'vod_remarks': source,
                'vod_content': '磁力链接：\n' + magnet}

    def categoryContent(self, tid, pg, filter, extend):
        tid = str(tid or '')
        page = int(pg or 1)
        items = []
        if tid == 'recent':
            recent = [x for x in (self._state.get('recent') or []) if isinstance(x, dict)]
            head = self._info_item('🗑 管理解析记录（单条删除 / 全选）',
                                   '进入后可以单条删除，或一键全选删除全部记录。',
                                   '%d 条' % len(recent), 'recent.manage')
            head['vod_tag'] = 'folder'
            return self._page([head] + self._recent_items(), page)
        if tid == 'recent.manage':
            recent = [x for x in (self._state.get('recent') or []) if isinstance(x, dict)]
            rows = [self._info_item('☑ 全选删除（全部 %d 条）' % len(recent),
                                    '确认后清空「最近解析」的全部记录，不影响已下载的文件。',
                                    '点此确认全部删除', 'recent.del.all')]
            for i, entry in enumerate(recent):
                magnet = str(entry.get('u') or '')
                name = str(entry.get('n') or '') or self._name(magnet)
                rows.append(self._info_item('🗑 删除：' + name[:60],
                                            '磁力链接：%s' % magnet,
                                            '单条删除', 'recent.del.%d' % i))
            return self._page(rows, page)
        if tid.startswith('tg.'):
            for it in self._tg_scan(tid[3:]):
                items.append(self._link_item(it, 'TG·' + tid[3:]))
        elif tid.startswith('site.'):
            idx = int(tid[5:] or 0)
            if 0 <= idx < len(self.site_urls):
                for it in self._site_scan(self.site_urls[idx]):
                    items.append(self._link_item(it, '资源站%d' % (idx + 1)))
        elif tid == 'help':
            return self._page([{'vod_id': 'help', 'vod_name': '磁力（BT）播放', 'vod_pic': '',
                                'vod_remarks': '使用说明', 'vod_content': '搜索框粘贴 magnet 或 .torrent；'
                                '也可在首页进 TG·频道 / 资源站分类。'}], page)
        return self._page(items, page)

    def _name(self, magnet):
        m = _MAGNET_RE.search(magnet)
        if m:
            name = urllib.parse.unquote(m.group(1)).strip()
            if name:
                return name
        m = _BTIH_RE.search(magnet)
        if not m:
            return '磁力链接'
        # 裸磁链没有 dn=：优先用 aria2 落地的 <infohash>.torrent 反查真实片名
        cached = _cached_torrent_name(m.group(1))
        if cached:
            return cached
        return '磁力 ' + m.group(1)[:8]

    def searchContent(self, key, quick, pg='1'):
        raw_key = str(key or '').strip()
        key = raw_key
        source = '磁力链接'
        if not key.lower().startswith('magnet:'):
            # 本地 .torrent / file:// / http(s) 种子的尾巴
            probe = urllib.parse.urlparse(key).path if re.match(r'^https?://', key, re.I) else key
            if not probe.lower().endswith('.torrent'):
                # 非磁力/种子输入 → 在已配置的频道/站点里按关键词过滤
                kw = raw_key.lower()
                matched = [x for x in self._all_import_items()
                           if kw in (x['vod_name'] + ' ' + x['vod_content']).lower()]
                return self._page(matched, pg)
            try:
                raw = _read_torrent(key)
                self._cache_torrent(raw)
                key = _torrent_to_magnet(raw)
                source = '.torrent 种子'
            except Exception as exc:
                return {'list': [self._error_item('种子解析失败：%s' % exc)],
                        'page': 1, 'pagecount': 1, 'limit': 1, 'total': 1}
        item = {'vod_id': 'm.' + _b64e(key),
                'vod_name': self._name(key),
                'vod_pic': '', 'vod_remarks': source,
                'vod_content': '%s：\n%s' % (source, key)}
        self._remember(key, item['vod_name'])
        if key.lower().startswith('magnet:'):
            # 先让客户端把元数据解析任务挂上（不等结果），点进详情时往往已经有文件清单
            # 已有本地种子的不要重复下单：同一 infohash 注册两次会让 aria2 报 12，播放直接失败
            hit = _BTIH_RE.search(key)
            if not (hit and _cached_torrent_path(hit.group(1))):
                _app_resolve(key, 0)
        return {'list': [item], 'page': 1, 'pagecount': 1, 'limit': 1, 'total': 1}

    def _all_import_items(self):
        """搜索时汇总所有已配置频道/站点的磁力条目。"""
        out = []
        for ch in self.channels:
            for it in self._tg_scan(ch):
                out.append(self._link_item(it, 'TG·' + ch))
        for i, url in enumerate(self.site_urls):
            for it in self._site_scan(url):
                out.append(self._link_item(it, '资源站%d' % (i + 1)))
        seen = set()
        uniq = []
        for x in out:
            if x['vod_id'] in seen:
                continue
            seen.add(x['vod_id'])
            uniq.append(x)
        return uniq

    @staticmethod
    def _error_item(message):
        return {'vod_id': 'help', 'vod_name': '种子解析失败', 'vod_pic': '',
                'vod_content': message, 'vod_remarks': '看说明'}

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
        if vid == 'recent.del.all':
            n = self._forget_recent(None)
            return {'list': [self._info_item('已全选删除 %d 条记录' % n,
                                             '「最近解析」列表已清空，已下载的文件不受影响。', '完成')]}
        if vid.startswith('recent.del.'):
            try:
                idx = int(vid.rsplit('.', 1)[-1])
            except Exception:
                idx = -1
            n = self._forget_recent([idx])
            return {'list': [self._info_item('已删除 %d 条记录' % n,
                                             '「最近解析」列表已更新。', '完成')]}
        if vid.startswith('n.'):
            vid = 'm.' + vid[2:]
        if not (isinstance(vid, str) and vid.startswith('m.')):
            return {'list': []}
        magnet = _b64d(vid[2:])
        name = self._name(magnet)
        self._remember(magnet, name)
        content = '磁力链接：\n' + magnet
        hit = _BTIH_RE.search(magnet)
        files = _cached_torrent_files(hit.group(1)) if hit else []
        if not files and hit:
            # 本地没缓存种子：让客户端先解析（只取元数据，不播放），拿不到就提示解析中
            resolved = _app_resolve(magnet, 1500)
            files = _cached_torrent_files(hit.group(1))
            if not files and resolved:
                files = resolved['files']
                if resolved['name'] and self._is_placeholder_name(name):
                    name = resolved['name']
                    self._remember(magnet, name)
        videos = [(p, n) for p, n in files if _is_video_file(p)] if files else []
        if len(videos) > 1:
            videos.sort(key=lambda x: _natural_key(x[0]))
            eps = []
            for idx, (vp, _vn) in enumerate(videos[:200], 1):
                label = _stem(vp) or ('第%d个视频' % idx)
                eps.append(label.replace('$', '＄').replace('#', '＃') + '$' + magnet)
            play_url = '#'.join(eps)
            remarks = '%d 集' % min(len(videos), 200)
        else:
            play_url = name.replace('$', '＄').replace('#', '＃') + '$' + magnet
            remarks = ('%d 个文件' % len(files)) if files else '边下边播'
        if files:
            total = sum(n for _p, n in files)
            rows = []
            for i, (fp, fn) in enumerate(files[:20], 1):
                rows.append('%2d. %s（%s）' % (i, fp[:70], _humansize(fn)))
            if len(files) > 20:
                rows.append('… 其余 %d 个文件' % (len(files) - 20))
            title = '【已解析】'
            if len(videos) > 1:
                title += '%d 个视频（按集列出）/ ' % len(videos)
            content += ('\n\n%s%d 个文件，合计 %s\n%s'
                        % (title, len(files), _humansize(total), '\n'.join(rows)))
        else:
            content += ('\n\n【未解析】本地还没有这个磁链的种子元数据。'
                        '点开播放时才去网络要，冷门种子可能要几十秒甚至超时。')
        content += ('\n\n提示：一集对应一个文件，点哪集就只下哪个文件；'
                    'mp4/webm 内置直接播，mkv/HEVC 走「磁力外部播放器」。')
        return {'list': [{
            'vod_id': vid, 'vod_name': name, 'vod_pic': '',
            'vod_content': content,
            'vod_remarks': remarks,
            'vod_play_from': '磁力',
            'vod_play_url': play_url,
        }]}

    def playerContent(self, flag, id, vipFlags):
        vid = str(id or '')
        magnet = _b64d(vid[2:]) if vid.startswith('m.') else vid
        return {'parse': 0, 'playUrl': '', 'url': magnet, 'header': {}}
