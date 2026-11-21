#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 Win-Box 打「支持本地 .js 源」补丁（可重复执行，已打过会跳过）：

1) 主进程「导入本地源」的文件选择框同时接受 .py / .js；
2) 渲染层按钮文案改为「导入本地 .py / .js 文件」；
3) JS 沙箱支持 file:// 本地脚本（原来本地 .js 会因 undici 不支持 file: 而加载失败）。

用法：
  1. 完全退出 Win-Box
  2. python patch-winbox-js.py
  3. 重新打开 Win-Box

只改 resources/app.asar，不改 app.asar.unpacked，执行前自动备份。
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
WORKER = 'dist/js-worker.cjs'
MAIN_EXT = 'extensions: ["py"]'
MAIN_EXT_NEW = 'extensions: ["py", "js"]'
MAIN_NAME = '"Python \\u8718\\u86DB\\u811A\\u672C"'
MAIN_NAME_NEW = '"Python / JS \\u8718\\u86DB\\u811A\\u672C"'
R_BTN = 'children:"导入本地 .py 文件"'
R_BTN_NEW = 'children:"导入本地 .py / .js 文件"'
R_S1 = '`已导入 ${me} 个本地 .py 源`'
R_S1_NEW = '`已导入 ${me} 个本地源`'
R_S2 = '`已导入本地 .py 源：${M.key}`'
R_S2_NEW = '`已导入本地源：${M.key}`'
WORKER_OLD = '''async doLoad() {
    const res = await this.host.http.request({ url: this.api, method: "get", timeoutMs: 2e4 });
    let content = Array.isArray(res.content) ? Buffer.from(res.content).toString("utf-8") : String(res.content ?? "");
    content = content.replace(/^\\uFEFF/, "").trim();'''
WORKER_NEW = '''async doLoad() {
    let content;
    if (typeof this.api === "string" && this.api.startsWith("file://")) {
      try {
        let filePath = decodeURIComponent(new URL(this.api).pathname);
        if (process.platform === "win32" && /^\\/[a-zA-Z]:[\\\\/]/.test(filePath)) filePath = filePath.slice(1);
        content = require("node:fs").readFileSync(filePath, "utf-8");
      } catch (e) {
        throw new UnsupportedSourceError("FETCH_FAILED", `local js read failed: ${this.api} (${e instanceof Error ? e.message : String(e)})`);
      }
    } else {
      const res = await this.host.http.request({ url: this.api, method: "get", timeoutMs: 2e4 });
      content = Array.isArray(res.content) ? Buffer.from(res.content).toString("utf-8") : String(res.content ?? "");
    }
    content = content.replace(/^\\uFEFF/, "").trim();'''


def read_asar(path):
    raw = open(path, 'rb').read()
    a, b, c, d = struct.unpack('<4I', raw[:16])
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
    return struct.pack('<4I', 4, b, c, len(body)) + body + b'\x00' * pad + bytes(data_out)


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
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    asar = os.path.join(args.app, 'resources', 'app.asar')
    if not os.path.isfile(asar):
        raise SystemExit('找不到 %s' % asar)
    if winbox_running() and not args.force:
        raise SystemExit('Win-Box 正在运行，请先完全退出再执行（或加 --force）')

    raw, base, tree = read_asar(asar)
    replacements = {}
    changed = []

    def entry_of(path):
        n = tree
        for part in path.split('/'):
            n = n.get('files', {}).get(part, {})
        if not n:
            return None, None
        data = raw[base + int(n['offset']):base + int(n['offset']) + int(n['size'])]
        return n, data.decode('utf-8', 'replace')

    # 主进程
    e, text = entry_of(MAIN)
    if e and MAIN_EXT in text:
        text = text.replace(MAIN_EXT, MAIN_EXT_NEW, 1)
        text = text.replace(MAIN_NAME, MAIN_NAME_NEW, 1)
        replacements[MAIN] = text.encode('utf-8')
        changed.append(MAIN)
        print('✓ 主进程：本地源导入接受 .js')
    else:
        print('· 主进程：已打过或版本不匹配，跳过')

    # 渲染层
    for path, _ in packed_entries(tree):
        if not re.match(r'dist/renderer/assets/index-.*\.js$', path):
            continue
        e, text = entry_of(path)
        if not e:
            continue
        if R_BTN in text and R_S1 in text and R_S2 in text:
            text = text.replace(R_BTN, R_BTN_NEW, 1).replace(R_S1, R_S1_NEW, 1).replace(R_S2, R_S2_NEW, 1)
            replacements[path] = text.encode('utf-8')
            changed.append(path)
            print('✓ 渲染层：%s 导入按钮/文案更新' % path)
        else:
            print('· 渲染层：%s 已打过或版本不匹配，跳过' % path)

    # JS 沙箱
    e, text = entry_of(WORKER)
    if e and WORKER_OLD in text:
        text = text.replace(WORKER_OLD, WORKER_NEW, 1)
        replacements[WORKER] = text.encode('utf-8')
        changed.append(WORKER)
        print('✓ JS 沙箱：支持 file:// 本地脚本')
    else:
        print('· JS 沙箱：已打过或版本不匹配，跳过')

    if not replacements:
        print('没有需要修改的内容。')
        return 0

    out = build_asar(raw, base, tree, replacements)
    bak = asar + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(asar, bak)
    tmp = asar + '.new'
    open(tmp, 'wb').write(out)
    try:
        read_asar(tmp)
    except Exception as exc:
        os.remove(tmp)
        raise SystemExit('新 asar 自检失败：%s' % exc)
    os.replace(tmp, asar)
    print('完成，修改 %d 个文件；备份：%s' % (len(changed), bak))
    return 0


if __name__ == '__main__':
    sys.exit(main())
