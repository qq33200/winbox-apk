#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
winbox_live_sub.py — Win-Box 实时语音字幕（外语 → 中文）
=========================================================
链路：
    Win-Box 的 mpv 命名管道（读播放地址 / 进度 / 暂停）
      → ffmpeg 从当前位置拉流，输出 16k 单声道 PCM
      → Silero VAD 流式切段
      → SenseVoice 识别（中/英/日/韩/粤，自动判语种）
      → 非中文段交 DeepSeek 翻成简体中文
      → 写 SRT → mpv sub-add / sub-reload（字幕边播边出）

用法：
    set DEEPSEEK_API_KEY=sk-xxxx            （不设也能跑，只是不出中文）
    _asr_env\\Scripts\\python.exe winbox_live_sub.py
    _asr_env\\Scripts\\python.exe winbox_live_sub.py --bilingual   （中文 + 原文两行）

前提：
    Win-Box 播放内核必须是 mpv（设置 → 播放 → 内核 = mpv）。
    管道不存在时会每 2 秒重试，不会报错退出。

产物：
    _live/live.srt   实时字幕文件（可单独留档）
    _live/live.log   运行日志
"""
import argparse
import collections
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import sherpa_onnx

ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = r"D:\TVbox\Win-Box\resources\app.asar.unpacked\resources\ffmpeg\ffmpeg.exe"
MODEL_DIR = os.path.join(ROOT, "models", "sensevoice")
VAD_MODEL = os.path.join(ROOT, "models", "silero_vad.onnx")
WORK = os.path.join(ROOT, "_live")
SRT_PATH = os.path.join(WORK, "live.srt")
LOG_PATH = os.path.join(WORK, "live.log")
GUARD_STOP_FILE = os.path.join(WORK, "guard.stop")

# ---- Win-Box 插件设置（开关面板）--------------------------------------------
PLUGIN_ID = "ai-subtitle"
PANEL_ID = "ai-sub"


def default_prefs_path():
    """`<Win-Box 安装目录>/data/plugin-settings.json`。

    不写死盘符：从随包 ffmpeg 的路径往上退 5 层就是安装目录
    （…/Win-Box/resources/app.asar.unpacked/resources/ffmpeg/ffmpeg.exe）。
    退不出来就回落本机已知路径。
    """
    p = FFMPEG
    for _ in range(5):
        p = os.path.dirname(p)
    guess = os.path.join(p, "data", "plugin-settings.json")
    if os.path.isdir(os.path.dirname(guess)):
        return guess
    return r"D:\TVbox\Win-Box\data\plugin-settings.json"

SR = 16000
BLOCK = 1024                     # 每次从 ffmpeg 读的采样数
FOLLOW_TOL = 3.0                 # 播放位置与拉流位置允许的偏差（秒），超过则重连
PACE_LEAD = 1.5                  # 软件节流允许的预读量（秒）；必须 < FOLLOW_TOL
MIN_CUE_DUR = 0.7
# 实时字幕「兜底停留」：识别+翻译出来时这句话往往已经播过去了，至少留这么久
HOLD_AFTER = 1.8
MAX_CUE_DUR = 8.0
# 迟到的字幕：超过这么多秒还没写进来就干脆丢掉（参考 TV 的 lateOrShort 丢弃策略），
# 否则一条很早以前的句子会突然闪出来，反而干扰观看
STALE_DROP = 6.0
# 翻译带上几轮前文当上下文
CONTEXT_TURNS = 2
# 高频短语气词本地直译，不走网络（省 0.7~1.1 秒/条）
QUICK = {
    "うん": "嗯", "うんうん": "嗯嗯", "はい": "好", "はいはい": "好好好",
    "ああ": "啊", "ええ": "欸", "えっ": "诶？", "ううん": "不", "いや": "不要",
    "そう": "是啊", "そうだね": "是啊", "そうだよ": "是的", "そうか": "这样啊",
    "なに": "什么", "何": "什么", "だめ": "不行", "だめだめ": "不行不行",
    "やめて": "不要", "やめてよ": "别这样", "いたい": "疼", "きもちいい": "好舒服",
    "すごい": "好厉害", "かわいい": "好可爱", "ありがとう": "谢谢",
    "ごめん": "抱歉", "ごめんなさい": "对不起", "ちょっと": "等一下",
    "まって": "等等", "だいじょうぶ": "没事吧", "おねがい": "拜托了",
    "네": "是", "예": "是", "아니": "不是", "아니요": "不是", "응": "嗯",
    "yes": "是", "no": "不", "oh": "哦", "ok": "好", "okay": "好",
    "thank you": "谢谢", "sorry": "抱歉", "wait": "等等", "what": "什么",
}
TAIL_PAD = 0.20
MAX_CHARS = 24
PUNCT_BREAK = set("。！？!?；;…")
PUNCT_SOFT = set("，,、：:")

_log_lock = threading.Lock()


def log(msg, *a):
    line = ("[%s] " % time.strftime("%H:%M:%S")) + (msg % a if a else msg)
    with _log_lock:
        # ★ pythonw.exe 下 sys.stdout 是 None，print 会抛异常 ⇒ 必须兜住，
        #   否则后台静默启动时第一条日志就把进程带走。
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ------------------------------------------------------------------ mpv 管道
class MpvLink:
    """连接 mpv 的 input-ipc-server 命名管道。"""

    def __init__(self, name: str):
        self.name = name
        self.f = open(r"\\.\pipe\\" + name, "r+b", buffering=0)
        self.buf = b""
        self.seq = 0
        self.lock = threading.Lock()
        # ★ mpv 的 IPC 管道**同一时刻只接受一个客户端**。别的脚本连上去之后，
        #   我们这条会被对端关掉：read 返回空、所有 get 变 None。
        #   以前这种情况会「静默空转」（日志一条不出，看着像卡死），
        #   所以这里标记 dead，让主循环主动重连。
        self.dead = False
        self.fails = 0

    @staticmethod
    def find():
        try:
            names = os.listdir(r"\\.\pipe\\")
        except OSError:
            return []
        # ★ 只认 Win-Box 的管道：`mpv` 前缀会把系统里别人的 mpv 会话也捞进来，
        #   连错会话会把字幕挂到别人的播放器上（实测踩到）。
        return sorted(n for n in names if n.startswith("winbox-mpv-"))

    def cmd(self, command, timeout=1.5):
        if self.dead:
            return {"error": "closed"}
        with self.lock:
            self.seq += 1
            rid = self.seq
            try:
                self.f.write((json.dumps({"command": command, "request_id": rid}) + "\n").encode())
            except OSError:
                self.dead = True
                return {"error": "closed"}
            t0 = time.time()
            while time.time() - t0 < timeout:
                while b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    try:
                        m = json.loads(line.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if m.get("request_id") == rid:
                        return m
                try:
                    chunk = self.f.read(65536)
                except OSError:
                    self.dead = True
                    return {"error": "closed"}
                if chunk:
                    self.buf += chunk
                else:
                    # 读到空 = 对端已经关了这条连接
                    self.dead = True
                    return {"error": "closed"}
            return {"error": "timeout"}

    def get(self, prop, timeout=1.5):
        r = self.cmd(["get_property", prop], timeout)
        return r.get("data") if r.get("error") == "success" else None

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def mpv_pid_from_pipe(name: str):
    """管道名形如 `winbox-mpv-25016-1-mwc8bljo`，中间那段就是 Win-Box 主进程号。
    mpv.exe 是它的子进程，进程级回环用 INCLUDE_TARGET_PROCESS_TREE 一次覆盖。"""
    try:
        p = name.split("-")
        if len(p) >= 3 and p[0] == "winbox" and p[1] == "mpv":
            return int(p[2])
    except Exception:
        pass
    return None


def source_url(raw: str):
    """Win-Box 走本地中继 /play?url=<真实地址>&ua=<UA>；解出真实地址更稳。"""
    try:
        u = urllib.parse.urlparse(raw)
        if u.path.rstrip("/").endswith("/play"):
            q = urllib.parse.parse_qs(u.query)
            real = (q.get("url") or [""])[0]
            ua = (q.get("ua") or q.get("user-agent") or [""])[0]
            if real.startswith("http"):
                return real, ua
    except Exception:
        pass
    return raw, ""


# ------------------------------------------------------------------ HLS 本地中继
def relay_params(raw: str):
    """Win-Box 的播放地址形如 /play?url=<真实地址>&ua=&referer=&headers=<JSON>。
    返回 dict；不是中继地址则返回 None。"""
    try:
        u = urllib.parse.urlparse(raw)
        if u.path.rstrip("/").endswith("/play"):
            q = urllib.parse.parse_qs(u.query)
            real = (q.get("url") or [""])[0]
            if real.startswith("http"):
                return {"url": real,
                        "ua": (q.get("ua") or q.get("user-agent") or [""])[0],
                        "referer": (q.get("referer") or [""])[0],
                        "headers": (q.get("headers") or [""])[0]}
    except Exception:
        pass
    return None


class HlsRelay:
    """★ 为什么必须有这一层（2026-11-11 22:5x 实测，别再走回头路）：
      · getav 这类源把 **HLS 分片伪装成 .woff2**，还要求 UA/Referer/Origin 三件套，
        裸地址直连是 403（缺 UA 时甚至不是 403，而是**无限挂住**不返回）。
      · Win-Box 自己的 /play 中继能把播放列表给你，但会把**分片地址也改写回它自己**，
        于是路径永远是 /play，ffmpeg 的 hls 解复用器判成 "extension none" 直接拒，
        `-allowed_extensions ALL` / 显式加 woff2 都无效（这个 avbuild 构建不认）。
      ⇒ 做法：播放列表我们自己取、自己改写成本地地址（分片带 .ts 后缀），
        分片和 AES 密钥由本地小服务**带齐请求头**转发。扩展名天然合法，请求头不丢。
    """

    def __init__(self, raw, workdir):
        self.p = relay_params(raw)
        self.raw = raw
        self.workdir = workdir
        self.map = {}          # 本地名 -> 上游真实地址
        self.base = None       # 上游播放列表地址
        self.httpd = None
        self.port = 0
        self.lock = threading.Lock()

    # ---- 上游请求
    def _hdrs(self):
        h = {}
        if self.p["ua"]:
            h["User-Agent"] = self.p["ua"]
        else:
            h["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/128.0.0.0 Safari/537.36")
        if self.p["referer"]:
            h["Referer"] = self.p["referer"]
        if self.p["headers"]:
            try:
                for k, v in json.loads(self.p["headers"]).items():
                    h[str(k)] = str(v)
            except Exception:
                pass
        return h

    def _opener(self):
        """★ 本机环境变量里有 http_proxy=127.0.0.1:8350，对这类源站是全不通的
        （实测 SSL: UNEXPECTED_EOF）⇒ 默认直连；直连失败才退回系统代理。"""
        if getattr(self, "_op", None) is None:
            self._op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return self._op

    def _open(self, url, timeout=25):
        req = urllib.request.Request(url, headers=self._hdrs())
        try:
            return self._opener().open(req, timeout=timeout)
        except Exception:
            req2 = urllib.request.Request(url, headers=self._hdrs())
            return urllib.request.urlopen(req2, timeout=timeout)

    def _get(self, url, timeout=25):
        with self._open(url, timeout) as r:
            return r.read()

    @staticmethod
    def _unwrap(u, base):
        """相对地址补全；如果是中继包装，解出真实地址。"""
        u = urllib.parse.urljoin(base, u)
        p = relay_params(u)
        return p["url"] if p else u

    def _register(self, real, suffix):
        name = hashlib.md5(real.encode()).hexdigest()[:16] + suffix
        with self.lock:
            self.map[name] = real
        return name

    def _rewrite(self, body, base):
        out = []
        for ln in body.replace("\r\n", "\n").split("\n"):
            s = ln.strip()
            if not s:
                out.append(ln)
                continue
            if s.startswith("#"):
                # 改写 URI="..."（EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA）
                def sub(m):
                    real = self._unwrap(m.group(2), base)
                    sfx = ".key" if "KEY" in s[:16] else ".ts"
                    return '%s"%s"' % (m.group(1), self._register(real, sfx))
                out.append(re.sub(r'(URI=)"([^"]+)"', sub, ln))
            else:
                real = self._unwrap(s, base)
                out.append("http://127.0.0.1:%d/s/%s" % (self.port, self._register(real, ".ts")))
        return "\n".join(out) + "\n"

    def _handler(self):
        relay = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            # ★ ffmpeg 取完分片就断开，keep-alive 连接被重置是常态；
            #   socketserver 默认会打一整段 Traceback 刷屏，这里吃掉。
            def handle(self):
                try:
                    BaseHTTPRequestHandler.handle(self)
                except (ConnectionResetError, ConnectionAbortedError,
                        BrokenPipeError, TimeoutError, OSError):
                    pass

            def handle_error(self, request, client_address):
                pass

            def do_GET(self):
                try:
                    path = urllib.parse.urlparse(self.path).path
                    if path.endswith(".m3u8"):
                        body = relay._get(relay.base).decode("utf-8", "replace")
                        # 主列表 → 跟第一个子列表
                        if "#EXT-X-STREAM-INF" in body:
                            for ln in body.split("\n"):
                                t = ln.strip()
                                if t and not t.startswith("#"):
                                    relay.base = relay._unwrap(t, relay.base)
                                    body = relay._get(relay.base).decode("utf-8", "replace")
                                    break
                        data = relay._rewrite(body, relay.base).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    name = os.path.basename(path)
                    with relay.lock:
                        real = relay.map.get(name)
                    if not real:
                        self.send_error(404)
                        return
                    # ★ 分片边下边转（不整块读进内存）：单块 3~4 MB，
                    #   ffmpeg 不等整块落盘就能开始解码，起播快很多。
                    with relay._open(real, timeout=30) as r:
                        n = r.headers.get("Content-Length")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        if n:
                            self.send_header("Content-Length", n)
                        else:
                            self.send_header("Connection", "close")
                            self.close_connection = True
                        self.end_headers()
                        while True:
                            buf = r.read(65536)
                            if not buf:
                                break
                            self.wfile.write(buf)
                    return
                except Exception as e:
                    try:
                        self.send_error(502, str(e)[:80])
                    except Exception:
                        pass

        return H

    def start(self, timeout=14):
        """取一次播放列表就够（原来取两遍，白等 2 秒）。非 HLS 直接抛错让上层退回。"""
        self.base = self.p["url"]
        body = self._get(self.base, timeout).decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" in body:
            for ln in body.split("\n"):
                t = ln.strip()
                if t and not t.startswith("#"):
                    self.base = self._unwrap(t, self.base)
                    body = self._get(self.base, timeout).decode("utf-8", "replace")
                    break
        if not body.lstrip().startswith("#EXTM3U"):
            raise RuntimeError("不是 HLS 播放列表")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/pl.m3u8" % self.port

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


# ------------------------------------------------------------------ 拉流
class Puller:
    """ffmpeg 从指定位置拉流，产出 float32 单声道块。"""

    def __init__(self, url, start, ua=""):
        self.url = url
        self.start = max(0.0, start)
        self.ua = ua
        self.proc = None
        self.q = queue.Queue(maxsize=400)
        self.stopped = threading.Event()
        self.pace_t0 = 0.0        # 拉流开始时刻（用于软件节流）
        self.fed = 0.0            # 已投递给 VAD 的音频秒数
        self.relay = None

    def start_proc(self):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
        # ★ 防盗链 HLS 走本地中继（见 HlsRelay 里的实测说明）；失败就退回原地址
        self.relay = None
        rp = relay_params(self.url)
        if rp:
            try:
                rl = HlsRelay(self.url, WORK)
                src = rl.start()
                self.relay = rl
                log("HLS 本地中继已启动：%s", src)
            except Exception as e:
                self.relay = None
                log("本地中继启动失败（%s），退回原地址", str(e)[:120])
                src = rp["url"]
        else:
            src, ua2 = source_url(self.url)
            if ua2 and not self.ua:
                self.ua = ua2
        if self.start > 0:
            cmd += ["-ss", "%.3f" % self.start]
        # ★★ 2026-11-11 实测：这里**不能用 -re**。
        #   一旦加了 -re，`-ss` 落到关键帧之前时，ffmpeg 会把「关键帧→目标点」
        #   那段也按 1× 解码出来，于是每次起播/拖动都要白等一个 GOP：
        #       -ss 3.2  + -re → 3.15s 才出第一块 PCM
        #       -ss 12.7 + -re → 2.66s
        #       -ss 3.2  不带 -re → 0.04s     ← 快 80 倍
        #   改为「ffmpeg 全速解码 + 我们自己按 1× 节流」（见 _reader 的 PACE_LEAD）。
        #   注意：别试图用 -noaccurate_seek 提速 —— 它会从关键帧开始吐，
        #   内容比 -ss 目标点更早，时间轴整体前移，字幕会错位。
        if self.ua and not self.relay:
            cmd += ["-user_agent", self.ua]
        cmd += ["-i", src, "-vn", "-ac", "1", "-ar", str(SR),
                "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        # ★ 走本地中继时，ffmpeg 自己会读 http_proxy 环境变量，把 127.0.0.1 也送进代理
        #   （本机 http_proxy=127.0.0.1:8350）⇒ 必须把代理环境变量清掉再启动子进程。
        env = dict(os.environ)
        for k in ("http_proxy", "https_proxy", "all_proxy",
                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(k, None)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0, env=env)
        self.pace_t0 = time.time()
        self.fed = 0.0
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err_reader, daemon=True).start()

    def _reader(self):
        p = self.proc
        need = BLOCK * 2
        chunk_s = BLOCK / float(SR)
        buf = b""
        try:
            while not self.stopped.is_set():
                chunk = p.stdout.read(need)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= need:
                    part, buf = buf[:need], buf[need:]
                    a = np.frombuffer(part, dtype=np.int16).astype(np.float32) / 32768.0
                    # ★ 软件节流：放行速度不超过 1× + LEAD，替代 ffmpeg 的 -re。
                    #   LEAD 必须明显小于 FOLLOW_TOL(3.0s)，否则拖进度会被误判成 seek。
                    ahead = self.fed + chunk_s - ((time.time() - self.pace_t0) + PACE_LEAD)
                    if ahead > 0:
                        time.sleep(min(ahead, 0.5))
                    # ★ 队列满时必须阻塞等待（背压），不能丢帧 —— 丢帧会让 VAD 段错位
                    while not self.stopped.is_set():
                        try:
                            self.q.put(a, timeout=0.3)
                            break
                        except queue.Full:
                            continue
                    self.fed += chunk_s
        except Exception:
            pass

    def _err_reader(self):
        try:
            for line in self.proc.stderr:
                s = line.decode("utf-8", "replace").strip()
                if s:
                    log("ffmpeg: %s", s[:200])
        except Exception:
            pass

    def stop(self):
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.relay:
            try:
                self.relay.stop()
            except Exception:
                pass
            self.relay = None


# ------------------------------------------------------------------ 音频来源（回环）
class LoopbackSource:
    """录播放器**自己**发出的声音（Windows WASAPI 回环）。

    ★★ 两种回环模式，默认优先「进程回环」：
      · process —— 只抓目标进程树（Win-Box → mpv.exe）渲染的音频。
        电脑同时放音乐、来条提示音，都**不会**混进识别里。Win10 2004+ 才有。
      · device  —— 抓整个声卡。老系统兜底，但别的程序出声会污染。

    ★★ 为什么不再靠 ffmpeg 另拉一路流（2026-11-11 实测，别走回头路）：
      · 本机到 getav 源站：curl 单连接 285~325 KB/s（≈2.5 Mbps），
        Python 6 并发 455 KB/s，12 并发直接被 CDN 限流超时。
      · 而片源 1080p 每个 **6 秒分片 2.3~3.6 MB ≈ 4.7 Mbps**。
      ⇒ 再额外下一路视频 = 带宽翻倍，实测 45 秒墙钟只推进 2.8 秒音频，
        40 秒才蹦出一句字幕，完全不可用。
      回环则**零额外带宽**，「听到什么识别什么」，也不用管防盗链 / HLS 改写。

    时间轴怎么对：每块音频入队时记 (已喂秒数, 墙钟)；
    出句时按「这句在已喂流里的秒数」反查墙钟，再查那一刻 mpv 的 time-pos。
    """

    def __init__(self, pid=None, sr=SR, block=BLOCK):
        self.sr = sr
        self.block = block
        self.pid = int(pid) if pid else None
        self.mode = "process" if self.pid else "device"
        self.q = queue.Queue(maxsize=600)
        self.stopped = threading.Event()
        self.playing = threading.Event()
        self.marks = collections.deque(maxlen=6000)
        self.fed = 0.0
        self.mlock = threading.Lock()
        self.gain = 1.0
        self._silent_blocks = 0
        self.cap = None          # 进程回环
        self.rec = None          # 设备回环
        self.spk_name = ""
        self.name = ""
        self.ready = threading.Event()
        self.err = None
        self.rms = None
        self.peak = 0.0
        self._bytebuf = b""

    # ---- 打开设备（在采集线程里做：COM 要求同一线程初始化）
    def _open_backend(self):
        if self.mode == "process":
            import proc_loopback
            self.cap = proc_loopback.ProcessLoopback(self.pid, self.sr, 2).open()
            self.name = "进程回环 · pid %d（只抓播放器自己的声音）" % self.pid
            return
        import soundcard as sc
        spk = sc.default_speaker()
        if spk is None:
            raise RuntimeError("系统没有可用扬声器")
        mic = sc.get_microphone(id=str(spk.id), include_loopback=True)
        if mic is None:
            raise RuntimeError("该扬声器不支持回环采集")
        self.spk_name = spk.name
        self.rec = mic.recorder(samplerate=self.sr, channels=2, blocksize=self.block)
        self.rec.__enter__()
        self.name = "设备回环 · %s（注意：会混入其他程序的声音）" % spk.name

    def start(self, timeout=10.0):
        threading.Thread(target=self._loop, name="loopback", daemon=True).start()
        if not self.ready.wait(timeout):
            raise RuntimeError("音频回环启动超时")
        if self.err:
            raise self.err
        return self

    # ---- 采集
    def _agc(self, x):
        """把回环音频拉到 VAD/识别器能稳定工作的电平。

        ★ 2026-11-12 二次修正：上一版为了救「系统音量被调低」把上限提到 40 倍，
          结果在**正常音量**下反过来把信号推爆：实测原始 RMS 0.029 / 峰值 0.297，
          旧 AGC 输出 RMS 0.167 / 峰值 **1.0000**（117 个样本被硬切）。
          削波会毁掉识别精度；更要命的是静音段把增益顶到 40 倍、
          来声音时 EMA 回落太慢，VAD 输入忽大忽小 ⇒ 整段对白切不出句子
          （日志里出现过连续 2 分钟有电平却零识别，用户看到的就是「很多话没字幕」）。

          现在四条约束：
          ① 静音门控：RMS 过小的块不更新增益，别把底噪放大成「假语音」；
          ② 慢升快降：增益上升慢、回落快，音量突变立刻让路，防削波；
          ③ 上限 12 倍（正常音量只需 3~6 倍），目标峰值 0.45；
          ④ 软限幅 tanh 代替硬 clip，避免硬切产生谐波把识别带偏。
        """
        rms = float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
        peak = float(np.abs(x).max()) if len(x) else 0.0
        if rms > 0.0015 and peak > 1e-4:
            want = min(12.0, max(1.0, 0.45 / peak))
            k = 0.05 if want > self.gain else 0.45   # 慢升快降
            self.gain = (1 - k) * self.gain + k * want
            self._silent_blocks = 0
        else:
            self._silent_blocks += 1
            if self._silent_blocks > 30:             # 连续 ~2s 静音，缓缓回落到 1
                self.gain = 0.995 * self.gain + 0.005 * 1.0
        return np.tanh(x * self.gain * 1.1) / 1.1

    def _emit(self, mono):
        raw = np.asarray(mono, dtype=np.float32)
        # 记录**增益前**的原始电平：心跳里报出来，就能一眼分清
        # 「压根没声音（播放器静音/换设备）」和「有声音但没识别」。
        try:
            r = float(np.sqrt((raw ** 2).mean()))
            self.rms = r if self.rms is None else (0.85 * self.rms + 0.15 * r)
            self.peak = max(self.peak * 0.99, float(np.abs(raw).max()) if len(raw) else 0.0)
        except Exception:
            pass
        mono = self._agc(raw)
        try:
            self.q.put(mono, timeout=0.5)
        except queue.Full:
            return
        self.fed += len(mono) / float(self.sr)
        with self.mlock:
            self.marks.append((self.fed, time.time()))

    def _loop(self):
        try:
            self._open_backend()
        except Exception as e:
            self.err = e
            self.ready.set()
            return
        if self.cap is not None:
            self.cap.start()
        self.ready.set()
        need = self.block * 2 * 2      # block 帧 × 2 声道 × 2 字节
        while not self.stopped.is_set():
            if not self.playing.is_set():
                # ★ 不播的时候也要把管道里残的数据读掉，否则恢复播放时会先吐出
                #   一大段旧音频，时间轴整体错位。
                if self.cap is not None:
                    self.cap.read()
                time.sleep(0.02)
                continue
            if self.cap is not None:
                d = self.cap.read()
                if not d:
                    time.sleep(0.006)
                    continue
                self._bytebuf += d
                while len(self._bytebuf) >= need:
                    raw, self._bytebuf = self._bytebuf[:need], self._bytebuf[need:]
                    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    self._emit(a.reshape(-1, 2).mean(axis=1))
            else:
                try:
                    buf = self.rec.record(numframes=self.block)
                except Exception as e:
                    if self.stopped.is_set():
                        break
                    log("回环采集中断，1 秒后重试：%s", str(e)[:80])
                    time.sleep(1.0)
                    continue
                a = np.asarray(buf, dtype=np.float32)
                self._emit(a.mean(axis=1) if a.ndim == 2 else a)

    def wall_at(self, sec):
        """这句在「已喂流」里的 sec 秒，对应哪一刻墙钟。"""
        with self.mlock:
            if not self.marks:
                return time.time()
            prev = None
            for f, w in reversed(self.marks):
                if f <= sec:
                    return w + (sec - f)
                prev = (f, w)
            return prev[1] - (prev[0] - sec)

    def stop(self):
        self.stopped.set()
        self.playing.clear()
        if self.cap is not None:
            try:
                self.cap.close()
            except Exception:
                pass
            self.cap = None
        if self.rec is not None:
            try:
                self.rec.__exit__(None, None, None)
            except Exception:
                pass
            self.rec = None


# ------------------------------------------------------------------ ASR
class Asr:
    def __init__(self, threads=6, model_path=None, max_speech=7.0):
        if model_path is None:
            fp32 = os.path.join(MODEL_DIR, "model.onnx")
            int8 = os.path.join(MODEL_DIR, "model.int8.onnx")
            # ★ 2026-11-12 A/B（SenseVoice 官方 5 语种样本）：
            #   fp32 明显更准（英文 gold；int8 错成 code），速度仅慢 25%（0.20 vs 0.16 s/句）。
            #   默认用 fp32；内存/速度紧张时设 WINBOX_ASR_INT8=1 回退 int8。
            if os.environ.get("WINBOX_ASR_INT8") == "1" or not os.path.exists(fp32):
                model_path = int8
            else:
                model_path = fp32
        self.model_path = model_path
        print("[asr] 识别模型：%s（%.0f MB）" % (
            os.path.basename(model_path), os.path.getsize(model_path) / 1048576.0), flush=True)
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_path,
            tokens=os.path.join(MODEL_DIR, "tokens.txt"),
            num_threads=threads, use_itn=True, language="auto", debug=False)
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = VAD_MODEL
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.45
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = max_speech
        cfg.sample_rate = SR
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=90)
        # 预热：首次 decode 要建 ONNX 会话/分配缓冲，实测能吃掉 2~4 秒。
        # 在这里先空跑一次，把这笔开销从「第一句字幕」挪到「服务启动」。
        try:
            t = time.time()
            self.recognize(np.zeros(int(0.6 * SR), dtype=np.float32))
            log("推理预热完成（%.2fs）", time.time() - t)
        except Exception as e:
            log("推理预热跳过：%s", e)

    def feed(self, chunk):
        self.vad.accept_waveform(chunk)

    def take(self):
        """取出已完成的语音段：[(start_sec_in_stream, samples)]"""
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            s = seg.start / float(SR)
            a = np.array(seg.samples, dtype=np.float32)
            self.vad.pop()
            if len(a) >= int(0.25 * SR):
                out.append((s, a))
        return out

    def recognize(self, a):
        st = self.rec.create_stream()
        st.accept_waveform(SR, a)
        self.rec.decode_stream(st)
        return st.result


def split_cues(tokens, times, t0):
    cues, buf, buf_t = [], [], []

    def flush():
        if not buf:
            return
        text = "".join(buf).strip()
        if text:
            s, e = t0 + buf_t[0], t0 + buf_t[-1] + TAIL_PAD
            if e - s < MIN_CUE_DUR:
                e = s + MIN_CUE_DUR
            cues.append([s, e, text])

    for ch, t in zip(tokens, times):
        buf.append(ch)
        buf_t.append(t)
        if ch in PUNCT_BREAK or (ch in PUNCT_SOFT and len(buf) >= 12) or len(buf) >= MAX_CHARS:
            flush()
            buf, buf_t = [], []
    flush()
    return cues


# ------------------------------------------------------------------ 翻译
# OpenAI 兼容的翻译后端。前三个都是**免费**的，本机离线方案见文档。
PROVIDERS = {
    "zhipu": {
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4-flash",
        "env": "ZHIPU_API_KEY",
        "label": "智谱 GLM-4-Flash（永久免费）",
    },
    "siliconflow": {
        "url": "https://api.siliconflow.cn/v1/chat/completions",
        "model": "tencent/Hunyuan-MT-7B",
        "env": "SILICONFLOW_API_KEY",
        "label": "硅基流动 Hunyuan-MT-7B（免费·翻译专用）",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "env": "DEEPSEEK_API_KEY",
        "label": "DeepSeek（付费，质量最好）",
    },
}
PROVIDER_ORDER = ["zhipu", "siliconflow", "deepseek"]


class Translator:
    def __init__(self, provider=None, key=None, url=None, model=None):
        self.name, self.key, self.url, self.model = "off", "", "", ""
        if url and model:
            self.name, self.url, self.model = "custom", url, model
            self.key = key or ""
        else:
            names = [provider] if provider else PROVIDER_ORDER
            for n in names:
                cfg = PROVIDERS.get(n)
                if not cfg:
                    continue
                k = key or os.environ.get(cfg["env"], "").strip()
                if k:
                    self.name, self.key, self.url, self.model = n, k, cfg["url"], model or cfg["model"]
                    break
        self.enabled = bool(self.key and self.url)
        self.fail = 0
        # ★ 上下文翻译（参考 iptvorganization/TV 的 ai_subtitle_context_enabled）：
        #   影视对白前后是连贯的，单句丢给模型常常把人称、指代、语气翻错。
        #   把最近几轮「原文 → 译文」当成对话历史一起送，代词的指向会稳很多。
        #   只带 CONTEXT_TURNS 轮，太多会拖慢并且跑题。
        self.ctx = collections.deque(maxlen=CONTEXT_TURNS)
        self.ctx_lock = threading.Lock()
        # 完全相同的句子直接复用译文（一句「うん」反复出现时省一次往返）
        self.memo = {}

    def describe(self):
        if not self.enabled:
            return "未配置（只输出原文）"
        lbl = PROVIDERS.get(self.name, {}).get("label", self.name)
        return f"{lbl} · {self.model}"

    def _post(self, texts):
        """返回 (译文字符串, 错误描述)。错误**不吞**，交给调用方决定怎么报。"""
        msgs = [{"role": "system", "content":
                 "你是影视字幕翻译。把用户给出的这一句翻译成简体中文。\n"
                 "规则：只输出译文本身，不要引号、不要编号、不要解释、不要音译补充；"
                 "严禁续写、扩写、编造原文没有的内容；专有名词照原文。\n"
                 "输入若已是中文，原样返回。\n"
                 "上面几组 user/assistant 是**已翻译过的前文对白**，只用来判断人称和指代，"
                 "不要重复翻译它们，也不要延续它们的内容。"}]
        with self.ctx_lock:
            hist = list(self.ctx)
        for s, z in hist:
            msgs.append({"role": "user", "content": s})
            msgs.append({"role": "assistant", "content": z})
        msgs.append({"role": "user", "content": " ||| ".join(texts)})
        body = {
            "model": self.model,
            "temperature": 0,
            # 单行字幕不可能长，卡个上限能少等几个 token 的生成时间
            "max_tokens": 160,
            "messages": msgs,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d["choices"][0]["message"]["content"].strip(), None
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            return None, "HTTP %d %s" % (e.code, detail.strip())
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, e)

    def translate(self, texts):
        """逐条翻译。返回与输入等长的列表，失败位置为 None。

        ★ 为什么不再用 `|||` 一次塞多条：
          实测 GLM-4-Flash 遇到多行输入会「顺着往下编」——只给 2 句日文，
          它返回了十几句没出现过的英中对照，条数居然还对得上，
          于是一整段胡话被当成译文写进字幕（现场见 2026-11-11 19:03 的 live.srt）。
          逐条送 + 长度保险，才是稳的。
        """
        if not self.enabled or not texts:
            return [None] * len(texts)
        out = []
        for t in texts:
            out.append(self._one(t))
        return out

    def _one(self, text):
        src = (text or "").strip()
        if not src:
            return None
        # ★ 短语气词与重复句走本地，直接省掉一次网络往返（实测一次 0.7~1.1 秒）。
        #   这类内容在这类片子里占比很高，「うん」「はい」「ああ」反复出现。
        key = src.strip("。！？!?…、,. ")
        q = QUICK.get(key)
        if q:
            with self.ctx_lock:
                self.ctx.append((src, q))
            return q
        hit = self.memo.get(src)
        if hit:
            return hit
        txt, err = self._post([src])
        if err:
            self.fail += 1
            log("翻译失败（第 %d 次）：%s", self.fail, err)
            return None
        s = (txt or "").strip()
        s = re.sub(r"^(译文|翻译|中文|简体中文)\s*[:：]\s*", "", s)   # 模型爱加的前缀
        s = s.strip().strip('"“”「」『』').strip()
        s = s.split("\n")[0].strip()
        if not s:
            return None
        # 长度保险：正常译文不会比原文长太多；超了基本就是模型自己编下去了
        if len(s) > len(src) * 4 + 24:
            log("译文异常膨胀（%d→%d 字），丢弃本条：%s", len(src), len(s), s[:40])
            self.fail += 1
            return None
        self.fail = 0
        with self.ctx_lock:
            self.ctx.append((src, s))
            if len(self.memo) < 500:
                self.memo[src] = s
        return s


# ------------------------------------------------------------------ 字幕
class Subtitle:
    def __init__(self):
        self.cues = []          # [start, end, zh, orig]
        self.lock = threading.Lock()
        self.injected = False
        self.dirty = False
        # 延迟打点：字幕写进来时，当前播放进度比这句话结束时间晚了多少秒。
        # 这是唯一能反映「观众实际等多久」的指标，优化就盯着它。
        self.lag = None
        self.lag_max = 0.0

    def add(self, items, min_end=None, now_pos=None):
        """items: [[start, end, zh, orig]]

        ★ min_end 是「当前播放位置 + 一点点」。实时字幕是先听到再识别再翻译，
          等结果出来时，这句话的时间窗**往往已经过去了**，直接写进 SRT 会
          一闪而过甚至根本不显示。给出 min_end 就把它兜住：
          结束时间至少延到「当前进度之后」，字幕会立刻出现并停留一会儿。

        ★ now_pos 用来丢掉「实在太晚」的条目（参考 TV-AutoBuild 的 lateOrShort 策略）：
          一条 20 秒前就该播完的句子，现在才写进来，突然闪出来只会让人出戏。
        """
        with self.lock:
            if now_pos is not None:
                keep = [c for c in items if now_pos - c[1] <= STALE_DROP]
                if len(keep) != len(items):
                    log("丢弃迟到字幕 %d 条（当前 %.1f，句末 %s）",
                        len(items) - len(keep), now_pos,
                        "%.1f" % items[0][1] if items else "-")
                items = keep
                if not items:
                    return
                self.lag = now_pos - items[-1][1]
                self.lag_max = max(self.lag_max, self.lag)
            if min_end:
                fixed = []
                for s, e, zh, orig in items:
                    e2 = max(e, min_end)
                    e2 = min(e2, max(min_end, s + MAX_CUE_DUR))
                    # ★ 末句常常比当前进度还靠后（字幕迟到时），上面那句 min()
                    #   会把 e2 压到 min_end < s，写出「结束早于开始」的非法时间轴
                    #   （2026-11-12 实测：`00:04:26,824 --> 00:04:26,544`）。
                    e2 = max(e2, s + MIN_CUE_DUR)
                    fixed.append([s, e2, zh, orig])
                items = fixed
            self.cues.extend(items)
            self.cues.sort(key=lambda c: c[0])
            self._fix_overlap()
            self.dirty = True

    def _fix_overlap(self):
        for i in range(1, len(self.cues)):
            prev, cur = self.cues[i - 1], self.cues[i]
            if cur[0] < prev[1] + 0.03:
                limit = cur[0] - 0.03
                if limit - prev[0] >= MIN_CUE_DUR:
                    prev[1] = limit
                else:
                    cur[0] = prev[1] + 0.03

    @staticmethod
    def _ts(x):
        ms = int(round(max(0.0, x) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

    def flush(self, bilingual=False):
        with self.lock:
            if not self.dirty:
                return False
            lines = []
            for i, (s, e, zh, orig) in enumerate(self.cues, 1):
                text = zh or orig
                if bilingual and zh and orig and zh != orig:
                    text = zh + "\n" + orig
                # 最后一道保险：SRT 里出现 end<=start 会让部分播放器整条不显示
                if e <= s:
                    e = s + MIN_CUE_DUR
                lines.append("%d\n%s --> %s\n%s\n" % (i, self._ts(s), self._ts(e), text))
            tmp = SRT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines))
            os.replace(tmp, SRT_PATH)
            self.dirty = False
            return True


# ------------------------------------------------------------------ 插件开关
class PluginPrefs:
    """读 Win-Box 插件设置面板的值（`<userData>/plugin-settings.json`）。

    宿主存储结构：`{ "<pluginId>": { "<panelId>": { "<fieldKey>": value } } }`
    （见 main.cjs `pluginSettingsAccess()` 的 `pluginId → panelId → fieldKey` 分桶）

    ★ 语义约定：**缺值 = 开启**，且与 manifest 的 `default: true` 保持一致。
      两处必须对齐，否则会出现「界面显示关、字幕却出来了」这种鬼故事：
      · 宿主只在用户**动过**开关时才写这个文件（读回时缺值回落 manifest 的 default）；
      · 所以「文件里没有 enabled」在界面上显示为 **开**（default=true）。
      故 Python 侧也必须把 None 当 True —— 少一边对齐，两边就打架。
      只有用户**明确关掉**（存了 false）才停。
    """

    def __init__(self, path=None):
        self.path = path or default_prefs_path()
        self.mtime = None
        self.data = {}
        self.reload()

    def reload(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self.mtime, self.data = None, {}
            return
        if self.mtime is not None and self.mtime == st.st_mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f) or {}
            self.mtime = st.st_mtime
        except Exception:
            pass

    def _field(self, key):
        self.reload()
        bucket = self.data.get(PLUGIN_ID) or {}
        return (bucket.get(PANEL_ID) or {}).get(key)

    @property
    def installed(self):
        """插件是否装进了 Win-Box。

        ★ 不能看 plugin-settings.json：宿主只在用户**拨过开关**时才写这个文件，
          所以「文件不存在」≠「插件没装」，而是「装了、开关保持默认开」。
          旧实现据此打出「未安装，按常开处理」，排查时非常误导。
          正确判据是插件目录里的 plugin.json 在不在。
        """
        try:
            # self.path = <Win-Box>/data/plugin-settings.json
            # 插件实际在 <Win-Box>/data/plugins/<id>.winbox/plugin.json
            plugins_dir = os.path.join(os.path.dirname(self.path), "plugins")
            return os.path.isfile(os.path.join(plugins_dir, PLUGIN_ID + ".winbox",
                                              "plugin.json"))
        except Exception:
            return False

    @property
    def enabled(self):
        v = self._field("enabled")
        return True if v is None else bool(v)

    @property
    def bilingual(self):
        return self._field("mode") == "bilingual"


# ------------------------------------------------------------------ 主服务
class Service:
    def __init__(self, args):
        self.args = args
        self.asr = Asr(args.threads)
        self.tr = Translator(args.provider, key=args.api_key, url=args.base_url, model=args.model)
        self.sub = Subtitle()
        self.prefs = PluginPrefs(getattr(args, "prefs", None))
        self.active = None          # None=还没判定过；True/False = 开关当前状态
        self.link = None
        self.pull = None
        self.cur_path = None
        self.stream_base = 0.0      # 本段拉流对应视频里的绝对起点
        self.consumed = 0           # 本段已消费的秒数
        # 音频来源：回环（默认）或拉流。to_abs 把「段在音频流里的相对秒数」
        # 换算成「片子里的绝对秒数」，两种模式算法不同，切换时替换掉。
        self.loop = None
        self.pos_samples = collections.deque(maxlen=3000)   # (墙钟, mpv time-pos)
        self.cur_pos = 0.0                                   # 当前播放位置（秒）
        self.vad_base = 0.0        # 切句器缓冲的起点（对应已喂音频的第几秒）
        self.to_abs = lambda r: self.stream_base + r
        # ★ 翻译队列：主循环只往这里丢，绝不在主循环里等网络。
        #   实测 GLM 单条最慢 17 秒（那次是模型跑飞），同步跑会把识别一起卡死。
        self.trq = queue.Queue()
        self.tr_workers = []
        self.tr_inflight = 0
        self.tr_lock = threading.Lock()
        self.last_reload = 0.0
        self.restart_streak = 0
        self.last_restart = 0.0
        self.follow_pause_until = 0.0
        self.last_pos = None
        self.last_pos_t = 0.0
        self.speed = 1.0          # 播放倍速（mpv 的 speed），判跳转要用
        self.speed_t = 0.0
        self.upgrade_t = 0.0      # 设备回环 → 进程回环的升级检查节流
        self.poll_t = 0.0         # 读 mpv 状态的节流（10Hz）
        self.last_paused = False
        self.stop = False
        # ★ 短句合并缓冲：VAD 会把一句话切成「欲し。」+「が終わるまで。」，
        #   逐片送模型必然翻成断句。把相邻短碎片拼起来再翻译，译文才连贯。
        self.pending = None       # {"s","e","parts","t"}
        self.pending_lock = threading.Lock()

    # --- 连接维护
    def ensure_link(self):
        if self.link:
            if not self.link.dead:
                return self.link
            # 连接被对端关掉（最常见原因：别的脚本连了同一个 IPC 管道）→ 重连
            log("mpv 管道连接已断开，重连中…")
            try:
                self.link.close()
            except Exception:
                pass
            self.link = None
        for name in MpvLink.find():
            try:
                l = MpvLink(name)
            except OSError:
                continue
            p = l.get("path")
            if p:
                log("已连接 mpv 管道：%s", name)
                self.link = l
                return l
            l.close()
        return None

    def drain(self, link=None):
        """把 VAD 里最后一段逼出来（片尾没有静音段时不会自动吐出）。"""
        try:
            self.asr.vad.flush()
        except Exception:
            pass
        segs = self.asr.take()
        if segs:
            self.handle_segments(segs)
        # 停播/换片时把攒着的短碎片也吐出去，否则最后半句永远不显示
        self._flush_pending()
        # 停播/换片时把在途翻译等回来，否则尾段只留原文
        if segs or self.tr_inflight:
            self.wait_translate(8.0)

    # --- 音频来源：回环（默认，优先进程级）
    def start_loopback(self):
        mode = (getattr(self.args, "source", "auto") or "auto").lower()
        if mode == "pull":
            log("音频来源：拉流（显式指定 --source pull）")
            return False
        pid = None
        for n in MpvLink.find():
            pid = mpv_pid_from_pipe(n)
            if pid:
                break
        if mode == "process" and not pid:
            log("找不到 mpv 管道，无法定位播放器进程，退回设备回环")
        plan = []
        if mode in ("auto", "process") and pid:
            plan.append(pid)
        if mode in ("auto", "device", "loopback", "process"):
            plan.append(None)          # None = 设备回环（兜底）
        for p in plan:
            try:
                lb = LoopbackSource(pid=p).start()
            except Exception as e:
                log("%s回环不可用：%s", "进程" if p else "设备", str(e)[:90])
                continue
            self.loop = lb
            self.to_abs = self.loop_abs
            log("音频来源：%s", lb.name)
            return True
        log("回环都不可用，退回拉流（会额外占用带宽）")
        return False

    def loop_abs(self, rel):
        """段在「当前切句器缓冲」里的相对秒数 → 片子里的绝对秒数。

        ★★ 必须加上 vad_base：`vad.reset()` 之后，Asr.take() 返回的 seg.start
        是**从 0 重新开始**的，而 `loop.fed` / `marks` 是服务启动以来一直累加的。
        2026-11-12 踩过：换片时 reset 了 VAD，但没补这个基准，于是刚说的话
        被算成片子开头第 3 秒，全部判成「迟到」丢掉——日志里
        `丢弃迟到字幕（当前 144.4，句末 3.9）` 就是这个。
        """
        return self.pos_at_wall(self.loop.wall_at(rel + self.vad_base))

    def pos_at_wall(self, w):
        best = None
        for tw, p in reversed(self.pos_samples):
            if tw <= w:
                best = (tw, p)
                break
        if best is None:
            if not self.pos_samples:
                return 0.0
            tw, p = self.pos_samples[0]
            return max(0.0, p - (tw - w))
        return max(0.0, best[1] + (w - best[0]))

    def _reset_engine(self, clear_ctx=False):
        """丢弃切句器里残留的音频，并（可选）清空翻译上下文。
        跳转/换片必须做，否则会把「跳前」和「跳后」的音频拼成一句话。"""
        try:
            self.asr.vad.reset()
        except Exception:
            try:
                self.asr = Asr(self.args.threads)
            except Exception:
                pass
        # reset 之后 seg.start 从 0 重来，记住此时已喂到第几秒（见 loop_abs 的注释）
        try:
            self.vad_base = self.loop.fed if self.loop else 0.0
        except Exception:
            self.vad_base = 0.0
        if clear_ctx:
            try:
                with self.tr.ctx_lock:
                    self.tr.ctx.clear()
            except Exception:
                pass

    def restart_pull(self, path, pos):
        if self.pull:
            self.pull.stop()
            self.pull = None
        self.to_abs = lambda r: self.stream_base + r
        try:
            self.asr.vad.reset()
        except Exception:
            self.asr = Asr(self.args.threads)
        self.stream_base = max(0.0, pos - 0.3)
        self.consumed = 0.0
        self.last_pos = pos
        self.last_pos_t = time.time()
        # ★★ 这里必须把**原始** path 交给 Puller，不能先 source_url() 解开。
        #   解开后就只剩裸 CDN 地址，relay_params() 认不出来 ⇒ 中继不启动
        #   ⇒ Referer/Origin 丢掉 ⇒ 403 或无限挂住（2026-11-11 踩过）。
        #   解包交给 Puller.start_proc() 内部按需处理。
        self.pull = Puller(path, self.stream_base, "")
        try:
            self.pull.start_proc()
        except Exception as e:
            log("拉流失败：%s", e)
            self.pull = None
            return
        log("开始拉流 base=%.1fs → %s", self.stream_base, source_url(path)[0][:110])

    # --- 处理段
    def handle_segments(self, segs):
        for rel_start, audio in segs:
            # rel_start 是「在本段音频流里的相对秒数」；换算成片子里的绝对时间
            # 由 self.to_abs 决定：拉流模式是 stream_base + rel，
            # 回环模式要反查墙钟再查 mpv 的 time-pos（见 LoopbackSource 注释）。
            abs_start = self.to_abs(rel_start)
            t0 = time.time()
            r = self.asr.recognize(audio)
            txt = (r.text or "").strip()
            lang = str(getattr(r, "lang", "")).strip("<|>") or "?"
            if not txt:
                continue
            # ★ 「只有标点」的段直接丢：VAD 偶尔会把一声气息切成一段，
            #   SenseVoice 输出「。」，再走一次翻译就变成一个只有句号的字幕。
            if not txt.strip("。．.！？!?…、,，·~～ 　"):
                continue
            cues = split_cues(r.tokens, r.timestamps, abs_start)
            if not cues:
                continue
            # ★ 过滤要放在**切句之后**：整段文本可能是正常句子，但 split_cues
            #   会切出一条只有「。」的碎片（2026-11-12 实测），它仍会去翻译、上屏。
            cues = [c for c in cues if c[2].strip("。．.！？!?…、,，·~～ 　")]
            if not cues:
                continue
            # ★ 不再凭单次语种判定跳过翻译。
            #   SenseVoice 在 ≤1s 的碎片上 LID 极不可靠：实测日语片里
            #   冒出 en 5 条 / zh 2 条 / yue 1 条，判成 zh 就「不翻译直接上屏原文」，
            #   用户看到的就是「这句话没翻译」。
            #   改成：只有**文本本身确实是中文汉字**（且不含假名/韩文）才跳过；
            #   并且短碎片一律照常送翻译（模型对中文会原样返回，代价只是一次请求）。
            def _is_chinese_text(t):
                core = t.strip("。．.！？!?…、,，·~～ 　")
                if not core or len(core) < 2:
                    return False
                if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", core):   # 假名 / 韩文
                    return False
                han = sum(1 for c in core if "\u4e00" <= c <= "\u9fff")
                return han >= 2 and han == len(
                    [c for c in core if not c.isspace()])

            for (s, e, t) in cues:
                if is_noise(t):
                    continue
                if _is_chinese_text(t):
                    self._flush_pending()
                    self.sub.add([[s, e, t, t]], min_end=self.cur_pos + HOLD_AFTER,
                                 now_pos=self.cur_pos)
                else:
                    self._queue_or_merge(s, e, t)
            log("识别 %s %.2fs → %s", lang, time.time() - t0, txt[:60])

    # --- 短句合并：把 VAD 切碎的相邻片段拼成一句再翻译
    def _queue_or_merge(self, s, e, text):
        core_len = len(re.sub(r"[\s。．.！？!?…、,，·~～]", "", text))
        # ★ 不能一看到句末标点就当完整句：SenseVoice 对**每个碎片**都会补「。」，
        #   实测「欲し。」「が終わるまで。」都是半句。只有够长才算完整。
        ends = core_len >= 8 and bool(re.search(r"[。．.！？!?…]$", text.strip()))
        with self.pending_lock:
            p = self.pending
            # 长句 / 带句末标点的完整句 → 先把攒着的吐出去，自己直接入队
            if core_len >= 12 or ends:
                self._flush_pending_locked()
                self.enqueue_translate(s, e, text)
                return
            if p is None:
                self.pending = {"s": s, "e": e, "parts": [text], "t": time.time()}
                return
            # 间隔太大说明是另一句了，先吐旧的
            if s - p["e"] > 1.6 or len(p["parts"]) >= 3:
                self._flush_pending_locked()
                self.pending = {"s": s, "e": e, "parts": [text], "t": time.time()}
                return
            p["parts"].append(text)
            p["e"] = e
            p["t"] = time.time()

    def _flush_pending_locked(self):
        p = self.pending
        if not p:
            return
        self.pending = None
        # 拼接时去掉碎片自带的句末句号（否则「欲し。が終わるまで。」读起来像两句）
        parts = [re.sub(r"[。．.！？!?…]+$", "", p).strip() for p in p["parts"]]
        text = "".join(parts).strip()
        if text and not re.search(r"[。．.！？!?…]$", text):
            text += "。"
        if text:
            self.enqueue_translate(p["s"], p["e"], text)

    def _flush_pending(self):
        with self.pending_lock:
            self._flush_pending_locked()

    def tick_pending(self):
        """主循环每轮调用：攒够 0.9 秒还没等到下一片就吐出去，别把字幕憋没了。"""
        with self.pending_lock:
            if self.pending and time.time() - self.pending["t"] > 0.9:
                self._flush_pending_locked()

    # --- 翻译：后台线程池
    def start_workers(self):
        n = max(1, int(getattr(self.args, "workers", 3) or 3))
        for i in range(n):
            th = threading.Thread(target=self._tr_worker, name="tr-%d" % i, daemon=True)
            th.start()
            self.tr_workers.append(th)
        log("翻译线程：%d 个（%s）", n, self.tr.describe())

    def enqueue_translate(self, s, e, text):
        # ★ 连 Subtitle 实例一起带上：换片时 self.sub 会被整体替换，
        #   在途的翻译结果必须写回它入队时那一个，否则会把上一部片子的句子
        #   塞进新片子的字幕里。
        sub = self.sub
        with self.tr_lock:
            self.tr_inflight += 1
        self.trq.put((sub, s, e, text))

    def _tr_worker(self):
        while not self.stop:
            try:
                sub, s, e, text = self.trq.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                _t0 = time.time()
                zh = self.tr._one(text)
                _dt = time.time() - _t0
                # ★ 翻译往返通常 1~4 秒，句子早就播过去了；
                #   用「当前进度 + HOLD_AFTER」兜住结束时间，否则字幕写入即过期。
                sub.add([[s, e, zh or text, text]], min_end=self.cur_pos + HOLD_AFTER,
                                now_pos=self.cur_pos)
                if zh:
                    log("译文 %.2fs [%s] → %s", _dt, text[:24], zh[:34])
            except Exception as ex:
                log("翻译线程异常：%s", ex)
                sub.add([[s, e, text, text]], min_end=self.cur_pos + HOLD_AFTER,
                                now_pos=self.cur_pos)
            finally:
                with self.tr_lock:
                    self.tr_inflight -= 1

    def wait_translate(self, timeout=20.0):
        """等翻译队列排空（停播/退出时用），避免尾段字幕丢译文。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.tr_lock:
                if self.tr_inflight <= 0:
                    return True
            time.sleep(0.15)
        return False

    # --- 插件开关
    def bilingual(self):
        return bool(self.args.bilingual or self.prefs.bilingual)

    def apply_toggle(self):
        """按插件面板的开关决定干不干活。返回 True = 应工作。

        关闭时负责收尾：断流、摘掉外部字幕轨、清掉当前片的缓存，
        这样再次打开会从"当前播放位置"重新开始，而不是接着旧字幕往下走。
        """
        try:
            on = self.prefs.enabled
        except Exception:
            on = True
        if on == self.active:
            return on
        self.active = on
        if on:
            log("AI 字幕：已开启")
            if self.prefs.bilingual:
                log("显示方式：中文 + 原文（双语）")
        else:
            busy = bool(self.pull) or self.sub.injected
            if busy:
                log("AI 字幕：已关闭 → 停止识别并摘除字幕")
            else:
                log("AI 字幕：已关闭（等开关打开）")
            if self.pull:
                self.pull.stop()
                self.pull = None
            if self.loop:
                self.loop.playing.clear()
            if self.sub.injected and self.link:
                try:
                    self.link.cmd(["sub-remove"], timeout=3)
                    self.sub.injected = False
                    log("外部字幕轨已摘除")
                except Exception:
                    pass
            self.sub = Subtitle()
            self.cur_path = None        # 再次开启时按新片重来
        return on

    # --- 主循环
    def run(self):
        log("服务启动（模型已加载）")
        log("翻译后端：%s", self.tr.describe())
        if not self.tr.enabled:
            log("提示：设置 ZHIPU_API_KEY（免费）或 SILICONFLOW_API_KEY 即可出中文字幕")
        self.start_workers()
        log("插件开关：%s（%s）", self.prefs.path,
            "已安装" if self.prefs.installed else "未安装，按常开处理")
        self.start_loopback()
        last_tick = 0
        last_hb = time.time()
        last_quiet = 0.0
        while not self.stop:
            # ★ 守护调度：Win-Box 关掉 / 插件开关关掉时，守护会写这个停止信号。
            #   必须走 break → shutdown()，才能摘掉外部字幕轨、停采集、释放模型；
            #   直接被 TerminateProcess 杀掉会留下一条残留字幕轨。
            if os.path.exists(GUARD_STOP_FILE):
                try:
                    with open(GUARD_STOP_FILE, encoding="utf-8") as f:
                        why = f.read().strip() or "守护请求停止"
                except Exception:
                    why = "守护请求停止"
                try:
                    os.remove(GUARD_STOP_FILE)
                except OSError:
                    pass
                log("%s → 字幕服务退出", why)
                break
            # ★ 插件开关：关掉时连 mpv 都不去连，省 CPU
            if not self.apply_toggle():
                time.sleep(1.0)
                continue
            link = self.ensure_link()
            if not link:
                if self.pull:
                    self.pull.stop()
                    self.pull = None
                if self.loop:
                    self.loop.playing.clear()
                time.sleep(2)
                continue
            # ★ 降低对 mpv 的轮询频率：原来主循环 0.04 秒一轮、每轮 3 次 IPC
            #   ⇒ 每秒 75 次属性读取，会和 mpv 的播放线程抢命令锁。
            #   音频消费仍然保持 0.04 秒的节奏，只是状态读取降到 10Hz。
            if time.time() - self.poll_t >= 0.1:
                self.poll_t = time.time()
                try:
                    path = link.get("path")
                    pos = link.get("time-pos")
                    paused = link.get("pause")
                    self.last_paused = bool(paused)
                except Exception:
                    path = None
            else:
                path, pos, paused = self.cur_path, self.cur_pos, self.last_paused
            if link and time.time() - self.speed_t > 1.0:
                sp = link.get("speed")
                if isinstance(sp, (int, float)) and sp > 0:
                    self.speed = float(sp)
                self.speed_t = time.time()
            # ★ 让采集始终跟着**当前**的播放器进程树：
            #   ① 启动时播放器没起 ⇒ 只能降级「设备回环」（会混音），管道一出现就升级；
            #   ② **Win-Box 重启后主进程号会变**（实测 25016 → 27932），
            #      旧的进程回环还在盯已死的进程树 ⇒ 电平恒为 0、永远不出字幕。
            #      所以 pid 一变也必须重建。
            if self.loop and time.time() - self.upgrade_t > 5:
                self.upgrade_t = time.time()
                want = mpv_pid_from_pipe(link.name) if link else None
                if want and not (self.loop.mode == "process" and self.loop.pid == want):
                    try:
                        lb = LoopbackSource(pid=want).start()
                        old = self.loop
                        self.loop = lb
                        old.stop()
                        self.pos_samples.clear()
                        self._reset_engine(clear_ctx=True)
                        self.to_abs = self.loop_abs
                        log("音频来源已切到进程回环 · pid %d（只抓播放器自己的声音）", want)
                    except Exception as e:
                        log("切换进程回环失败（pid %s）：%s", want, str(e)[:70])
            if not path or pos is None:
                if self.pull:
                    log("播放已停止，吐出尾段并断开拉流")
                    self.drain()
                    self.pull.stop()
                    self.pull = None
                    if self.sub.dirty and self.link:
                        self.sub.flush(self.bilingual())
                        self.inject(self.link, self.cur_path)
                if self.loop:
                    self.loop.playing.clear()
                if not self.link or self.link.dead:
                    # 管道断了就别闷着：让 ensure_link 立刻重连
                    try:
                        self.link.close()
                    except Exception:
                        pass
                    self.link = None
                # ★ 这条分支以前是「静默 continue」，一旦播放器停住或者管道断了，
                #   日志会一个字都不出，看着像服务死了。改成 15 秒报一次状态。
                if time.time() - last_hb > 15:
                    last_hb = time.time()
                    log("等待播放（管道%s，已挂载字幕轨=%s）",
                        "已断开" if (not self.link or self.link.dead) else "正常",
                        "是" if self.sub.injected else "否")
                self.last_pos = None      # 同上：离开播放时清基准
                self.last_pos_t = 0.0
                time.sleep(1)
                continue

            # ★ 回环模式：只要在播就持续采样 (墙钟, time-pos)，
            #   出句时用它把「音频流里的相对秒数」翻回片子里的绝对时间。
            self.cur_pos = pos
            if self.loop and not paused:
                self.pos_samples.append((time.time(), pos))

            if path != self.cur_path:
                old = self.sub
                self.cur_path = path
                self.sub = Subtitle()
                log("检测到媒体：%s", path[:120])
                if self.loop:
                    self.loop.playing.set()
                    self.pos_samples.clear()
                    self.pos_samples.append((time.time(), pos))
                    self.stream_base = pos
                    self.to_abs = self.loop_abs
                    self.last_pos = pos
                    self._reset_engine(clear_ctx=True)
                else:
                    self.restart_pull(path, pos)
                # 换片时把上一部片子的外部字幕轨摘掉（mpv 换文件本身也会丢，但显式清更干净）
                if old.injected and self.link:
                    try:
                        self.link.cmd(["sub-remove"])
                    except Exception:
                        pass
            elif paused:
                if self.loop:
                    self.loop.playing.clear()
                    self.drain()
                    if self.sub.dirty and self.link:
                        self.sub.flush(self.bilingual())
                        self.inject(self.link, self.cur_path)
                elif self.pull:
                    self.drain()
                    self.pull.stop()
                    self.pull = None
                    if self.sub.dirty and self.link:
                        self.sub.flush(self.bilingual())
                        self.inject(self.link, self.cur_path)
                # ★ 离开播放状态时把基准清掉：否则恢复播放后第一次比较，
                #   会拿「几十秒前的 last_pos_t」去算 dt，得出一个巨大的期望位移，
                #   于是 0.5→0.6 这种微小平移也被判成「跳转」并重置切句器
                #   （2026-11-12 实测：`播放位置跳转 0.5→0.6（86.1 秒内）`）。
                self.last_pos = None
                self.last_pos_t = 0.0
                time.sleep(0.6)
                continue
            elif self.loop:
                # 回环不需要「重启」，但拖动进度必须重置切句器：
                # VAD 里残留的是「跳之前」的音频，不清掉会把跳前跳后拼成一句。
                # （对应 TV-AutoBuild 里那句 `pcm timeline gap resetVad gapUs=`）
                #
                # ★★ 判据必须是**变化率**，不能只看位置差：
                #   2026-11-11 实测踩坑 —— 主循环有时候一次迭代耗时好几秒，
                #   这几秒里位置自然前进 5 秒，用 `abs(pos-last_pos) > 3` 就**每秒误判成跳转**，
                #   于是 VAD 被反复 reset，90 秒一条字幕都出不来（日志里一串
                #   「播放位置跳转 1516.2→1521.4」就是这个）。
                #   正确做法：拿「实际前进」和「按倍速应该前进」比较。
                self.loop.playing.set()
                now = time.time()
                if self.last_pos is not None and self.last_pos_t:
                    dt = now - self.last_pos_t
                    dp = pos - self.last_pos
                    expect = dt * (self.speed or 1.0)
                    if dt > 0.2 and abs(dp - expect) > max(2.5, expect * 0.5):
                        log("播放位置跳转 %.1f→%.1f（%.1f 秒内）重置切句与翻译上下文",
                            self.last_pos, pos, dt)
                        self._reset_engine(clear_ctx=True)
                self.last_pos, self.last_pos_t = pos, now
            elif not self.pull:
                self.restart_pull(path, pos)
            elif time.time() > self.follow_pause_until:
                # ★ 不能拿「拉流位置 vs 播放位置」判断 seek：ffmpeg 启动要 1~2 s，
                #   永远落后，会无限重启并把音频切碎（实测每 3 s 重启一次）。
                #   改用「播放位置的变化率」：位置跳变与墙钟不符才算 seek。
                now = time.time()
                if self.last_pos is not None and self.last_pos_t:
                    dt = now - self.last_pos_t
                    dp = pos - self.last_pos
                    if dt > 0.25 and abs(dp - dt) > FOLLOW_TOL:
                        self.restart_streak = self.restart_streak + 1 if now - self.last_restart < 2.5 else 1
                        self.last_restart = now
                        if self.restart_streak > 4:
                            log("连续跳变过多，位置跟随暂停 6 秒")
                            self.follow_pause_until = now + 6
                            self.restart_streak = 0
                        else:
                            log("播放位置跳变 %.1fs（seek），重新拉流", dp - dt)
                            self.restart_pull(path, pos)
                self.last_pos, self.last_pos_t = pos, now

            if self.loop:
                got = 0
                while got < 120:
                    try:
                        chunk = self.loop.q.get_nowait()
                    except queue.Empty:
                        break
                    self.asr.feed(chunk)
                    got += 1
                segs = self.asr.take()
                if segs:
                    self.handle_segments(segs)
            elif self.pull:
                got = 0
                while got < 40:
                    try:
                        chunk = self.pull.q.get_nowait()
                    except queue.Empty:
                        break
                    self.asr.feed(chunk)
                    self.consumed += len(chunk) / float(SR)
                    got += 1
                segs = self.asr.take()
                if segs:
                    self.handle_segments(segs)
            self.tick_pending()          # 短句合并超时兜底，if/elif 之外每轮都走

            if time.time() - last_tick > 0.2:
                last_tick = time.time()
                if self.sub.dirty:
                    if self.sub.flush(self.bilingual()):
                        self.inject(link, path)

            # ★ 心跳：以前管道一断就「静默空转」，日志一个字不出，看不出卡在哪。
            if time.time() - last_hb > 15:
                last_hb = time.time()
                log("心跳 pos=%.1f pause=%s fed=%.1fs 队列=%d 电平=%s 字幕=%s 落后=%s 最大=%s",
                    pos or 0, paused,
                    self.loop.fed if self.loop else (self.consumed if self.pull else 0),
                    self.loop.q.qsize() if self.loop else (self.pull.q.qsize() if self.pull else 0),
                    ("%.4f/%.3f" % (self.loop.rms, self.loop.peak)) if (self.loop and self.loop.rms is not None) else "-",
                    "已挂载" if self.sub.injected else "未挂载",
                    "%.1fs" % self.sub.lag if self.sub.lag is not None else "-",
                    "%.1fs" % self.sub.lag_max)
                # 采集到的几乎是静音：多半是播放器在缓冲/被静音，不是识别坏了。
                if (self.loop and self.loop.rms is not None and not paused
                        and self.loop.rms < 0.0008 and self.loop.peak < 0.01
                        and time.time() - last_quiet > 180):
                    last_quiet = time.time()
                    log("采集电平接近静音（RMS %.4f）：播放器可能在缓冲或已静音，"
                        "此时不会产生字幕，属正常现象", self.loop.rms)

            time.sleep(0.04)

        self.shutdown()

    def _drop_own_tracks(self, link):
        """把以前挂上去、指向本目录的外挂字幕轨全部摘掉。

        ★ 为什么必须有这一步：`sub-add` 是「追加」，不是「替换」。
          每次重启服务、每次换片都会再加一条，于是播放器的字幕菜单里
          越堆越多同名 `live.srt · subrip · 外挂`（2026-11-12 实测截图如此）。
          这里只清 `_live` 目录下的（也就是我们自己产生的），用户自己加载的别动。
        """
        try:
            tl = link.get("track-list") or []
        except Exception:
            return 0
        home = os.path.normcase(os.path.dirname(SRT_PATH))
        drop = []
        for t in tl:
            if t.get("type") != "sub" or not t.get("external"):
                continue
            f = os.path.normcase(t.get("external-filename") or "")
            if f and os.path.dirname(f) == home:
                drop.append(t.get("id"))
        for i in drop:
            link.cmd(["set_property", "sid", i], timeout=2)
            link.cmd(["sub-remove"], timeout=2)
        return len(drop)

    def inject(self, link, path):
        if not self.sub.injected:
            n = self._drop_own_tracks(link)
            if n:
                log("清掉旧的外挂字幕轨 %d 条", n)
            r = link.cmd(["sub-add", SRT_PATH, "select"], timeout=3)
            if r.get("error") == "success":
                self.sub.injected = True
                log("字幕轨已挂载")
            else:
                log("sub-add 失败：%s", r)
        else:
            link.cmd(["sub-reload"], timeout=3)

    def shutdown(self):
        log("退出中…")
        if self.loop:
            self.loop.stop()
        if self.pull:
            self.pull.stop()
        if self.link:
            try:
                self.link.cmd(["sub-remove"])
            except Exception:
                pass
            self.link.close()


