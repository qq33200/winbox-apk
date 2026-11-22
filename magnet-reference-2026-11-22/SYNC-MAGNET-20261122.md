# 磁力源同步（2026-11-22 10:12，WorkBuddy → Muse）

> 同步记录，不含执行指令。采不采纳、怎么采纳，由用户与你判断。

## 一句话

`magnet.py` 从 17,408 B 扩到 27,941 B，加了**最近解析 / 管理删除 / 片名回退 / 种子本地缓存**；
顺带把「拖动不能秒播」定位清楚了——**不是参数问题，是 aria2 没有按播放点重排分片的接口**，
要真秒播只能换 libtorrent。

## 一、文件

```
magnet-reference-2026-11-22/magnet.py
  27,941 B   CRLF（677 处）   ← 与运行时那份逐字节相同
  md5    4dc3419b96d40689c8d7c9f158602ed6
  sha256 2fabd362fd9b210c117ff85006faaae708d7a74356ba49947f1ef920316264fe
```

本文就是 md5 的**基准是 CRLF 版本**（桌面上跑的就是这个字节序列）。
`.gitattributes` 里对这个目录加了 `-text`，避免 `core.autocrlf=true` 把它归一成 LF。

对照：

| 版本 | 字节 | md5 | 位置 |
| --- | --- | --- | --- |
| 旧（APK 内置） | 17,408 | `19cf1586727d90deb387188f0e9a4137` | `assets/flutter_assets/assets/python_sources/magnet.py` |
| 新（本目录） | 27,941 | `4dc3419b96d40689c8d7c9f158602ed6` | 桌面运行时 `data/imports/py/452f6c6a-…/magnet.py` |

桌面旧版备份在同目录 `magnet.py.bak-before-record`。

## 二、四项改动

### 1. 最近解析 + 管理（契约与 `pikpak.py` 同构）

| 位置 | vod_id | 行为 |
| --- | --- | --- |
| 首页分类 `recent` | — | 分类名「最近解析」 |
| 分类首行 | `recent.manage` | 进管理页（`vod_tag=folder`） |
| 管理页首行 | `recent.del.all` | 全选删除 |
| 管理页逐条 | `recent.del.<i>` | 单条删除 |
| 记录卡片 | `n.<b64(magnet)>` | 点开进详情，与 `m.` 完全等价 |

- 状态文件 `magnet-state.json`，与 py 同目录；目录不可写时退到临时目录
- 同一磁链只留一条（按 `u` 去重），上限 60 条
- `ext` 里 `"remember": "0"` 可关闭记录
- **`pikpak.py` 本身没有改名功能，只有删除**，所以「编辑」按删除对齐；TVBox 的 py 侧也没有输入框可用

### 2. 片名三级回退

裸磁链没有 `dn=`，原来只能显示 `磁力 ED27491B`。现在：

1. magnet 里的 `dn=`
2. aria2 落地的 `<infohash>.torrent` → 解析 `info.name`
3. 兜底 `磁力 <hash8>`

缓存目录按 py 自身位置上溯推断，候选：`bt/`、`winbox-cache/bt`、`files/bt`、
`app_flutter/bt`、`cache/bt`（桌面实测命中 `<data>/winbox-cache/bt`）。
记录里的占位名会在下次进列表时自动升级并落盘。

**限制（已实测）**：公开的 infohash→种子源全部不可用——
`itorrents.org` 301 跳到 `limetorrents.fun` 后 403；`torcache.net`、`btdig.com`、
`bt.box.n0808.com` 直连与走代理均连不通。
所以**首次播放前仍可能是 hash 名**，播过一次之后才有真名。

### 3. 种子本地缓存

贴 `.torrent`（本地路径 / `file://` / `http(s)`）时，把原始种子存一份到 bt 缓存目录的
`<infohash>.torrent`，下次解析同一磁链可直接取名，也省一次下载。

### 4. 参数没动

这次只改 py，aria2 参数保持上一轮那套（`head=32M,tail=8M` / `bt-max-peers=128` /
`listen-port=6881-6889` / `disk-cache=64M`）。

## 三、「拖动不能秒播」的诊断（重要）

### 3.1 先排除服务端

直接对运行中的 `/bt` 端点打 Range（文件已下完的状态）：

| 请求 | 响应 | 耗时 |
| --- | --- | --- |
| `Range: bytes=0-1023` | 206，1024 B | 4.9 ms |
| `Range: bytes=130000000-130001023` | 206，1024 B | 4.9 ms |
| `Range: bytes=240000000-240001023` | 206，1024 B | 5.1 ms |
| `HEAD /bt/<hash>/1` | 200，`Accept-Ranges: bytes`，`Content-Length: 261339698` | — |

日志里也**没有**一条 `首段未就绪` / `片段未就绪` 告警。所以服务端 Range 与门控都正常。

### 3.2 卡点在引擎

- aria2 的分片优先级只有 `--bt-prioritize-piece=<head>,<tail>`，**只有首段和尾段两档**
- 没有「按播放位置重排分片」的接口，`aria2.changeOption` 也改不了这个
- 拖动到未下载区时，`waitCovered(pos, pos+512K)` 只能等那片到货；45 秒超时后返回 503
- 加大 `head` 只影响开头，治不了拖动

对比对象为什么能：迅雷引擎有 `SynPlayPos(playPos)`，播放器回报位置 → 引擎按位置重排优先级
（见 `REFERENCE-heiye-download.md` 第 2.4 节）。

### 3.3 两条路

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| 维持 aria2 | 只做「起播优化」：元数据直取 + 参数 + 更大 tracker 池 | 拖动仍会等，天花板到此为止 |
| 换 libtorrent | `set_piece_deadline()` + `sequential_download(true)`，播放点 ±N MB 给 deadline | 要重写 sidecar、RPC、`/bt` 门控的数据源（改读 `pieceAvailability`） |

**Android 侧换引擎的代价比桌面大**：现在 `aria2_sidecar.dart` + `bt_stream.dart` + `/bt` 路由
都是围着 aria2 的 RPC 写的，换 libtorrent4j 等于把这层重做一遍。

## 四、手机端要注意

1. **APK 里那份还是旧的**（17,408 B），要替换 `assets/flutter_assets/assets/python_sources/magnet.py`
2. `_bt_cache_dirs()` 的路径推断**只在桌面验证过**；Android 上 py 被加载到哪个目录、
   bt 缓存是不是 `files/bt`，我没法在这里验证。真机上如果片名不生效，把 py 所在目录打出来
   对一下候选列表即可
3. 既有问题未变：Android 端 HTTPS tracker 拿不到 CA（随包 README 自述），
   元数据只能靠 DHT 与 UDP/HTTP tracker

## 五、待确认（编号回答即可）

1. APK 内置的 `magnet.py` 是你这边替换，还是要用户自己覆盖 `Download/pikpak.py` 那种方式？
2. 手机端 bt 缓存的真实路径是什么？（我猜 `files/bt` 或 `app_flutter/bt`，需要你确认）
3. 「拖动秒播」这个议题，你那边是打算等 libtorrent 迁移，还是先维持 aria2？
4. `recent.manage` 这套契约你那边 UI 是否需要额外适配？（pikpak 源已经在用同一套，理论上不用改）
