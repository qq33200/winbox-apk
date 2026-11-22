# 桌面接入 libtorrent：接口契约与移植指引（WorkBuddy → Muse，2026-11-22 13:10）

> 同步记录，不含执行指令。采不采纳、怎么采纳，由用户与你判断。

## 0. 结论先行

桌面端已经在跑**双引擎**：aria2 继续做下载管理器，libtorrent 只接管"正在播的那个种子"。
分线依据是**归属**，不是共用——同一个种子两个引擎不能同时管，原因见第 2 节。

实测收益（同一台机、同一个 4.14 GB 种子）：

| 指标 | libtorrent 2.1.1 | aria2 1.37 |
| --- | --- | --- |
| 校验已有 4 GB | **4.0 s** | 11.7 s |
| 冷启动到首块数据 | 2.4 s（仅 2 个 peer） | — |
| 攒够 512 KB | 3.6 s（109 KB/s） | 45 s 超时（无源时） |
| 拖到 50% 取 64 MB | **7.6 s**（速度 109 KB/s → 6.3 MB/s） | 无此能力 |
| 本地完整文件取 256 KB | 206 / **6 ms** | 需先 11.7 s 校验 |

关键在最后两行：`set_piece_deadline` 是 aria2 完全没有的能力，也是"拖动秒播"的唯一实现路径。

## 1. 文件与指纹

```
magnet-reference-2026-11-22/libtorrent-sidecar.py
  11,423 B   LF（0 处 CRLF）
  md5    caad66e6971792edc1d865fedac7c3b9

magnet-reference-2026-11-22/magnet.py
  34,082 B   CRLF（828 处）
  md5    aa6e1b5f57102ece34f145ff62a56b9a
```

两份都与桌面运行时里的文件**逐字节相同**（运行时路径 `data/winbox-cache/libbt/` 与
`data/imports/py/452f6c6a-…/magnet.py`）。本目录 `.gitattributes` 对 `*.py` 设了 `-text`，
字节不会被 git 归一化，md5 可直接核对。

`magnet.py` 相对上次同步（`4dc3419b`，27,941 B）新增：多视频分集、文件清单展示、
`/bt/resolve` 解析调用、`_is_placeholder_name` 片名升级。

## 2. 为什么不能两个引擎共用一个种子

| | 状态文件 | 内容 |
| --- | --- | --- |
| aria2 | `<name>.aria2` | 已完成 piece 位图 |
| libtorrent | `.fastresume` | piece 位图 + peer 缓存 |

两者**格式不互通**，互不知道对方下到哪。同时写同一批文件会导致：同一段数据下两遍、
互相覆盖正在写的块、最坏写出坏帧。切换引擎还要 `recheck`（重新哈希校验）才能接管。

所以桌面采用的形态是**按种子归属分流**：

```
/bt/<hash>/<idx>
  ├─ 该 hash 在 libtorrent 会话里 → 用 libtorrent 的取流地址
  └─ 否则                        → 原 aria2 路径
```

进入 libbt 的种子，aria2 不再碰；反之亦然。

## 3. 桌面实现

### 3.1 启动链

```
用户点播放
  ↓
open(magnet)
  ├─ ① 完成标记 <bt>/<infohash>.done 存在且文件大小一致 → 直接读盘（不拉起任何引擎）
  ├─ ② <bt>/../libbt/enabled.flag 存在 → 拉起 sidecar → /open → /select → 取流地址
  └─ ③ 以上任一失败 → 回退 aria2（原有逻辑，未改）
```

### 3.2 运行时依赖（桌面踩过的坑，手机天然没有）

- 壳自带嵌入版 Python：`data/winbox-cache/python/3.11.6/python.exe`
- **它的 `python311._pth` 里没有 `Lib/site-packages`**，默认 `sys.path` 只有 `python311.zip` 和 `.`
  → sidecar 脚本顶部必须显式插入：

```python
_SP = os.path.join(os.path.dirname(sys.executable), 'Lib', 'site-packages')
if os.path.isdir(_SP) and _SP not in sys.path:
    sys.path.insert(0, _SP)
```

- 装 libtorrent 到该运行时：拿 cp311 wheel 解压到 `Lib/site-packages` 即可，
  不需要 pip（嵌入版没带 pip）：

```
pip download --only-binary :all: --no-deps \
    --implementation cp --python-version 311 --abi cp311 --platform win_amd64 \
    libtorrent -d wheels
# 然后把 .whl 当 zip 解压进 site-packages
```

### 3.3 sidecar 接口契约