def check_key(args):
    """只验翻译后端可用性，不启动字幕服务。退出码 0=可用 2=未配置 3=不可用。"""
    tr = Translator(args.provider, key=args.api_key, url=args.base_url, model=args.model)
    print("翻译后端：%s" % tr.describe())
    if not tr.enabled:
        print("没有找到 key。可设置下面任一环境变量后再试：")
        for k, cfg in PROVIDERS.items():
            print("    set %-20s = <你的 key>      → %s" % (cfg["env"], cfg["label"]))
        return 2

    print("接口地址：%s" % tr.url)
    print("模型    ：%s" % tr.model)
    # 不打印 key 本身。只报「长度」和「是否带点」——这两个才是判断复制是否完整的依据。
    print("key     ：已读取（%d 位%s）"
          % (len(tr.key), "，含分隔点" if "." in tr.key else "，**不含分隔点**"))
    print("\n发一条测试请求……")

    samples = ["こんにちは、元気ですか？", "I'll be back.", "안녕하세요"]
    t0 = time.time()
    txt, err = tr._post(samples)
    dt = time.time() - t0

    if err:
        print("\n[失败] 耗时 %.2fs" % dt)
        print("       %s" % err)
        low = err.lower()
        print("\n可能原因与处理：")
        if "401" in err or "invalid" in low or "验证不正确" in err or "过期" in err:
            print("  · key 不对。智谱的 key 形如 xxxxxxxx.yyyyyyyy —— **中间那个点不能丢**，")
            print("    长度明显短于 40 位基本就是复制漏了。请到智谱控制台 → API Key 管理重新复制整串。")
            print("  · 也可能是该 key 已被删除/重置，或账号没完成实名认证。")
        elif "402" in err or "余额" in err or "quota" in low or "insufficient" in low:
            print("  · 额度用完了。智谱请用 glm-4-flash（免费）；或换 SILICONFLOW_API_KEY。")
        elif "429" in err or "频率" in err or "rate" in low:
            print("  · 触发限流。等一会儿再试，或换后端。")
        elif "403" in err:
            print("  · 无权限：账号未实名，或该模型没开通。")
        else:
            print("  · 先确认本机能直连该域名（代理可能拦）；再确认 key 与环境变量名对不对。")
        return 3

    print("\n[成功] 耗时 %.2fs" % dt)
    print("原文：%s" % " ||| ".join(samples))
    print("译文：%s" % txt)
    return 0


