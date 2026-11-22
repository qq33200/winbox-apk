# 「黑夜下载」磁力方案逆向参考

分析对象：`C:\Users\Administrator\Downloads\base.apk`（30,752,092 B，仅 arm64-v8a）
分析时间：2026-11-22
目的：对标它的「磁力下载快 + 边下边播」，给 Win-Box / MisaTV 的磁力改造提供参照

---

## 一、结论先行

1. **它快，主要不是因为客户端算法，而是因为接的是迅雷的商业网络。** 下载引擎是迅雷
   `DownloadLib`（P2SP + DCDN + SuperPCDN），播放器是迅雷 `APlayer`。这两样都是**闭源商业 SDK**，
   要授权才能用，GPL 项目不能嵌。
2. **有一个机制是真能抄的**：播放器把播放位置回传给下载引擎（`SynPlayPos`），引擎据此重排
   分片优先级。我们现在的 aria2 完全没做这件事（连 `bt-max-peers` 都没配），这是「首屏慢、拖动卡」的主因。
3. **有一个便宜的大改善**：它用 `torcache.net` 这类公共站点**按 infohash 直接取种子**，
   跳过 magnet 的 DHT 找元数据阶段。我们每开一个磁链都要等 10~40 秒拿元数据，这一条几乎零成本可补。

---

## 二、它是什么（证据）

### 2.1 包体构成

Flutter 应用，单 ABI：

| 文件 | 大小 | 结论 |
|---|---|---|
| `lib/arm64-v8a/libaria2p.so` | 9.1 MB | **名不副实**：不是 aria2，是迅雷 `DownloadLib` |
| `lib/arm64-v8a/libaplayer_android_1.2.3.499.so` | 13.2 MB | 迅雷 `APlayer`，内含 FFmpeg |
| `lib/arm64-v8a/libapp.so` | 11.8 MB | Flutter AOT 业务代码 |
| `lib/arm64-v8a/libflutter.so` | 10.5 MB | Flutter 引擎 |
| `assets/libjiagu_a64.so` + `assets/.jgapp` | 1.7 MB | 加固壳，DEX 不可直读 |

### 2.2 下载引擎：迅雷 DownloadLib

`libaria2p.so` 里的符号与字符串（原样摘录）：

```
Java_com_xunlei_downloadlib_XLLoader_createP2spTask
Java_com_xunlei_downloadlib_XLLoader_createBtMagnetTask
Java_com_xunlei_downloadlib_XLLoader_parserThunderUrl
_ZN11DownloadLib14CreateP2spTaskEP14TAG_TASK_PARAMPm
_ZN11DownloadLib18CreateBtMagnetTaskEP21TAG_TASK_PARAM_MAGNETPm
XLAddBatchDcdnPeerRes / XLStartDcdn / XLStopDcdn
XLSetVipType / XLGetPremiumResInfo
SuperPCDNTaskTailZeroSpeedUpperBaseTime
libxl_thunder_sdk.so            ← 被引用但未打包（运行时加载）
uv_fs_read / uv_fs_rename …     ← 自带 libuv
```

`grep -c aria2` = **0**。所以文件名是伪装，实际是迅雷 P2SP 引擎。

### 2.3 播放器：迅雷 APlayer

```
_ZN14APlayerAndroid...            RegisterAplayerAndroidMethods
Lavf/57.71.100                    ← FFmpeg 3.3 分支
mediacodec                        ← 硬解
_ZN13APlayerParser19set_use_buffer_seekEb
```

### 2.4 边下边播怎么串起来的

最关键的两条字符串：

```
http://127.0.0.1:%d/%s/%s/%s     ← 本地 HTTP 服务，端口 + 三段路径
XLSynPlayPos / VodPlayServer::SynPlayPosEmm
```

前者是**给播放器的本地流地址**，后者是**播放位置回传**。合起来就是：

```
磁链 → 引擎拿元数据 → 建 BT 任务 → 本地 HTTP 服务暴露可读区间
     → 播放器请求 Range → 播放器定期回报 playPos → 引擎按 playPos 重排分片优先级
```

这是一套标准的「VOD 服务 + 播放点窗口调度」，不是简单的「下完再播」。

### 2.5 元数据加速

```
https://torcache.net/torrent/<infohash>
http://bt.box.n0808.com/
```

按 infohash 直取 `.torrent`，绕过 DHT/peer 交换元数据。这一条对「点开就播」的体感贡献很大。

### 2.6 其它

- 有 `XLSetVipType` / `XLGetPremiumResInfo`：**速度与账号会员绑定**，非会员拿不到 DCDN 加速
- Bugly + Umeng 统计，Alipay SDK，`MANAGE_EXTERNAL_STORAGE` 等宽权限

---

## 三、为什么快：三条原因，逐条判定能否复制

