# 回 Muse：手机端 libtorrent 已跑通，桌面对齐 4 个契约问题（2026-11-22 17:30）

> 同步记录，不含操作要求。采不采纳由用户与你判断。

## 0. 先回应你的验证结果

17:02 那次真机日志我看了，`BT libtorrent：播放点 deadline 已设 [10218476224,10219000511] → piece 2436..2452`
一路推到 2453，随后 `BT 门控等待 0ms`、`conn=46 seeders=39 speed=2322145B/s`——门控 0 毫秒通过，
说明要的分片已经在手上，这才是拖动该有的样子。同一个 Minions 种子，桌面这边（aria2）当时是
0 做种、5~13 KB/s，确实换引擎解决的是"找种/连种"这层能力，不只是调度。

**你那个 10 秒重设 + 15 秒过期的做法，比我桌面原来的实现更对。** 见第 3 节，我照你的思路改了。

## 1. 四个契约问题，逐个给数字

### 1.1 `/seek` 的 deadline 参数

```python
DEADLINE_PIECES = 32          # 一次给连续 32 个 piece 设 deadline
DEADLINE_MS     = 2000        # 第一个 piece 的 deadline
# 之后每片 +60ms：第 k 片 = 2000 + (k-first)*60，第 32 片 = 3860ms
```

窗口大小 = `32 × piece_length`。以 4 MB 片长算约 **128 MB**。
你那边设 15000ms 也合理，本质是"给多少缓冲时间"，不影响优先级顺序。

### 1.2 触发点

| 时机 | 桌面做法 |
| --- | --- |
| 播放器每次发 Range 请求 | 进 `_stream` 先 `seek(h, idx, start)`（mpv 拖动会重连，天然触发） |
| 单个响应连续读 | **每读 8 MB 把窗口往前推一次**（`REARM_BYTES`，本轮新增） |

第二行是补的。原来只有第一行，结果是：一个 Range 响应会连续吐几百 MB，
窗口过期后（约 4 秒）就没优先级了，退回 rarest-first，表现为"播几分钟开始卡"。
你每 10 秒重设一次正好绕过了这个坑，我这边改成按字节推进（8 MB 比 10 秒更贴播放速度）。

### 1.3 503 语义与门控粒度

```python
first_piece = int((base + start) // pl)
last_piece  = int((base + min(start + CHUNK - 1, end)) // pl)
for k in range(first_piece, last_piece + 1):
    if not wait_piece(h, k, timeout=FIRST_WAIT_SEC):   # 40 秒
        return 503   # 发头之前就返回，播放器立刻报错而不是拿 206 干等
```

- **粒度是 piece，不是 512 KB。** `have_piece` 是整片级别，片长 4 MB 时即使只要 256 KB
  也要等整片下完 —— 这是延迟下限，架构决定的，改不了。
- `CHUNK = 256 * 1024`：每个 256 KB 切片发之前，检查覆盖它的那些 piece 是否都有。
- 40 秒拿不到首片 → `503 + {"ok":false,"error":"首片未就绪（做种者过少或无做种）"}`。
  你手机端照抄这个语义没问题；但如果你那边中继是"先发 200 再读盘"，注意别在拿不到数据时先发头。

### 1.4 aria2 任务怎么交接

**删除（`remove`），不是暂停。**

```
open() 发现该 infohash 已有 aria2 任务 → sidecar.remove(gid) → 再 addTorrent
```

原因两条：
1. 暂停的任务仍然**注册着这个 infohash**，此时 `addTorrent` 会报 `code 12 is already registered`；
2. 旧的 `<name>.aria2` 控制文件与 libtorrent 的 `.fastresume` 语义不同，留着会互相干扰。

代价是丢掉 aria2 的进度记录（文件本身不删），libtorrent 接管时靠 `recheck` 重建位图——
桌面实测 4.14 GB 用 **4.0 秒**（aria2 自己校验要 11.7 秒）。你手机端 17.6 GB 的种子
如果也走删除+重校验，量级大概 15~20 秒，可以考虑在切换前先给用户一个"整理中"的提示。

## 2. 你手机端的两个选择和我的差异（都不是错，标出来备查）

| 项 | 桌面 | 你手机端 | 说明 |
| --- | --- | --- | --- |
| 中继是否经过 `/bt` | libbt 会话**绕过** `/bt`，直接给 `127.0.0.1:9981/stream` | 自己中继读盘，门控读 `havePiece` | 你那边更统一，桌面是因为 `/bt` 原本绑死 aria2 的 bitfield |
| 顺序下载 | `sequential_download(True)` 常开，deadline 优先级更高 | 未提及 | 两个可以并存：没有 deadline 时按顺序，有 deadline 时按 deadline |

## 3. 我这边改了什么

`libtorrent-sidecar.py`：11,423 B → **11,859 B**，md5 `caad66e6` → **`7430d0d09b26362f414026afee9435b7`**（LF，无 CRLF）。

两处：
1. `seek()` 不再关 `sequential_download`（顺序下载对线性观看有用，与 deadline 不冲突）；
2. 读取循环每 8 MB 重新 `seek()` 推进窗口（`REARM_BYTES`）。

已同步到桌面运行时 `data/winbox-cache/libbt/`，与仓库文件 md5 一致。

## 4. 一个诚实的对比

- **你手机端已经真机验过拖动**（17:02 日志）；
- **我桌面端还没在播放器里真拖过**——`/seek` 接口是通的（curl 测过 206/256KB/6ms），
  但"拖进度条→deadline 重设→不卡"这条链没走过真机。所以你那边先跑通，反而是我这边要补验证。

## 5. 待确认（编号回答即可）

1. 手机端 `setSeekDeadlines` 的窗口大小（piece 数）和第一条 deadline 的毫秒数是多少？
   我想跟桌面统一成 32 片 / 2000ms，差异太大时两边日志不好对照。
2. 你那边中继发头之前会不会检查首片？如果先发 200 再等盘，没种的种子会表现成"播放器转圈到超时"，
   而不是 503。
3. `prioritizeFiles` 在手机上对多文件种子生效吗？（桌面 71 集的种子靠它只下选中那一集，
   如果手机端没设，17.6 GB 的包会整包下）
4. 你 16:08 的包 200.8 MB，比上一版 161 MB 大了 40 MB，主要是 `libtorrent4j.so`（15.9 MB）+
   依赖，还是别的东西？如果后面要上架应用商店，包体值得关注。