# ------------------------------------------------------------------ 公共构件
# ★ overlay_sub.py（BieCuiLe 浮层）自己写了 consumer 循环，原本没接 AGC 和短句合并，
#   导致它和 Win-Box 线质量断层（碎片、逐词切碎、漏翻译）。下面把 AGC 与合并抽成模块级，
#   两条线共用同一份逻辑，避免日后漂移。
class Agc:
    """把回环音频拉到 VAD/识别器能稳定工作的电平（与 Service._agc 同算法）。"""
    def __init__(self):
        self.gain = 1.0
        self._silent_blocks = 0

    def process(self, x):
        rms = float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
        peak = float(np.abs(x).max()) if len(x) else 0.0
        if rms > 0.0015 and peak > 1e-4:
            want = min(12.0, max(1.0, 0.45 / peak))
            k = 0.05 if want > self.gain else 0.45     # 慢升快降
            self.gain = (1 - k) * self.gain + k * want
            self._silent_blocks = 0
        else:
            self._silent_blocks += 1
            if self._silent_blocks > 30:               # 连续 ~2s 静音，缓缓回落到 1
                self.gain = 0.995 * self.gain + 0.005 * 1.0
        return np.tanh(x * self.gain * 1.1) / 1.1


class SegMerger:
    """把 VAD 切碎的相邻短碎片拼成完整句再回调（与 Service._queue_or_merge 同策略）。"""
    def __init__(self, on_merged, timeout=0.6, min_merge=8, full=12, gap=1.6,
                 max_parts=2, max_span=3.0):
        self.on_merged = on_merged
        self.timeout = timeout
        self.min_merge = min_merge
        self.full = full
        self.gap = gap
        self.max_parts = max_parts
        # ★ 2026-11-13 主人反馈「长句对话要等几句话说完才出字幕」：
        #   旧策略只按「片数 3 / 间隔 1.6s」判 flush，连续对话时 3 片能跨 8~10 秒。
        #   加「音频总跨度上限」：攒到 max_span 秒就先发，宁可切短也不让字幕迟到。
        self.max_span = max_span
        self.pending = None
        self.lock = threading.Lock()

    def push(self, s, e, text):
        text = (text or "").strip()
        if not text:
            return
        core_len = len(re.sub(r"[\s。．.！？!?…、,，·~～]", "", text))
        # 只在够长时才把「句末标点」当完整句：SenseVoice 每个碎片都自带「。」，
        # 「欲し。」「が終わるまで。」其实是半句，误判会合并永远不触发。
        ends = core_len >= self.min_merge and bool(re.search(r"[。．.！？!?…]$", text.strip()))
        with self.lock:
            p = self.pending
            if core_len >= self.full or ends:
                self._flush_locked()
                self.on_merged(s, e, text)
                return
            if p is None:
                self.pending = {"s": s, "e": e, "parts": [text], "t": time.time()}
                return
            if (s - p["e"] > self.gap or len(p["parts"]) >= self.max_parts
                    or e - p["s"] >= self.max_span):
                self._flush_locked()
                self.pending = {"s": s, "e": e, "parts": [text], "t": time.time()}
                return
            p["parts"].append(text)
            p["e"] = e
            p["t"] = time.time()

    def _flush_locked(self):
        p = self.pending
        if not p:
            return
        self.pending = None
        parts = [re.sub(r"[。．.！？!?…]+$", "", x).strip() for x in p["parts"]]
        text = "".join(parts).strip()
        if text and not re.search(r"[。．.！？!?…]$", text):
            text += "。"
        if text:
            self.on_merged(p["s"], p["e"], text)

    def flush(self):
        with self.lock:
            self._flush_locked()

    def tick(self):
        with self.lock:
            if self.pending and time.time() - self.pending["t"] > self.timeout:
                self._flush_locked()