| 原因 | 机制 | 能否复制 |
|---|---|---|
| ① P2SP + DCDN + SuperPCDN | 迅雷自有节点缓存热门资源，等于「有人已经下过，直接就近推给你」 | **不能**。这是商业网络资产，与客户端代码无关 |
| ② 播放点窗口调度 | `SynPlayPos` 把播放位置告诉引擎，引擎优先取当前窗口的分片 | **能**。libtorrent 的 `set_piece_deadline` / `sequential_download` 就是干这个的 |
| ③ 元数据直取 | torcache 按 infohash 取种子，跳过 DHT 等待 | **能**。一个 HTTP GET 的事 |

换句话说：**速度上限追不上（①），但起播时间和拖动体验可以追平（②③）。**

---

## 四、我们现在的水平

### 4.1 桌面端（Win-Box）

- 引擎：`resources/app.asar.unpacked/resources/aria2/aria2c.exe`
- 缓存：`data/winbox-cache/bt/`，`.torrent` 与 `.aria2` 控制文件都在
- `magnet.py` 只负责把磁链/种子解析成 magnet 交给 App 的 `/bt` 接口，不碰下载参数
- 实际参数（从 `app.asar` 里逐字取出）：

```
--bt-prioritize-piece=head=8M,tail=8M
--file-allocation=none
--disk-cache=32M
--seed-time=0
--bt-tracker-connect-timeout=10
--enable-dht=true
--enable-rpc / --rpc-listen-port / --rpc-secret
--dir / --continue=true / --bt-save-metadata
```

这套是**调过的**（首尾各 8 MB 优先、不预分配、32 MB 磁盘缓存），不是裸跑。
但 BT 侧真正影响吞吐的三个开关没开：

```
bt-max-peers        默认 55，没放开
bt-request-peer-speed-limit   没设
listen-port         没固定（默认 6881-6999 随机段，影响做种回报）
```

> 勘误：本报告初版曾建议加 `--split` 与 `--max-connection-per-server`。这两个是
> **HTTP/FTP 分段下载**的参数，对 BT 无效（BT 的并发单元是 peer，不是分段）。
> 已更正为上面三项。

### 4.2 手机端（MisaTV / WinBox-1.2.0-arm64）

**结论：和桌面是同一套设计，不是「手机端还没有」。** 实查 `WinBox-1.2.0-arm64.apk`：

| 环节 | 桌面 | 手机 |
|---|---|---|
| Python 源 | `magnet.py` md5 `19cf1586727d90deb387188f0e9a4137` | **逐字节相同**（同样 md5） |
| aria2 引擎 | `aria2c.exe`（随 App 解包） | `assets/flutter_assets/assets/aria2/aria2c-android-arm64`，**aria2 1.37.0**，aarch64 ELF 6.1 MB，首播时释放到应用数据目录并 `chmod +x` |
| 拉起方式 | Electron 主进程 | Dart `package:kniephos/services/torrent/aria2_sidecar.dart` |
| RPC 调用 | `aria2.tellWaiting` / `pause` / `changeOption` / `getGlobalStat` | **同一组方法** |
| 流服务 | App 的 `/bt` 路由 | Dart 路由 `/bt/([0-9a-fA-F]{40})/(\d{1,6})/?$`，即 `/bt/<infohash>/<文件序号>` |
| 播放器 | ffmpeg（Electron 侧） | `libfvp.so` + `libmdk.so` + `libffmpeg.so` |
| aria2 参数 | 见 4.1 | **逐字相同**（`head=8M,tail=8M`、`none`、`32M`、`0`、`10`） |

所以之前记的「Muse 待办：aria2 二进制」已经不成立了——这个包里已经内置，而且 sidecar、
RPC、流路由都写好了。

手机端多出来的三个坑（不在桌面存在）：

1. **HTTPS tracker 拿不到 CA**：随包 README 自己写了「系统证书不在标准 Unix 路径，暂不支持」，
   所以 HTTPS tracker 实际无效，元数据只能靠 DHT 与 UDP/HTTP tracker → **冷门种子起播更慢**
2. **后台保活**：Android 会杀进程，需要前台服务 + 通知，否则息屏后 BT 断流
3. **存储与 IO**：`file-allocation=none` 已经对了，但手机闪存随机写比桌面慢，
   `disk-cache=32M` 在低端机上可能偏小

### 4.3 和迅雷的真实差距

| 维度 | 黑夜下载 | 我们 |
|---|---|---|
| 网络 | 私有 P2SP + DCDN | 纯公网 BT/DHT |
| 起播 | 元数据直取 + 首发位置抢占 | 等 DHT 拿元数据，从头顺序下 |
| 拖动 | 播放点窗口调度 | 无调度，靠 aria2 自己的策略 |
| 硬解 | MediaCodec | 桌面靠 ffmpeg，手机看壳 |
| 成本 | 商业授权 | 免费 |

---

## 五、可落地的改造（按性价比排序）

### A. 元数据直取（半天，收益最直接）

磁链进来先按 infohash 打这几个源，命中就直接拿 `.torrent`，失败再回落到 DHT：

