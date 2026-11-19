# -*- coding: utf-8 -*-
"""
Win-Box 横封面补丁
================================================================
Win-Box 前端把卡片封面写死成竖版海报 `aspect-ratio: 2/3`（4 处），
没有现成开关。本脚本改 asar 里的 CSS，把卡片封面改成 16:9 横版。

命令：
    winbox_landscape.py status    查看当前封面比例状态
    winbox_landscape.py apply     打补丁（自动备份 + 关闭 Win-Box）
    winbox_landscape.py restore   还原（自动关闭 Win-Box）

★ 只动 dist/renderer/assets 下的 CSS，不碰任何爬虫/播放逻辑。
★ App 更新会覆盖 asar，更新后重跑 apply 即可。
"""
import hashlib
import json
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winbox_patch as wp  # noqa: E402  复用 asar 读写/重建/校验

ASAR = r'D:/TVbox/Win-Box/resources/app.asar'
BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'app.asar.bak.20261116-landscape')
CSS_NAME = 'index-eAZHR_S9.css'

# 替换顺序：先比例，再修正 .rec-row 的固定高度（132*9/16 ≈ 74）
REPL = [
    (b'aspect-ratio:2/3', b'aspect-ratio:16/9'),
    (b'aspect-ratio:2 / 3', b'aspect-ratio:16 / 9'),
    (b'width:132px;height:198px;aspect-ratio:16 / 9',
     b'width:132px;height:74px;aspect-ratio:16 / 9'),
    # 卡片加宽：一行 8 个 → 6 个（1274px / 200px ≈ 6）
    (b'--card-w: 132px', b'--card-w: 200px'),
    (b'minmax(138px,1fr)', b'minmax(200px,1fr)'),
]


def locate_css(header):
    hits = [(p, m) for p, m in wp.walk(header)
            if p.startswith('dist/renderer/assets/') and p.endswith('.css')]
    if not hits:
        raise RuntimeError('asar 里找不到 renderer CSS')
    for p, m in hits:
        if p.endswith('/' + CSS_NAME):
            return p, m
    return hits[0]


def css_state():
    header, data, ds = wp.read_asar(ASAR)
    path, meta = locate_css(header)
    css = wp.read_member(data, ds, meta)
    return path, css.count(b'aspect-ratio:2/3') + css.count(b'aspect-ratio:2 / 3'), \
        css.count(b'aspect-ratio:16/9') + css.count(b'aspect-ratio:16 / 9'), css


def cmd_status():
    path, old, new, css = css_state()
    print('=' * 58)
    print('asar     :', ASAR)
    print('CSS      :', path, '(%d 字节)' % len(css))
    print('竖版 2:3 : %d 处' % old)
    print('横版 16:9: %d 处' % new)
    print('状态     :', '✅ 已打横封面补丁' if old == 0 and new >= 4 else '原版（竖封面）')
    print('md5      :', hashlib.md5(open(ASAR, 'rb').read()).hexdigest())
    print('备份     :', '有' if os.path.isfile(BACKUP) else '无')
    print('=' * 58)
    return 0


def build_landscape(header, data, ds, target, new_css):
    """按 asar 格式重建：重排 offset + 重算 integrity（沿用 winbox_patch 的做法）"""
    entries = [(p, m) for p, m in wp.walk(header) if not m.get('unpacked')]
    entries.sort(key=lambda x: int(x[1]['offset']))
    body = bytearray()
    for p, m in entries:
        chunk = new_css if p == target else wp.read_member(data, ds, m)
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


def cmd_apply():
    header, data, ds = wp.read_asar(ASAR)
    target, meta = locate_css(header)
    css = wp.read_member(data, ds, meta)

    if css.count(b'aspect-ratio:2/3') + css.count(b'aspect-ratio:2 / 3') == 0:
        print('已经是横封面版，跳过（幂等）')
        return 0

    if not os.path.isfile(BACKUP):
        shutil.copy2(ASAR, BACKUP)
        print('已备份 ->', BACKUP)

    out = css
    for old, new in REPL:
        n = out.count(old)
        out = out.replace(old, new)
        print('  %d 处: %s -> %s' % (n, old.decode(), new.decode()))
    if out.count(b'aspect-ratio:16/9') + out.count(b'aspect-ratio:16 / 9') < 4:
        raise RuntimeError('替换数量不足，中止（CSS 版本可能变了）')

    print('关闭 Win-Box ...')
    wp.kill_app()

    new_asar = build_landscape(header, data, ds, target, out)
    wp.verify(new_asar)
    print('asar 结构校验通过 (%d 字节)' % len(new_asar))
    print('写入中 ...')
    wp.write_over(ASAR, new_asar)
    print('完成。md5:', hashlib.md5(open(ASAR, 'rb').read()).hexdigest())
    print('→ 请手动启动 Win-Box 看效果')
    return 0


def cmd_restore():
    if not os.path.isfile(BACKUP):
        print('没有备份，无法还原')
        return 1
    print('关闭 Win-Box ...')
    wp.kill_app()
    shutil.copy2(BACKUP, ASAR)
    print('已还原竖封面版:', ASAR)
    print('→ 请手动启动 Win-Box')
    return 0


def main():
    c = sys.argv[1].lower() if len(sys.argv) > 1 else 'status'
    if c == 'status':
        return cmd_status()
    if c == 'apply':
        return cmd_apply()
    if c == 'restore':
        return cmd_restore()
    print(__doc__)
    return 0


if __name__ == '__main__':
    sys.exit(main())
