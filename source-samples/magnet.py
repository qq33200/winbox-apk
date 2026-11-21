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

    # ------------------------------------------------------------------ CatVod
    def homeContent(self, filter):
        classes = [{'type_id': 'help', 'type_name': '使用说明'}]
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
            return urllib.parse.unquote(m.group(1)).strip() or '磁力链接'
        m = _BTIH_RE.search(magnet)
        return ('磁力 ' + m.group(1)[:8]) if m else '磁力链接'

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
                key = _torrent_to_magnet(_read_torrent(key))
                source = '.torrent 种子'
            except Exception as exc:
                return {'list': [self._error_item('种子解析失败：%s' % exc)],
                        'page': 1, 'pagecount': 1, 'limit': 1, 'total': 1}
        item = {'vod_id': 'm.' + _b64e(key),
                'vod_name': self._name(key),
                'vod_pic': '', 'vod_remarks': source,
                'vod_content': '%s：\n%s' % (source, key)}
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
        if not (isinstance(vid, str) and vid.startswith('m.')):
            return {'list': []}
        magnet = _b64d(vid[2:])
        name = self._name(magnet)
        return {'list': [{
            'vod_id': vid, 'vod_name': name, 'vod_pic': '',
            'vod_content': '磁力链接：\n' + magnet + '\n\n提示：第一次点开要等种子元数据与文件头落盘，'
                           '冷门种子可能几十秒；做种者过少会提示缓冲超时。',
            'vod_remarks': '边下边播',
            'vod_play_from': '磁力',
            'vod_play_url': name.replace('$', '＄').replace('#', '＃') + '$' + magnet,
        }]}

    def playerContent(self, flag, id, vipFlags):
        vid = str(id or '')
        magnet = _b64d(vid[2:]) if vid.startswith('m.') else vid
        return {'parse': 0, 'playUrl': '', 'url': magnet, 'header': {}}