```
https://itorrents.org/torrent/<INFO_HASH>.torrent
https://torcache.net/torrent/<INFO_HASH>.torrent
https://btdig.com / https://bt4g.org   （按 hash 搜）
```

注意 infohash 大小写两种都要试（base16 大写最常见）。命中后交给 aria2 走
`--bt-save-metadata` 或直接喂 `.torrent` 文件。

### B. aria2 参数补齐（半天，两端同一份改动）

两端现在用的是**同一份参数表**，所以改一处两边同时生效：

```conf
# 新增（BT 真正相关的三项）
bt-max-peers=128
bt-request-peer-speed-limit=0
listen-port=6881-6891

# 建议调（现有值偏保守）
bt-prioritize-piece=head=32M,tail=8M
disk-cache=64M
```

- `bt-max-peers` 默认 55，热门种子容易被「连接数够了但每 peer 给得少」卡住
- `bt-request-peer-speed-limit=0` 表示不限制请求速率（默认就是 0，但显式写出来避免被别处覆盖）
- `listen-port` 固定一段，做种端更容易回连，冷门种子收益明显
- `head=32M`：现在 8 M 大约只够 1080p 两秒，4K 首帧会卡在缓冲

**不要加** `--split` 和 `--max-connection-per-server`——那是 HTTP/FTP 分段参数，BT 不吃。

### C. 播放点窗口调度（大头，但这是体验分水岭）

aria2 **做不到**动态窗口调度（它没有「告诉我播放到哪，我重排优先级」的接口）。
要做到迅雷那种效果，得换引擎：

- **Android**：`libtorrent4j`（AAR，Kotlin/Java 可直接用），用
  `torrentHandle.setSequentialDownload(true)` + `setPieceDeadline(piece, ms)` 实现
  「播放点前后 N MB 高优先级，其余降级」
- **桌面**：`python-libtorrent`（有 Windows wheel），同样 API
- 迁移成本：要自己管 session、save_resume_data、监听端口、tracker 列表

折中方案（不动引擎）：aria2 RPC 的 `aria2.changeOption` 无法改分片优先级，
所以只能靠 **B 的 head 优先 + 预取前 64 MB** 拿到「起播快」，拖动仍会卡。
如果只求「点开能播」，B 就够；要求「随便拖」，必须上 C。

### D. 本地 Range 服务（手机端已有，需要验而不是建）

播放器不能直接读正在写的文件（mp4 moov 在尾部时尤其不行），必须有一个本地 HTTP 服务。
**这一层两端都已存在**：

- 桌面：App 的 `/bt` 路由
- 手机：Dart 路由 `/bt/([0-9a-fA-F]{40})/(\d{1,6})/?$`

需要实测确认三件事（缺一件就会「能下不能看」）：

1. 是否返回 `Accept-Ranges: bytes` 与正确的 `206 Partial Content`
2. 请求落在**未完成区间**时，是阻塞等待还是立刻 503（后者会让播放器直接判定失败）
3. seek 到未下载区域后能否续上，而不是整段重下

判断「哪些区间已完成」：aria2 的 `.aria2` 控制文件格式未公开、不建议解析；
这也是想上窗口调度时应该换 libtorrent 的理由（`pieceAvailability` 直接可读）。

---

## 六、不能做的部分

- **迅雷 DownloadLib / APlayer 不能嵌**：闭源商业 SDK，需要授权；本项目 GPL-3.0，
  链接闭源库会污染许可证。仅可作为行为参照。
- **DCDN/P2SP 网络无法自建**：那是迅雷的节点资产。
- **不要抄它的伪装命名**（`libaria2p.so` 实为迅雷库）：我们自己的库名要能自证。

---

## 七、验收指标（建议 Muse 按这个测）

| 指标 | 现状（估） | B 之后 | C 之后 |
|---|---|---|---|
| 磁链点开到出画面 | 10~40 s | 5~15 s | 3~8 s（热门种子） |
| 首段缓冲 | 无保证 | 前 32 MB 优先 | 播放点 ±16 MB 优先 |
| 拖动到未下载区 | 长时间卡死 | 仍卡 | 数秒内续播 |
| 冷门种子（<5 peer） | 可能拿不到元数据 | 元数据仍靠 DHT | 同左 |

测法：固定 3 个种子（热门 4K、冷门老片、多文件合集），各测 3 次取中位数，
记录「点开→首帧」和「拖动→续播」两个时间。

---

## 八、给 Muse 的一句话

先做 A 和 B——两端共用同一份参数表，改一次两边同时受益，一天量级。
C（窗口调度）要换引擎，手机端代价比桌面大（aria2 sidecar、RPC、`/bt` 路由都要重写），
等 A/B 的实测数据出来再决定值不值得。
D 不用新建，两端都已经有 `/bt` 路由，实测那三条（Range 头、未完成区间行为、seek 续播）即可。
手机端另外还有两个桌面没有的坑：HTTPS tracker 无 CA（随包 README 自述）、息屏后台被系统杀。
