# -*- coding: utf-8 -*-
"""
Win-Box 自动补丁工具（.py 源下载保留 ?tk= 鉴权参数）
================================================================

背景：客户端 PySpider.ensureReady() 用 api.split('?')[0] 去下载脚本，把订阅里的
      ?tk=<token> 丢掉，服务器回 28 字节的 "Access Denied: Missing Token"，
      被当成 .py 缓存 → Python 编译失败 → 「语法/编码错误」。

本工具直接改 resources/app.asar 里的 dist/main.cjs，做到：
  1. 下载用完整 api（含查询串）；
  2. 读写缓存前校验内容像不像 python，不再是脚本就删掉重下，错误页不落盘。

用法（PowerShell / cmd 里都行）：
    python winbox_patch.py                 # 打一次补丁（默认动作）
    python winbox_patch.py status          # 只看当前状态
    python winbox_patch.py launch          # 打补丁并启动客户端（做桌面快捷方式用这个）
    python winbox_patch.py watch           # 常驻守护：检测到 asar 变化（更新）就自动重打
    python winbox_patch.py restore         # 还原成上一个备份的原版
    python winbox_patch.py --asar <路径>    # 指定 app.asar
    python winbox_patch.py --restart        # 应用在跑时，先关掉再打（打完自动开回来）
    python winbox_patch.py --install-autostart / --uninstall-autostart

安全设计：
  · 幂等：已经是补丁版直接跳过，不重复改；
  · 只认精确代码锚点，上游代码一变就「不支持」退出，绝不乱改；
  · 改前把原版备份成 app.asar.orig，改后重新解析校验（条目/大小/hash/越界）；
  · 原子替换（写 .new 再 rename），失败不会留下半个文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time

MARK = 'looksLikePySource'
LOG_NAME = 'winbox_patch.log'

HELPER = '''function looksLikePySource(buf) {
  if (!buf || buf.length < 16) return false;
  const head = buf.slice(0, 300).toString("utf8").replace(/^\\uFEFF/, "").trimStart();
  if (!head) return false;
  if (head[0] === "<" || head[0] === "{" || head[0] === "[") return false;
  if (/^(access denied|forbidden|not found|unauthorized|denied|error|bad request|\\d{3}\\b)/i.test(head)) return false;
  return true;
}
'''

OLD_CACHE = '''      if ((0, import_node_fs15.existsSync)(this.pyPath) && (0, import_node_fs15.readFileSync)(this.pyPath).length > 0) {
        this.ready = true;
        return true;
      }
      const res = await this.host.http.request({ url: base, method: "get", timeoutMs: 6e4, buffer: 2 });'''

NEW_CACHE = '''      if ((0, import_node_fs15.existsSync)(this.pyPath)) {
        const cached = (0, import_node_fs15.readFileSync)(this.pyPath);
        if (looksLikePySource(cached)) {
          this.ready = true;
          return true;
        }
        try {
          (0, import_node_fs15.unlinkSync)(this.pyPath);
        } catch {
        }
      }
      const dlUrl = /^https?:\\/\\//i.test(this.api || "") ? this.api : base;
      const res = await this.host.http.request({ url: dlUrl, method: "get", timeoutMs: 6e4, buffer: 2 });'''

OLD_WRITE = '      (0, import_node_fs15.writeFileSync)(this.pyPath, buf);'

NEW_WRITE = '''      if (!looksLikePySource(buf)) {
        const snip = buf.slice(0, 80).toString("utf8").replace(/\\s+/g, " ").trim();
        this.loadError = `python 脚本地址返回的不是脚本（${buf.length} 字节：${snip}）—— 常见原因是缺少鉴权参数（如 ?tk=）或被防盗链拦截；已拒绝写入缓存`;
        return false;
      }
      (0, import_node_fs15.writeFileSync)(this.pyPath, buf);'''

ASAR_CANDIDATES = [
    r'D:\TVbox\Win-Box\resources\app.asar',
    os.path.expandvars(r'%LOCALAPPDATA%\Programs\win-box\resources\app.asar'),
    os.path.expandvars(r'%PROGRAMFILES%\Win-Box\resources\app.asar'),
    os.path.expandvars(r'%PROGRAMFILES(X86)%\Win-Box\resources\app.asar'),
]

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(HERE, 'app.asar.orig')
LOG_PATH = os.path.join(HERE, LOG_NAME)


# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    line = '%s  %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        print(line, flush=True)      # pythonw 下 stdout 为 None，不能让它把守护带崩
    except Exception:
        pass
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            try:
                os.replace(LOG_PATH, LOG_PATH + '.1')
            except OSError:
                open(LOG_PATH, 'w', encoding='utf-8').close()   # 换不动就清空
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------- asar 读写
def read_asar(path: str):
    with open(path, 'rb') as f:
        magic, s1, s2, json_len = struct.unpack('<IIII', f.read(16))
        if magic != 4:
            raise ValueError('不是 asar 文件（头部 magic=%d）' % magic)
        header = json.loads(f.read(json_len).decode('utf-8'))
        f.seek(0)
        data = f.read()
    return header, data, 8 + s1


def read_header_only(path: str):
    """只读头部（守护每 5 秒轮询用，别每次把 70MB 全读一遍）。"""
    with open(path, 'rb') as f:
        magic, s1, s2, json_len = struct.unpack('<IIII', f.read(16))
        if magic != 4:
            raise ValueError('不是 asar 文件')
        return json.loads(f.read(json_len).decode('utf-8')), 8 + s1


def read_member_at(path: str, data_start: int, meta: dict) -> bytes:
    with open(path, 'rb') as f:
        f.seek(data_start + int(meta['offset']))
        return f.read(meta['size'])


def walk(node, prefix=''):
    for name, meta in node.get('files', {}).items():
        p = prefix + name
        if 'files' in meta:
            yield from walk(meta, p + '/')
        else:
            yield p, meta


def read_member(data: bytes, data_start: int, meta: dict) -> bytes:
    off = int(meta['offset'])
    return data[data_start + off: data_start + off + meta['size']]


def asar_version(data: bytes, data_start: int, header: dict) -> str:
    try:
        pkg = read_member(data, data_start, header['files']['package.json'])
        return json.loads(pkg.decode('utf-8')).get('version', '?')
    except Exception:
        return '?'


def build_patched(header: dict, data: bytes, data_start: int, target='dist/main.cjs') -> bytes:
    entries = [(p, m) for p, m in walk(header) if not m.get('unpacked')]
    entries.sort(key=lambda x: int(x[1]['offset']))
    new_main = patched_code(read_member(data, data_start, dict(entries)[target]))
    body = bytearray()
    for p, m in entries:
        chunk = new_main if p == target else read_member(data, data_start, m)
        m['offset'] = str(len(body))
        m['size'] = len(chunk)
        if 'integrity' in m:
            bs = m['integrity'].get('blockSize', 4194304) or 4194304
            m['integrity']['hash'] = hashlib.sha256(chunk).hexdigest()
            m['integrity']['blocks'] = [hashlib.sha256(chunk[i:i + bs]).hexdigest()
                                        for i in range(0, len(chunk), bs)] or [m['integrity']['hash']]
        body += chunk
    hjson = json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    s1 = len(hjson) + 10
    pad = (8 + s1) - (16 + len(hjson))
    return struct.pack('<IIII', 4, s1, len(hjson) + 6, len(hjson)) + hjson + b'\x00' * pad + bytes(body)


def patched_code(main: bytes) -> bytes:
    src = main.decode('utf-8')
    anchor = '// src/engine/spider/PySpider.ts\n'
    if src.count(OLD_CACHE) != 1 or src.count(OLD_WRITE) != 1 or src.count(anchor) != 1:
        raise RuntimeError('代码锚点不匹配（版本可能已变化）')
    out = src.replace(anchor, anchor + HELPER, 1)
    out = out.replace(OLD_CACHE, NEW_CACHE, 1)
    out = out.replace(OLD_WRITE, NEW_WRITE, 1)
    return out.encode('utf-8')


def verify(new: bytes) -> None:
    """重新解析一遍，确认结构没坏。"""
    magic, s1, s2, json_len = struct.unpack('<IIII', new[:16])
    if magic != 4:
        raise ValueError('头部 magic 异常')
    header = json.loads(new[16:16 + json_len].decode('utf-8'))
    data_start = 8 + s1
    end = 0
    for p, m in walk(header):
        if m.get('unpacked'):
            continue
        end = max(end, int(m['offset']) + m['size'])
        if 'integrity' in m:
            chunk = new[data_start + int(m['offset']): data_start + int(m['offset']) + m['size']]
            if hashlib.sha256(chunk).hexdigest() != m['integrity']['hash']:
                raise ValueError('integrity 校验失败：%s' % p)
    if end != len(new) - data_start:
        raise ValueError('数据区长度不吻合（尾部多余/缺失）')


def state_of(asar: str) -> dict:
    header, ds = read_header_only(asar)
    meta = header['files']['dist']['files']['main.cjs']
    main = read_member_at(asar, ds, meta).decode('utf-8', 'replace')
    if MARK in main:
        s = 'patched'
    elif OLD_CACHE in main and OLD_WRITE in main:
        s = 'original'
    else:
        s = 'unsupported'
    return {'state': s, 'version': asar_version_at(asar, ds, header),
            'size': os.path.getsize(asar), 'path': asar}


def asar_version_at(asar: str, ds: int, header: dict) -> str:
    try:
        pkg = read_member_at(asar, ds, header['files']['package.json'])
        return json.loads(pkg.decode('utf-8')).get('version', '?')
    except Exception:
        return '?'


# ---------------------------------------------------------------- 应用进程
def app_exe(asar: str) -> str:
    root = os.path.dirname(os.path.dirname(asar))
    for name in os.listdir(root):
        if name.lower().endswith('.exe') and 'win-box' in name.lower():
            return os.path.join(root, name)
    return ''


def app_running() -> bool:
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Win-Box.exe'],
                             capture_output=True, timeout=10).stdout.lower()
        return b'win-box.exe' in out
    except Exception:
        return False


def live_running(asar: str) -> bool:
    """只有真正被加载的那份才受进程占用影响（测试副本随便改）。"""
    return is_live_asar(asar) and app_running()


def kill_app() -> None:
    subprocess.run(['taskkill', '/F', '/IM', 'Win-Box.exe'], capture_output=True, timeout=15)


# ---------------------------------------------------------------- 主操作
def is_live_asar(asar: str) -> bool:
    """真正被客户端加载的那份（<安装目录>\\resources\\app.asar）。测试副本不算。"""
    return os.path.basename(os.path.dirname(os.path.abspath(asar))).lower() == 'resources'


def write_over(path: str, data: bytes) -> str:
    """优先原子替换；某些环境/杀软会拦截 rename，退回就地写入。"""
    tmp = path + '.new'
    with open(tmp, 'wb') as f:
        f.write(data)
    try:
        os.replace(tmp, path)
        return 'replace'
    except OSError:
        with open(path, 'r+b') as f:
            f.write(data)
            f.truncate()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 'inplace'


def apply_patch(asar: str, restart: bool = False, backup: str = BACKUP) -> str:
    if not os.path.exists(asar):
        return 'missing'
    st = state_of(asar)
    if st['state'] == 'patched':
        return 'already'
    if st['state'] == 'unsupported':
        log('⚠ 这个版本的 main.cjs 找不到预期代码（作者可能已经自己修了），不做修改。')
        return 'unsupported'

    was_running = live_running(asar)
    if was_running:
        if not restart:
            log('⚠ 客户端正在运行，app.asar 被占用，本次跳过（关闭后再跑一次，或用 --restart）。')
            return 'locked'
        log('→ 关闭客户端以便替换 app.asar')
        kill_app()
        time.sleep(2)

    log('→ 正在给 %s（版本 %s）打补丁…' % (asar, st['version']))
    header, data, ds = read_asar(asar)
    new = build_patched(header, data, ds)
    verify(new)

    if not os.path.exists(backup):
        shutil.copy2(asar, backup)
        log('   已备份原版 → %s' % backup)
    else:
        try:
            if os.path.getsize(backup) != os.path.getsize(asar):
                shutil.copy2(asar, backup)
                log('   版本变化，已刷新原版备份')
        except OSError:
            pass

    tmp = asar + '.new'
    with open(tmp, 'wb') as f:
        f.write(new)
    try:
        mode = write_over(asar, new)
    except PermissionError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        log('⚠ 替换失败：文件被占用（客户端还开着）。')
        return 'locked'
    if mode == 'inplace':
        log('   （rename 被系统拦截，已改成就地写入）')
    try:
        if os.path.exists(asar + '.new'):
            os.remove(asar + '.new')
    except OSError:
        pass

    st2 = state_of(asar)
    ok = st2['state'] == 'patched'
    log('   %s 完成：%d → %d 字节，校验%s' % ('✅' if ok else '❌', st['size'], st2['size'],
                                          '通过' if ok else '失败'))
    if was_running and restart:
        exe = app_exe(asar)
        if exe:
            log('→ 重新启动客户端')
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
    return 'patched' if ok else 'failed'


def restore(asar: str, backup: str = BACKUP) -> str:
    if not os.path.exists(backup):
        log('没有备份文件：%s' % backup)
        return 'nobackup'
    if live_running(asar):
        log('→ 关闭客户端')
        kill_app()
        time.sleep(2)
    shutil.copy2(backup, asar)
    log('已还原原版 → %s' % asar)
    return 'restored'


def find_asar(explicit: str = '') -> str:
    if explicit:
        return os.path.abspath(explicit)
    for p in ASAR_CANDIDATES:
        if os.path.exists(p):
            return p
    return ''


_MUTEX = None


def single_instance() -> bool:
    """同一个用户下只允许一个守护（命名互斥体，进程退出自动释放）。"""
    global _MUTEX
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        _MUTEX = k32.CreateMutexW(None, False, 'Local\\WinBoxPatchGuard')
        if not _MUTEX:
            return True                      # 建不出来就不拦，宁可重复也不瘫痪
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def watch(asar: str) -> None:
    if not single_instance():
        log('已有守护在跑，本进程退出（避免两个进程同时改 app.asar）。')
        return
    log('守护启动：%s（每 5 秒检查一次）' % (asar or '等待 app.asar 出现'))
    last = None
    while True:
        try:
            if not asar or not os.path.exists(asar):
                found = find_asar()
                if found and found != asar:
                    asar = found
                    log('找到 app.asar：%s' % asar)
                else:
                    time.sleep(5)
                    continue
            sig = (os.path.getmtime(asar), os.path.getsize(asar))
            if sig != last:
                last = sig
                st = state_of(asar)
                label = {'patched': '已打补丁', 'original': '原版（需要打）',
                         'unsupported': '结构不认识'}[st['state']]
                log('检测到 app.asar 变化 → 版本 %s，%s' % (st['version'], label))
                if st['state'] == 'original':
                    if live_running(asar):
                        log('   客户端在运行，等它退出后再打…')
                    else:
                        apply_patch(asar)
                        last = (os.path.getmtime(asar), os.path.getsize(asar))
                elif st['state'] == 'unsupported':
                    log('   这个版本结构不认识（可能作者已修复），保持原样。')
            elif state_of(asar)['state'] == 'original' and not live_running(asar):
                apply_patch(asar)
                last = (os.path.getmtime(asar), os.path.getsize(asar))
            time.sleep(5)
        except KeyboardInterrupt:
            log('守护退出')
            return
        except Exception as e:
            log('守护异常：%s: %s' % (type(e).__name__, e))
            time.sleep(10)


def autostart(action: str) -> None:
    startup = os.path.join(os.path.expandvars(r'%APPDATA%'),
                           r'Microsoft\Windows\Start Menu\Programs\Startup')
    vbs = os.path.join(startup, 'WinBoxPatchGuard.vbs')
    if action == 'install':
        os.makedirs(startup, exist_ok=True)
        pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        py = sys.executable
        script = (
            'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'Set sh = CreateObject("WScript.Shell")\r\n'
            'py = "%s"\r\n'
            'If Not fso.FileExists(py) Then py = "%s"\r\n'
            'If Not fso.FileExists(py) Then py = "pythonw.exe"\r\n'
            'Do\r\n'
            '  sh.Run """" & py & """ """ & "%s" & """ watch", 0, True\r\n'
            '  WScript.Sleep 10000\r\n'
            'Loop\r\n'
            % (pyw, py, os.path.abspath(__file__))
        )
        with open(vbs, 'w', encoding='ascii', newline='') as f:
            f.write(script)
        log('已装开机守护（静默 + 自愈循环）：%s' % vbs)
    else:
        if os.path.exists(vbs):
            os.remove(vbs)
            log('已移除开机守护')
        else:
            log('没有安装过开机守护')


