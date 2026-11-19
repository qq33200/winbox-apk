# -*- coding: utf-8 -*-
"""
Win-Box 封面自适应服务
================================================================
py 源返回的 vod_pic 大多是竖版海报（2:3）。本服务统一处理成 16:9 横封面：

  - 原图本来就是横的（宽高比 >= 1.2）→ 原样返回，不做任何处理
  - 竖图 → 居中偏上裁一条 16:9 横带（保住人物主体，避开底部大字）
  - 取图失败 → 回 1x1 透明 PNG，绝不回 JSON（图片控件收到非图片会崩）

只处理封面，不碰播放正文。带内存缓存，同一张图只下载一次。

监听 127.0.0.1:9978，由 runner.py 在首次取数时自动拉起。
"""
import base64
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PY_DIR = r'D:/TVbox/Win-Box/data/winbox-cache/python/3.11.6'
# ★ embed 版 Python 的 _pth 是隔离的：必须先注入 site-packages，再 import PIL
for _p in (PY_DIR, os.path.join(PY_DIR, 'Lib'),
           os.path.join(PY_DIR, 'Lib', 'site-packages')):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from PIL import Image  # noqa: E402  启动时就加载，缺库立刻暴露
LOG = os.path.join(HERE, 'crop_service.log')
PORT = 9917
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
CACHE_DIR = os.path.join(HERE, 'cache')
os.makedirs(CACHE_DIR, exist_ok=True)

_img_cache = {}
_lock = threading.Lock()
_hits = [0]

PLACEHOLDER_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
                   b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
                   b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


def log(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    except Exception:
        pass


def system_proxy():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        if winreg.QueryValueEx(k, 'ProxyEnable')[0] != 1:
            return None
        s = str(winreg.QueryValueEx(k, 'ProxyServer')[0] or '').strip()
        if s and '://' not in s:
            s = 'http://' + s
        return s or None
    except Exception:
        return None


def quote_url(u):
    try:
        p = urllib.parse.urlsplit(u)
        return urllib.parse.urlunsplit(
            (p.scheme, p.netloc, urllib.parse.quote(p.path, safe='/%'), p.query, p.fragment))
    except Exception:
        return u


def split_pic(raw):
    """拆 TVBox 的 `图片地址@Referer=xxx@User-Agent=xxx` 语法"""
    parts = str(raw).split('@')
    headers = {}
    for p in parts[1:]:
        if '=' in p:
            k, v = p.split('=', 1)
            headers[k.strip()] = urllib.parse.unquote(v.strip())
    return parts[0], headers


def fetch(url, headers, timeout=12):
    hh = {'User-Agent': headers.get('User-Agent') or UA,
          'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'}
    if headers.get('Referer'):
        hh['Referer'] = headers['Referer']
    # 豆瓣图床有防盗链（直接取 418），补固定 Referer
    if 'doubanio.com' in url:
        hh['Referer'] = 'https://movie.douban.com/'
    proxy = system_proxy()
    opener = (urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
        if proxy else urllib.request.build_opener())
    req = urllib.request.Request(quote_url(url), headers=hh)
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def guess_ctype(data):
    if data[4:12] in (b'ftypavif', b'ftypavis'):
        return 'image/avif'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def to_landscape(data):
    """横图原样；竖图做「完整封面 + 模糊背景」16:9 合成，不裁掉任何内容。

    竖版海报强行裁一条横带会切掉标题/人物（用户反馈「裁切不全」）。
    改为：横框背景用原图放大高斯模糊铺满，中间放完整竖图（contain，高度填满），
    这样比例是横的、内容一点不少、也不会变形。

    ★ PIL 不认识的格式（如腾讯图床的 AVIF）原样放行——浏览器自己能显示。
    """
    from PIL import ImageFilter

    if data[4:12] in (b'ftypavif', b'ftypavis'):
        return data, False
    try:
        im = Image.open(io.BytesIO(data)).convert('RGB')
    except Exception:
        return data, False   # 未知图片格式：原样返回，不回占位图
    w, h = im.size
    if w / h >= 1.2:
        return data, False

    TW, TH = 640, 360
    # 背景：原图拉伸铺满 + 高斯模糊
    bg = im.resize((TW, TH), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(18))
    # 前景：完整竖图，按高度填满 16:9 框（宽度按比例缩放，左右自然留空露出模糊背景）
    fg = im.copy()
    fg.thumbnail((TW, TH), Image.Resampling.LANCZOS)
    bg.paste(fg, ((TW - fg.width) // 2, (TH - fg.height) // 2))
    buf = io.BytesIO()
    bg.save(buf, 'JPEG', quality=88)
    return buf.getvalue(), True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, data, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=300')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/ping':
            body = json.dumps({'ok': True, 'cached': len(_img_cache)}).encode()
            self._send(body, 'application/json')
            return
        if parsed.path != '/crop':
            self._send(PLACEHOLDER_PNG, 'image/png')
            return

        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        _hits[0] += 1
        if _hits[0] % 50 == 1:
            log('已处理 %d 次' % _hits[0])

        raw = q.get('u') or ''
        if raw.startswith('b64:'):
            try:
                raw = base64.urlsafe_b64decode(
                    raw[4:] + '=' * (-len(raw[4:]) % 4)).decode('utf-8')
            except Exception:
                raw = ''
        url, headers = split_pic(raw)
        if q.get('r'):
            headers['Referer'] = q['r']
        if not url.startswith(('http://', 'https://')):
            self._send(PLACEHOLDER_PNG, 'image/png')
            return

        key = hashlib.md5((url + '|' + headers.get('Referer', '')).encode()).hexdigest()
        with _lock:
            hit = _img_cache.get(key)
        if hit:
            self._send(hit[0], hit[1])
            return

        try:
            data = fetch(url, headers)
            out, cropped = to_landscape(data)
            ctype = 'image/jpeg' if cropped else guess_ctype(out)
        except Exception as e:
            log('失败 %s: %s' % (url[:65], e))
            self._send(PLACEHOLDER_PNG, 'image/png')
            return
        with _lock:
            if len(_img_cache) > 1200:
                _img_cache.clear()
            _img_cache[key] = (out, ctype)
        self._send(out, ctype)

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    # 绑 0.0.0.0：Win-Box 的图片代理从 192.168.10.11 访问过来，不能只听 127.0.0.1
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    log('封面自适应服务启动 :%d  PIL=%s' % (port, sys.argv[0]))
    srv.serve_forever()


if __name__ == '__main__':
    main()
