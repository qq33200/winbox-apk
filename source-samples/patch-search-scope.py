#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 Win-Box 打两个小补丁（可重复执行，已打过会跳过）：

1) 主进程 searchAll：搜索词是 PikPak 分享链接时，只查 PikPak 源，不再遍历全部源；
2) 渲染层搜索结果横幅：显示"实际执行的源数"，而不是配置里的源总数。

用法：
  1. 完全退出 Win-Box（任务管理器里确认没有 Win-Box.exe）
  2. python patch-search-scope.py
  3. 重新打开 Win-Box

默认 app 目录 D:/TVbox/Win-Box，可用 --app 指定。
只改 resources/app.asar，不改 app.asar.unpacked。执行前会自动备份。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time

MAIN = 'dist/main.cjs'
BANNER_OLD = '"」共搜 ",e.length," 个源：命中 "'
BANNER_NEW = '"」共搜 ",(K.perSource?.length??e.length)," 个源：命中 "'
TERM_ANCHOR = 'const term = (wd || "").trim();'
REFRESH_ANCHOR = 'if (!opts?.refresh) {'
POOL_ANCHOR = 'const pool = this.searchableSites();'


def read_asar(path):
    raw = open(path, 'rb').read()
    if len(raw) < 16:
        raise SystemExit('不是有效的 asar：%s' % path)
    a, b, c, d = struct.unpack('<4I', raw[:16])
    if a != 4:
        raise SystemExit('asar 头异常（a=%d）' % a)
    tree = json.loads(raw[16:16 + d].decode('utf-8'))
    return raw, 8 + b, tree


def packed_entries(tree):
    out = []

    def walk(node, path=''):
        for key, value in node.get('files', {}).items():
            cur = path + '/' + key if path else key
            if 'files' in value:
                walk(value, cur)
            elif value.get('unpacked') or 'link' in value:
                continue
            elif 'offset' in value and int(value.get('size', 0)) > 0:
                out.append((cur, value))

    walk(tree)
    out.sort(key=lambda x: int(x[1]['offset']))
    return out


def patch_main(text):
    if 'pikpakLink' in text:
        return text, False
    i = text.find(TERM_ANCHOR)
    if i < 0:
        return text, False
    j = text.find(REFRESH_ANCHOR, i)
    k = text.find(POOL_ANCHOR, i)
    if j < 0 or k < 0:
        return text, False

    # 1) 记录是否是 PikPak 链接
    text = text[:i + len(TERM_ANCHOR)] + \
        '\n    const pikpakLink = /(?:mypikpak\\.com|pikpak\\.me)\\/s\\//i.test(term);' + \
        text[i + len(TERM_ANCHOR):]
    # 偏移会变，重新定位
    i = text.find(TERM_ANCHOR)
    j = text.find(REFRESH_ANCHOR, i)
    text = text[:j] + 'if (!opts?.refresh && !pikpakLink) {' + text[j + len(REFRESH_ANCHOR):]

    # 2) 源池收缩
    k = text.find(POOL_ANCHOR, i)
    block = '''let pool = this.searchableSites();
    if (pikpakLink) {
      const only = pool.filter((b) => b.key === "pikpak" || /pikpak/i.test(String(b.api || "")));
      if (only.length > 0) {
        this.logger.i(`search: pikpak link -> only pikpak source (skip ${pool.length - only.length} other sources)`);
        pool = only;
      }
    }'''
    text = text[:k] + block + text[k + len(POOL_ANCHOR):]
    return text, True


def patch_renderer(text):
    if BANNER_NEW in text:
        return text, False
    if BANNER_OLD not in text:
        return text, False
    return text.replace(BANNER_OLD, BANNER_NEW, 1), True


def build_asar(raw, base, tree, replacements):
    packed = packed_entries(tree)
    data_out = bytearray()
    for path, entry in packed:
        off = int(entry['offset'])
        size = int(entry['size'])
        data = raw[base + off:base + off + size]
        if path in replacements:
            data = replacements[path]
            entry['size'] = len(data)
            entry['integrity'] = {
                'algorithm': 'SHA256',
                'hash': hashlib.sha256(data).hexdigest(),
                'blockSize': 4194304,
                'blocks': [hashlib.sha256(data[i:i + 4194304]).hexdigest()
                           for i in range(0, len(data), 4194304)],
            }
        entry['offset'] = str(len(data_out))
        data_out += data
    body = json.dumps(tree, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    pad = (-len(body)) % 4
    c = 4 + len(body) + pad
    b = c + 4
    header = struct.pack('<4I', 4, b, c, len(body)) + body + b'\x00' * pad
    return header + bytes(data_out)


def winbox_running():
    if os.name != 'nt':
        return False
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Win-Box.exe'],
                           capture_output=True, text=True, errors='ignore', timeout=10)
        return 'Win-Box.exe' in (r.stdout or '')
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', default=r'D:/TVbox/Win-Box')
    ap.add_argument('--force', action='store_true', help='Win-Box 正在运行时也继续（不推荐）')
    args = ap.parse_args()

    asar = os.path.join(args.app, 'resources', 'app.asar')
    if not os.path.isfile(asar):
        raise SystemExit('找不到 %s' % asar)
    if winbox_running() and not args.force:
        raise SystemExit('Win-Box 正在运行，请先完全退出再执行（或加 --force）')

    raw, base, tree = read_asar(asar)
    replacements = {}
    changed = []

    # 主进程
    node = tree
    for part in MAIN.split('/'):
        node = node.get('files', {}).get(part, {})
    if not node:
        raise SystemExit('asar 内找不到 %s' % MAIN)
    off, size = int(node['offset']), int(node['size'])
    main_text = raw[base + off:base + off + size].decode('utf-8', 'replace')
    new_main, ok = patch_main(main_text)
    if ok:
        replacements[MAIN] = new_main.encode('utf-8')
        changed.append(MAIN)
        print('✓ 主进程：已加入 PikPak 链接单源判断')
    else:
        print('· 主进程：已打过或版本不匹配，跳过')

    # 渲染层（文件名带 hash，自动找）
    for path, entry in packed_entries(tree):
        if not re.match(r'dist/renderer/assets/index-.*\.js$', path):
            continue
        off, size = int(entry['offset']), int(entry['size'])
        text = raw[base + off:base + off + size].decode('utf-8', 'replace')
        new_text, ok = patch_renderer(text)
        if ok:
            replacements[path] = new_text.encode('utf-8')
            changed.append(path)
            print('✓ 渲染层：%s 横幅改为实际源数' % path)
        else:
            print('· 渲染层：%s 已打过或版本不匹配，跳过' % path)

    if not replacements:
        print('没有需要修改的内容。')
        return 0

    out = build_asar(raw, base, tree, replacements)
    bak = asar + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(asar, bak)
    tmp = asar + '.new'
    open(tmp, 'wb').write(out)
    # 简单自检：重新解析新文件
    try:
        read_asar(tmp)
    except Exception as exc:
        os.remove(tmp)
        raise SystemExit('新 asar 自检失败：%s' % exc)
    os.replace(tmp, asar)
    print('完成，已修改 %d 个文件；原文件备份：%s' % (len(changed), bak))
    print('现在可以重新打开 Win-Box。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