def main() -> int:
    args = sys.argv[1:]
    cmd = 'patch'
    asar_arg = ''
    restart = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--asar':
            asar_arg = args[i + 1]
            i += 2
            continue
        if a == '--restart':
            restart = True
        elif a == '--install-autostart':
            autostart('install')
            return 0
        elif a == '--uninstall-autostart':
            autostart('uninstall')
            return 0
        elif a in ('patch', 'status', 'watch', 'launch', 'restore'):
            cmd = a
        i += 1

    asar = find_asar(asar_arg)
    if not asar and cmd == 'watch':
        # 更新正在替换文件时会短暂「找不到」，守护不能就此退出，等它回来
        log('还没找到 app.asar（可能更新正在替换），守护继续等待…')
        watch('')
        return 0
    if not asar:
        log('找不到 app.asar，请用 --asar 指定路径')
        return 2

    st = state_of(asar)
    if cmd == 'status':
        log('路径 %s' % st['path'])
        log('版本 %s｜%d 字节｜状态 %s' % (st['version'], st['size'],
                                        {'patched': '已打补丁', 'original': '原版（未打）',
                                         'unsupported': '结构不认识'}[st['state']]))
        return 0
    if cmd == 'restore':
        return 0 if restore(asar) == 'restored' else 1
    if cmd == 'watch':
        watch(asar)
        return 0
    if cmd == 'launch':
        r = apply_patch(asar)
        if r == 'already':
            log('已是最新补丁版，直接启动。')
        elif r == 'locked':
            log('客户端已在运行：这次不改文件，关掉客户端后重开这个脚本即可打上。')
        exe = app_exe(asar)
        if exe and not app_running():
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
        return 0 if r in ('patched', 'already') else 1

    r = apply_patch(asar, restart=restart)
    if r == 'already':
        log('已是最新补丁版，无需处理（版本 %s）。' % st['version'])
    return 0 if r in ('patched', 'already') else 1


if __name__ == '__main__':
    sys.exit(main())