def is_pure_chinese(text):
    """基本全是汉字（允许标点/数字/空白）才判纯中文 → 不必送翻译。

    ★ 与 Service 旧逻辑等价，但改成模块级，overlay 也能用。
    含拉丁字母/假名/谚文/其他 CJK 区 → 一律送翻译（英文、日文、韩文都要翻）。
    """
    if not text:
        return False
    cnt = 0
    for ch in text:
        o = ord(ch)
        if o < 0x3000:
            if ch in " \t\n\r0123456789.,!?;:'\"":
                continue
            return False
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
            cnt += 1
        elif ch in "，。！？、；：""''（）《》…—～·【】「」『』":
            continue
        else:
            return False
    return cnt > 0


def is_noise(text):
    """识别层噪声过滤：单音节/极短外文碎片大概率是呼吸、停顿被误识，上屏只会刷屏。

    ★ 2026-11-12 实测：54 条识别里 ≤2 字碎片占 40%，其中 28% 是「あ。/ う。/ さの？」这类
       单假名噪声。它们要么单独上屏成满屏「啊/呜」，要么污染短句合并缓冲。
       含中文的短句（嗯/好/是）保留；纯假名/英文且有效字符 ≤2 的视为噪声丢弃。
    """
    c = re.sub(r"[\s。．.！？!?…、,，·~～]", "", text or "")
    if not c:
        return True
    if re.search(r"[一-鿿]", c):     # 含汉字 → 保留
        return False
    return len(c) <= 2


