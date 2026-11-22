# 磁力源

搜索框支持两种输入，直接交给 Win-Box 内置 BT 引擎边下边播：

1. `magnet:?xt=urn:btih:...`（磁力链接）
2. `.torrent` 种子：
   - 本地路径：`D:/下载/xxx.torrent`
   - file URL：`file:///D:/下载/xxx.torrent`
   - 网络 URL：`https://example.com/xxx.torrent`

种子会在源里解析出 infoHash（SHA1 of info 段）+ tracker + 名称，拼成 magnet 再播放，
所以不需要改 Win-Box 主程序。mp4/webm 内置直接播；mkv/HEVC 走配置页「磁力外部播放器」。

安装路径：`D:/TVbox/Win-Box/data/imports/py/452f6c6a-8e6d-44e0-a1c1-8891284b98e5/magnet.py`

## 最近解析 / 管理（与 PikPak 源同构）

首页多了「最近解析」分类，交互契约与 `pikpak.py` 完全一致：

| 位置 | vod_id | 行为 |
| --- | --- | --- |
| 分类首行 | `recent.manage` | 进管理页（folder） |
| 管理页首行 | `recent.del.all` | 全选删除 |
| 管理页逐条 | `recent.del.<i>` | 单条删除 |
| 记录卡片 | `n.<b64(magnet)>` | 点开进详情，与 `m.` 等价 |

- 记录文件：`magnet-state.json`（与 py 同目录；目录不可写时退到临时目录）
- 同一磁链只留一条；占位名（`磁力 xxxxxxxx`）不会覆盖已经拿到的真实片名
- ext 里 `"remember": "0"` 可关闭记录

## 片名：裸磁链也能显示真实名字

裸磁链没有 `dn=`，以前只能显示 `磁力 ED27491B`。现在按这个顺序取名字：

1. magnet 里的 `dn=`
2. aria2 落地的 `<infohash>.torrent`（播放过一次之后就有）→ 解析 `info.name`
3. 兜底 `磁力 <hash8>`

缓存目录按 py 自身位置上溯推断（桌面 `<data>/winbox-cache/bt`，手机 `bt/`、`app_flutter/bt` 等），
全部不存在就当没有，不影响其它逻辑。

**首次播放前仍可能是 hash 名**：公开的 infohash→种子源（itorrents / torcache / btdig）
本机实测全部不可用（403 / 连不通），所以没法在第一次播放前凭空拿到名字。
播过一次之后再进「最近解析」就是真实片名了。

## 种子本地缓存

粘贴 `.torrent`（本地路径 / `file://` / `http(s)`）时，把种子原样存一份到 bt 缓存目录的
`<infohash>.torrent`，下次解析同一磁链可以直接用它取片名，也省一次下载。

## 已知边界：拖动不能「秒播」

`/bt` 端点本身没问题——实测任意偏移 `Range` 都是 5ms 返回 206。卡点在引擎：

- aria2 只有 `--bt-prioritize-piece=head,tail`（首尾两段），**没有**「按播放点重排分片」的接口
- 拖动到还没下载的位置时，服务端的 piece 门控只能等那片到货（45 秒超时后 503）
- 迅雷能做到，是因为它自己的引擎有 `SynPlayPos`（播放器回报播放位置 → 重排优先级）

要真正实现分块秒播，只能换引擎：libtorrent 的 `set_piece_deadline()` + `sequential_download`
就是干这个的（Android 用 `libtorrent4j`，桌面用 `python-libtorrent`）。这是换心脏的活儿，
不是调参能解决的。


## TG 频道 / 资源站导入

磁力源也可以从公开 TG 频道或资源站网页里抓 `magnet:` 链接，自动变成可播条目：

- ext 配置：
```json
{
  "proxy": "http://127.0.0.1:7890",
  "channels": ["频道名1", "频道名2"],
  "site_urls": ["https://example.com/magnet-list.txt"],
  "tg_pages": 2
}
```
- `channels`：TG 公开频道 username（只能读公开预览页 `t.me/s/<频道>`，私密/关历史的读不到）；
- `site_urls`：发磁力的网页/纯文本 URL（GitHub Gist raw、列表页都行），源抓页面里的 `magnet:?xt=`；
- 首页会出现「资源站1」「TG·xxx」分类；搜索关键词也会在这些来源里过滤；
- 抓到的磁链走内置 aria2 边下边播，和手动粘贴 magnet 完全一样；
- 结果缓存 5 分钟（内存），切标签不重复抓。

**实测源**（已配好）：GitHub Gist 公开磁链列表，抓到 10 条真实磁链，
分类/详情/播放/搜索全链路验证通过。要加自己的频道/站点，改 ext 的 `channels` / `site_urls` 即可。