只监听 `127.0.0.1`，默认端口 **9981**（`aria2` 是随机端口，这个我用固定端口便于排查）。

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/health` | — | `{"ok":true}` |
| POST | `/open` | `{"torrent":"<绝对路径>","magnet":"<磁力>","save_path":"<目录>"}` | `{"ok":true,"ih":"<40位hash>","files":[{"index":1,"path":"a/b.mp4","length":123}]}` |
| POST | `/select` | `{"ih":"…","index":1}` | `{"ok":true,"index":1,"files":71}` |
| POST | `/seek` | `{"ih":"…","index":1,"offset":2070167552}` | `{"ok":true,"pieces":32}` |
| GET | `/status?ih=…` | — | `{"ok":true,"state":"seeding","progress":100.0,"rate":0,"peers":0,"seeds":0,"done":…,"wanted":…,"pieces":1035,"piece_length":4194304}` |
| GET | `/stream?ih=…&index=1` | 标准 `Range` 头 | `206` + 原始字节；首片未就绪 → **503** |
| POST | `/close` | `{"ih":"…"}` | `{"ok":true}` |

`/open` 的 `torrent` 优先于 `magnet`：调用方若已有 `<hash>.torrent` 就传路径，省掉元数据等待。

`/files` 的 `index` 是 **1-based**，与 aria2 的 `index` 语义一致（壳里同一个 `pickVideoFile` 在选择）。

### 3.4 壳侧路由与回退

- `open()` 先查完成标记，再试 libbt，最后回落 aria2
- libbt 成功后，会话里带 `streamUrl`，`handOff()` 直接把它当播放地址（不再经过 `/bt`）
- 会话 `engine: "libbt"`，`drop()` 时走 `/close` 而不是 `aria2.remove`
- 回退开关：删 `<bt>/../libbt/enabled.flag` 即回纯 aria2，**不需要换包**

## 4. 四个必须照抄的设计点

这四条都是桌面踩出来的，手机照抄能省一轮返工。

### 4.1 多文件种子必须 `select-file`

71 集的合集如果不下 `prioritize_files`，libtorrent 会整包下载。桌面流程是
`/open` → 拿文件清单 → `pickVideoFile` 选出目标 → `/select` 只保留该文件优先级 4、其余 0。

顺序不能颠倒：**先拿到清单才能选**，所以 `/open` 与 `/select` 是两个调用，不是一个。

### 4.2 首片就绪再发响应头

最初的写法是先 `send_response(206)` 再循环等 piece，结果是：播放器拿到 206 +
`Content-Length`，然后卡到 60 秒超时。

现在改成：

```python
# 发头之前先确认首片
first_piece = int((base + start) // pl)
last_piece  = int((base + min(start + CHUNK - 1, end)) // pl)
for k in range(first_piece, last_piece + 1):
    if not wait_piece(h, k, timeout=40):
        return 503                      # 拿不到首片就别发 206
```

没有做种的种子会稳定返回 503，播放器立刻报错而不是无限转圈。

### 4.3 会话探活

力 case：sidecar 进程挂了，壳里缓存的会话还指着 9981 死端口 → 播放器连接被拒。

```js
if (existed.engine === "libbt") {
  try { const alive = await libbtCall(`/status?ih=${ih}`, 1500);
        if (alive && alive.ok) return handOff(existed); } catch {}
  sessions.delete(ih);   // 失效就删，交给 ② 重建
}
```

### 4.4 完成标记跳过校验

aria2 每次播放都会重算整片哈希（4 GB = 11.7 秒）。桌面做法：
aria2 报 `status=complete` 时写 `<bt>/<infohash>.done`（JSON，含 `absPath`/`fileLength`/
`fileOffset`/`pieceLength`/`files`），下次 `open()` 命中且文件大小一致就直接读盘。

libtorrent 侧不需要这个（它校验只要 4 秒），但**手机如果继续用 aria2 做下载，值得照抄**。

## 5. 实测数据（可复现）

```
# 冷启动（空目录、2 个 peer）
首块数据        2.4 s
攒够 512 KB     3.6 s   速度 109 KB/s

# 拖到 50%（给 16 个 piece 设 deadline）
64 MB 到手      7.6 s   速度 109 KB/s → 6.3 MB/s

# 接管已有 4.0 GB 数据
macOS/Windows 校验   4.0 s   有效数据 100%
aria2 同文件校验     11.7 s

# 本地完整文件取流（libbt）
Range 0-262143  206 / 262144 B / 6 ms
```

## 6. 手机端移植映射（libtorrent4j）

| sidecar 里的调用 | libtorrent4j 等价 |
| --- | --- |
| `lt.session(settings)` | `SessionManager` + `SettingsPack` |
| `lt.add_torrent_params()` / `parse_magnet_uri` | `AddTorrentParams.parseMagnetUri(uri)` |
| `h.set_sequential_download(True)` | `torrentHandle.setSequentialDownload(true)` |
| `h.set_piece_deadline(k, ms)` | `torrentHandle.setPieceDeadline(k, ms)` |
| `h.have_piece(k)` | `torrentHandle.havePiece(k)` |
| `h.prioritize_files(prios)` | `torrentHandle.prioritizeFiles(int[])` |
| `ti.files().file_offset(i)` | `torrentInfo.files().fileOffset(i)` |
| `h.status().total_done` | `torrentStatus.totalDone()` |
| `.fastresume` | `torrentHandle.saveResumeData()` / `AddTorrentParams.resumeData` |

HTTP 那层建议直接用 Kotlin 的 `NanoHTTPD` 或 `ServerSocket` 手写，逻辑照 4.2 的
"首片就绪再发头"来。**不要**在 Android 上复用 Python sidecar（嵌入版 Python + libtorrent
在手机上没必要，libtorrent4j 更贴合）。

## 7. 桌面还剩下的问题

1. **真实拖动未验证**：`/seek` 接口接好了，但还没在播放器里拖过进度条验证端到端。
2. **首次播放仍需元数据**：没有 `<hash>.torrent` 的新磁链，靠 DHT/tracker，冷门种子仍可能 40 秒拿不到。
3. **无做种的种子**：现在稳定 503（比之前无限挂好），但播不了就是播不了。

## 8. 待确认（编号回答即可）

1. 手机端你是打算用 `libtorrent4j` 另写一套，还是先只做"完成标记 + aria2 免校验"这个轻改动？
2. 手机端的本地中继端口是多少？`magnet.py` 里 `/bt/resolve` 写死了 `127.0.0.1:9978`，
   桌面是这个端口，手机若不是需要改成配置项。
3. APK 内置的 `magnet.py` 这次要一起换（34,082 B / `aa6e1b5f`），是你替换还是要用户手动覆盖？
4. `libtorrent4j` 的 AAR 你有现成版本吗？我这边只验证了桌面 Python 绑定，
   手机端的 API 名称是我按文档对照的，没有真机跑过。
