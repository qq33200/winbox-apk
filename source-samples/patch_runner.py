# -*- coding: utf-8 -*-
"""
给 Win-Box 的 runner.py 挂上「封面自适应」钩子（幂等、可还原）。

改两个序列化出口：_serialize（一次性）与 _serialize_raw（常驻池），
把所有 py 源返回的 vod_pic 改写成本地裁切服务地址。

用法：python patch_runner.py apply|status|restore
"""
import os
import shutil
import sys

RUNNER = r'D:/TVbox/Win-Box/data/winbox-cache/python/3.11.6/runner.py'
BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'runner.py.bak.20261116')
MARK = 'AUTO_CROP_BEGIN'

HELPER = '''

# === AUTO_CROP_BEGIN · 封面自适应（只处理封面，不碰播放正文）===
import urllib.parse as _ac_up
import socket as _ac_sock
import subprocess as _ac_sp

# ★ 用局域网 IP 而不是 localhost：Win-Box 前端会把所有图片包一层 127.0.0.1:9978/img?u=...
#   若 u 指向 localhost，该代理拒绝转发 → 封面空白。用 LAN IP 它才会当普通远程图去取。
CROP_BASE = 'http://192.168.10.11:9917'
_CROP_HOME = r'D:/workbuddy/WIN-TVBOX/winbox-auto-crop'


def _ensure_crop_service():
    """本地裁切服务没起来就拉起它（detached，不阻塞取数）。"""
    try:
        s = _ac_sock.socket()
        s.settimeout(0.35)
        try:
            s.connect(('127.0.0.1', 9917))
            return
        finally:
            s.close()
    except OSError:
        pass
    try:
        import sys as _sys
        py = _sys.executable
        if py.lower().endswith('pythonw.exe'):
            pass
        else:
            alt = os.path.join(os.path.dirname(py), 'pythonw.exe')
            if os.path.isfile(alt):
                py = alt
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_GROUP|NO_WINDOW
        _ac_sp.Popen([py, os.path.join(_CROP_HOME, 'crop_service.py')],
                     cwd=_CROP_HOME, creationflags=flags,
                     stdin=_ac_sp.DEVNULL, stdout=_ac_sp.DEVNULL, stderr=_ac_sp.DEVNULL)
    except Exception:
        pass


def _ac_walk(obj):
    if isinstance(obj, dict):
        for k in ('vod_pic', 'vod_pic_thumb'):
            v = obj.get(k)
            if isinstance(v, str) and v and not v.startswith(CROP_BASE):
                # v=2：竖图处理从「裁一条横带」改成「完整海报+模糊背景」，换 URL 破缓存
                obj[k] = CROP_BASE + '/crop?v=2&u=' + _ac_up.quote(v, safe='')
        for v in list(obj.values()):
            _ac_walk(v)
    elif isinstance(obj, list):
        for v in obj:
            _ac_walk(v)


def _auto_crop_pics(result):
    """把结果里的封面地址统一改写到裁切服务；失败就原样返回，绝不影响取数。"""
    try:
        if isinstance(result, str):
            parsed = json.loads(result)
            _ac_walk(parsed)
            return json.dumps(parsed, ensure_ascii=False)
        _ac_walk(result)
    except Exception:
        pass
    return result
# === AUTO_CROP_END ===
'''


def read():
    with open(RUNNER, 'r', encoding='utf-8') as f:
        return f.read()


def write(text):
    tmp = RUNNER + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    try:
        os.replace(tmp, RUNNER)
    except OSError:
        with open(RUNNER, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        os.remove(tmp)


def status():
    s = read()
    print('=' * 56)
    print('runner.py :', RUNNER)
    print('状态      :', '✅ 已挂封面自适应' if MARK in s else '原版（未挂）')
    print('备份      :', '有' if os.path.isfile(BACKUP) else '无')
    print('=' * 56)
    return 0


def apply():
    s = read()
    if MARK in s:
        print('已挂过，跳过（幂等）')
        return 0
    if not os.path.isfile(BACKUP):
        shutil.copy2(RUNNER, BACKUP)
        print('已备份 ->', BACKUP)

    anchor = 'def _serialize(result, real_out=_REAL_OUT):'
    if s.count(anchor) != 1:
        raise RuntimeError('_serialize 锚点不匹配')
    s = s.replace(anchor, HELPER + '\n\n' + anchor, 1)

    old1 = '    out = result\n    if isinstance(out, str):'
    if s.count(old1) < 1:
        raise RuntimeError('_serialize 内部锚点不匹配')
    s = s.replace(old1, '    out = _auto_crop_pics(result)\n    if isinstance(out, str):', 1)

    old2 = ('def _serialize_raw(result):\n'
            '    """把蜘蛛返回值转成可 JSON 传输的字符串：dict/list → json；str 原样。"""\n'
            '    if isinstance(result, str):\n'
            '        return result\n'
            '    return json.dumps(result, ensure_ascii=False)')
    if s.count(old2) != 1:
        raise RuntimeError('_serialize_raw 锚点不匹配')
    new2 = ('def _serialize_raw(result):\n'
            '    """把蜘蛛返回值转成可 JSON 传输的字符串：dict/list → json；str 原样。"""\n'
            '    result = _auto_crop_pics(result)\n'
            '    if isinstance(result, str):\n'
            '        return result\n'
            '    return json.dumps(result, ensure_ascii=False)')
    s = s.replace(old2, new2, 1)

    old3 = ('def main():\n'
            '    # ★ 蜘蛛 print() → stderr')
    if s.count(old3) != 1:
        raise RuntimeError('main 锚点不匹配')
    s = s.replace(old3,
                  'def main():\n'
                  '    _ensure_crop_service()\n'
                  '    # ★ 蜘蛛 print() → stderr', 1)

    write(s)
    print('补丁完成；语法自检...')
    import py_compile
    py_compile.compile(RUNNER, doraise=True)
    print('OK')
    return 0


def restore():
    if not os.path.isfile(BACKUP):
        print('没有备份')
        return 1
    shutil.copy2(BACKUP, RUNNER)
    print('已还原原版 runner.py')
    return 0


if __name__ == '__main__':
    c = sys.argv[1].lower() if len(sys.argv) > 1 else 'status'
    sys.exit({'status': status, 'apply': apply, 'restore': restore}.get(c, status)())
