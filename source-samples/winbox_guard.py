#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""winbox_guard.py — Win-Box 字幕服务的守护调度器（轻量，不加载任何模型）。

开机自启的是**这个守护**，不是字幕服务。它每 2 秒检查：

    Win-Box 进程在不在  AND  插件面板「启用 AI 字幕」是不是开着
        → 是：拉起 winbox_live_sub.py（若还没跑）
        → 否：写停止信号，让它优雅退出（会摘掉外部字幕轨）

这样字幕服务跟着 Win-Box 走，Win-Box 关了就释放内存；
插件开关关掉也立刻停，再打开自动恢复。

★ 刻意不 import winbox_live_sub：那个模块一旦导入就会预热/加载 SenseVoice 模型，
  守护要保持轻量（几 MB），否则等于开机就常驻一个 370MB 的服务。
"""
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "_live")
STOP_FILE = os.path.join(WORK, "guard.stop")
LOG = os.path.join(WORK, "guard.log")

PLUGIN_ID = "ai-subtitle"
PANEL_ID = "ai-sub"
MUTEX = "Global\\WinBoxLiveSubtitleMain"
PIPE_GLOB = r"\\.\pipe\winbox-mpv-*"

# 与 winbox_live_sub.default_prefs_path() 保持同一套推导
FFMPEG = r"D:\TVbox\Win-Box\resources\app.asar.unpacked\resources\ffmpeg\ffmpeg.exe"


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def find_pid(exe_name):
    """按进程名找 pid（CreateToolhelp32Snapshot，纯 ctypes，不依赖 psutil）。"""

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if snap == -1:
        return None
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.decode("mbcs", "ignore").lower() == exe_name.lower():
                return int(e.th32ProcessID)
            ok = k.Process32Next(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return None


def prefs_path():
    p = FFMPEG
    for _ in range(5):
        p = os.path.dirname(p)
    guess = os.path.join(p, "data", "plugin-settings.json")
    if os.path.isdir(os.path.dirname(guess)):
        return guess
    return r"D:\TVbox\Win-Box\data\plugin-settings.json"


def switch_on():
    """读插件面板开关。★ 缺值 = 开启（manifest 的 default 是 true，宿主只在用户
    拨过开关时才写这个文件），必须和 winbox_live_sub.PluginPrefs 保持同一语义。"""
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
        v = ((data.get(PLUGIN_ID) or {}).get(PANEL_ID) or {}).get("enabled")
        return True if v is None else bool(v)
    except Exception:
        return True


def winbox_running():
    # 进程名为准；mpv 管道只作为「正在播放」的补充信号
    return find_pid("Win-Box.exe") is not None or bool(glob.glob(PIPE_GLOB))


def service_alive():
    """用同名互斥体探测字幕服务是否活着（不依赖 pid 文件，守护重启也不会失联）。"""
    k = ctypes.windll.kernel32
    h = k.CreateMutexW(None, False, MUTEX)
    err = k.GetLastError()
    if h:
        k.CloseHandle(h)
    return err == 183          # ERROR_ALREADY_EXISTS


def start_service():
    try:
        os.remove(STOP_FILE)
    except OSError:
        pass
    env = dict(os.environ)
    # 密钥由 _autostart_winbox_guard.bat 里 call _key.bat 注入，这里只透传
    NOWIN = 0x08000000
    cmd = [sys.executable, "-X", "utf8",
           os.path.join(ROOT, "winbox_live_sub.py")]
    p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NOWIN)
    log("Win-Box 已启动 / 开关打开 → 拉起字幕服务 pid=%d" % p.pid)


def stop_service(reason):
    # 写停止信号，让服务走自己的 shutdown()：摘字幕轨、停采集、释放模型
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(STOP_FILE, "w", encoding="utf-8") as f:
            f.write(reason)
    except Exception as e:
        log("写停止信号失败：%s" % str(e)[:60])
    log("%s → 通知字幕服务退出" % reason)


def main():
    # 守护自身单实例
    k = ctypes.windll.kernel32
    k.CreateMutexW(None, False, "Global\\WinBoxSubtitleGuard")
    if k.GetLastError() == 183:
        log("守护已在运行，本次退出")
        return 0

    os.makedirs(WORK, exist_ok=True)
    log("守护启动：跟随 Win-Box 启停，受插件开关控制")
    log("插件设置：%s" % prefs_path())

    last_state = None
    while True:
        try:
            wb = winbox_running()
            sw = switch_on()
            alive = service_alive()
            want = wb and sw

            state = (wb, sw, alive)
            if state != last_state:
                log("Win-Box=%s 开关=%s 服务=%s" %
                    ("在" if wb else "不在", "开" if sw else "关",
                     "跑" if alive else "停"))
                last_state = state

            if want and not alive:
                start_service()
            elif not want and alive:
                if not wb:
                    stop_service("Win-Box 已关闭")
                else:
                    stop_service("插件开关已关闭")
        except Exception as e:
            log("守护循环异常：%s" % str(e)[:100])
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
