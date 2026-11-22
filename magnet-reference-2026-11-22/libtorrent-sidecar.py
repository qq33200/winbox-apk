# -*- coding: utf-8 -*-
"""libtorrent 取流 sidecar（原型）

给 Win-Box 的磁力播放提供一个"按播放点调度"的取流服务，补 aria2 做不到的两件事：
  1. sequential_download + set_piece_deadline：播放点附近优先，拖动不用等整片
  2. 只读已完成的 piece：读完即走，不读稀疏空洞

启动：
    python libtorrent-sidecar.py --port 9980 --save-path D:/TVbox/Win-Box/data/winbox-cache/bt

接口：
    POST /open   {torrent|magnet, save_path}   → {ok, ih, name, files:[{index,path,length}]}
    GET  /status?ih=<hash>                     → {ok, state, progress, rate, peers, pieces}
    POST /seek   {ih, index, offset}           → 给该位置周围设 deadline
    GET  /stream?ih=<hash>&index=<n>           → 支持 Range 的取流（206）
    POST /close  {ih}

只做本机回环，不监听外网。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 嵌入版 Python 的 python311._pth 不含 Lib/site-packages，这里显式补上
_SP = os.path.join(os.path.dirname(sys.executable), 'Lib', 'site-packages')
if os.path.isdir(_SP) and _SP not in sys.path:
    sys.path.insert(0, _SP)

import libtorrent as lt

CHUNK = 256 * 1024
DEADLINE_PIECES = 32          # 播放点前后各给多少片设 deadline
DEADLINE_MS = 2000            # 第一片的 deadline，往后每片 +60ms
FIRST_WAIT_SEC = 40           # 首片最多等多久（等不到就回 503，避免播放器干等）


class Store(object):
    def __init__(self, save_path, port):
        self.save_path = save_path
        self.ses = lt.session({
            'listen_interfaces': '0.0.0.0:%d' % (port + 10),
            'enable_dht': True,
            'enable_lsd': True,
            'enable_upnp': True,
            'enable_natpmp': True,
            'alert_mask': lt.alert.category_t.status_notification | lt.alert.category_t.error_notification,
        })
        self.handles = {}
        self.paths = {}
        self.lock = threading.Lock()

    def open(self, source, save_path=None):
        path = save_path or self.save_path
        if not os.path.isdir(path):
            os.makedirs(path)
        p = lt.add_torrent_params()
        if source.startswith('magnet:'):
            p = lt.parse_magnet_uri(source)
            p.save_path = path
        else:
            p.ti = lt.torrent_info(source)
            p.save_path = path
        h = self.ses.add_torrent(p)
        # 立刻顺序下载（拖动时会临时关掉）
        h.set_sequential_download(True)
        ih = str(h.info_hash())
        with self.lock:
            self.handles[ih] = h
            self.paths[ih] = path
        return ih, h

    def get(self, ih):
        with self.lock:
            return self.handles.get(ih)

    @staticmethod
    def files_of(h):
        try:
            ti = h.torrent_file()
        except Exception:
            return []
        if not ti:
            return []
        out = []
        fs = ti.files()
        for i in range(fs.num_files()):
            out.append({'index': i + 1, 'path': fs.file_path(i), 'length': fs.file_size(i)})
        return out

    def status(self, h):
        st = h.status()
        return {
            'state': str(st.state),
            'progress': round(st.progress * 100, 2),
            'rate': st.download_rate,
            'peers': st.num_peers,
            'seeds': st.num_seeds,
            'done': st.total_done,
            'wanted': st.total_wanted,
            'pieces': st.num_pieces,
            'piece_length': h.torrent_file().piece_length() if h.torrent_file() else 0,
        }

    def seek(self, h, file_index, offset):
        """给该偏移附近的片设 deadline，并把顺序下载关掉（否则会从头抢）"""
        ti = h.torrent_file()
        if not ti:
            return 0
        pl = ti.piece_length()
        fs = ti.files()
        base = fs.file_offset(file_index - 1)
        first = int((base + offset) // pl)
        last = min(int(ti.num_pieces()) - 1, first + DEADLINE_PIECES)
        h.set_sequential_download(False)
        for k in range(first, last + 1):
            h.set_piece_deadline(k, DEADLINE_MS + (k - first) * 60)
        return last - first + 1

    def wait_piece(self, h, piece, timeout=60.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if h.have_piece(piece):
                return True
            time.sleep(0.08)
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    store = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            return {}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/open':
            d = self._body()
            src = d.get('torrent') or d.get('magnet') or ''
            if not src:
                return self._json({'ok': False, 'error': '缺少 torrent/magnet'}, 400)
            try:
                ih, h = self.store.open(src, d.get('save_path'))
                return self._json({'ok': True, 'ih': ih, 'files': self.store.files_of(h)})
            except Exception as exc:
                return self._json({'ok': False, 'error': str(exc)[:200]}, 500)
        if u.path == '/select':
            d = self._body()
            h = self.store.get(d.get('ih') or '')
            if not h:
                return self._json({'ok': False, 'error': '未知 infohash'}, 404)
            ti = h.torrent_file()
            if not ti:
                return self._json({'ok': False, 'error': '元数据未就绪'}, 503)
            n = ti.files().num_files()
            idx = int(d.get('index') or 1)
            prios = [0] * n
            if 1 <= idx <= n:
                prios[idx - 1] = 4
            h.prioritize_files(prios)
            return self._json({'ok': True, 'index': idx, 'files': n})
        if u.path == '/seek':
            d = self._body()
            h = self.store.get(d.get('ih') or '')
            if not h:
                return self._json({'ok': False, 'error': '未知 infohash'}, 404)
            n = self.store.seek(h, int(d.get('index') or 1), int(d.get('offset') or 0))
            return self._json({'ok': True, 'pieces': n})
        if u.path == '/close':
            d = self._body()
            ih = d.get('ih') or ''
            with self.store.lock:
                h = self.store.handles.pop(ih, None)
            if h:
                self.store.ses.remove_torrent(h)
            return self._json({'ok': True})
        self._json({'ok': False, 'error': 'not found'}, 404)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/health':
            return self._json({'ok': True})
        if u.path == '/status':
            h = self.store.get((q.get('ih') or [''])[0])
            if not h:
                return self._json({'ok': False, 'error': '未知 infohash'}, 404)
            return self._json(dict({'ok': True}, **self.store.status(h)))
        if u.path == '/stream':
            return self._stream(q)
        self._json({'ok': False, 'error': 'not found'}, 404)

    def _stream(self, q):
        h = self.store.get((q.get('ih') or [''])[0])
        if not h:
            return self._json({'ok': False, 'error': '未知 infohash'}, 404)
        try:
            idx = int((q.get('index') or ['1'])[0])
        except Exception:
            idx = 1
        ti = h.torrent_file()
        if not ti:
            return self._json({'ok': False, 'error': '元数据未就绪'}, 503)
        fs = ti.files()
        if idx < 1 or idx > fs.num_files():
            return self._json({'ok': False, 'error': '文件序号越界'}, 400)
        save_path = self.store.paths.get(str(h.info_hash())) or self.store.save_path
        abs_path = os.path.join(save_path, fs.file_path(idx - 1))
        total = fs.file_size(idx - 1)
        base = fs.file_offset(idx - 1)
        pl = ti.piece_length()

        rng = self.headers.get('Range') or ''
        start, end = 0, total - 1
        partial = bool(rng)
        m = re.match(r'bytes=(\d*)-(\d*)', rng)
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), total - 1)
            elif m.group(1):
                end = total - 1
        if end < start:
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % total)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        # 拖动点优先：先把这块的片设上 deadline
        self.store.seek(h, idx, start)

        # 首片就绪再发头：拿不到就回 503，别让播放器拿着 206 干等（aria2 那边就是这么处理的）
        first_piece = int((base + start) // pl)
        last_piece = int((base + min(start + CHUNK - 1, end)) // pl)
        for k in range(first_piece, last_piece + 1):
            if not self.store.wait_piece(h, k, timeout=FIRST_WAIT_SEC):
                self._json({'ok': False, 'error': '首片未就绪（做种者过少或无做种）'}, 503)
                return

        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if partial:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, total))
        self.end_headers()

        fh = open(abs_path, 'rb')
        try:
            pos = start
            while pos <= end:
                stop = min(pos + CHUNK - 1, end)
                first = int((base + pos) // pl)
                last = int((base + stop) // pl)
                for k in range(first, last + 1):
                    if not self.store.wait_piece(h, k, timeout=60):
                        self.close_connection = True
                        return
                fh.seek(pos)
                self.wfile.write(fh.read(stop - pos + 1))
                pos = stop + 1
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9980)
    ap.add_argument('--save-path', default=os.path.join(os.path.expanduser('~'), 'libtorrent-bt'))
    args = ap.parse_args()

    Handler.store = Store(args.save_path, args.port)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('[libbt] listening 127.0.0.1:%d  save_path=%s' % (args.port, args.save_path), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