def single_instance():
    """开机自启 + 手动双击都可能触发，用命名互斥体保证只有一个 Win-Box 字幕服务。"""
    import ctypes
    k = ctypes.windll.kernel32
    k.CreateMutexW(None, False, "Global\\WinBoxLiveSubtitleMain")
    return k.GetLastError() != 183


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--workers", type=int, default=3, help="并发翻译线程数（每条字幕一个请求）")
    ap.add_argument("--bilingual", action="store_true", help="中文 + 原文两行")
    ap.add_argument("--provider", default=None,
                    choices=list(PROVIDERS.keys()) + ["custom"],
                    help="翻译后端；缺省自动选第一个有 key 的（zhipu → siliconflow → deepseek）")
    ap.add_argument("--api-key", default=None, help="覆盖环境变量里的 key")
    ap.add_argument("--base-url", default=None, help="配合 --provider custom 用")
    ap.add_argument("--model", default=None, help="覆盖默认模型名")
    ap.add_argument("--check-key", action="store_true", help="只测翻译后端是否可用，然后退出")
    ap.add_argument("--prefs", default=None, help="指定 plugin-settings.json 路径（缺省自动找 Win-Box 数据目录）")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "process", "device", "loopback", "pull"],
                    help="音频来源：process=只抓播放器进程（默认优先，别的程序出声不影响）；"
                         "device=抓整个声卡（老系统兜底，会混入其他声音）；"
                         "pull=用 ffmpeg 另拉一路流（吃额外带宽，源站慢时严重跟不上）")
    args = ap.parse_args()

    # 守护进程 + 手动双击 + 开机自启都可能触发，保证只有一个字幕服务在跑
    if not single_instance():
        log("Win-Box 字幕服务已在运行，本次启动忽略")
        return 0

    if args.check_key:
        return check_key(args)

    os.makedirs(WORK, exist_ok=True)
    open(LOG_PATH, "w", encoding="utf-8").close()

    svc = Service(args)
    try:
        svc.run()
    except KeyboardInterrupt:
        svc.stop = True
        svc.shutdown()
        log("已停止")
    return 0


if __name__ == "__main__":
    # ★ pythonw.exe 下没有控制台，未捕获异常会**无声消失**。
    #   这里把 traceback 落到 live.log，后台模式出问题才有据可查。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        try:
            os.makedirs(WORK, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as _f:
                _f.write("\n[FATAL] " + traceback.format_exc() + "\n")
        except Exception:
            pass
        sys.exit(1)
