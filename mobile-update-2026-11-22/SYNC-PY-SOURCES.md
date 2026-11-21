# PY 源核对：本地 PY + 订阅链接（2026-11-22 01:20，WorkBuddy → Muse）

> 本文是**同步与核对记录**，不是执行指令。是否采纳、如何采纳，由用户与你判断。
> 本文取代同目录 `SYNC-SUBSCRIPTION.md` 的上一版（01:15），补上了第一版漏掉的**订阅链接**这一层。

---

## 0. 一句话结论

PY 源有**两个来源层**，两边都得分清：

- **本地 PY**：4 份唯一内容（113,610 / 124,383 / 123,211 / 121,169 B），散在 9 个路径
- **订阅链接**：`https://uzsub-n7k3q.pages.dev/py81/pikpak.py`，**97,054 B / md5 `9ec62735f515b84b22c41fbe8ff10d3b`**

订阅链接那份是**最老的**（比用户最初那份还小 24 KB），没有 `MOBILE` 分流、没有单链下钻，
封面硬编码 `http://127.0.0.1:9978/img?u=…`。**手机一旦导入这个订阅，封面必然全白。**

---

## 1. 本地 PY 清单

### 1.1 四份唯一内容

| 编号 | 称呼 | 字节 | md5 | 定位 |
|---|---|---|---|---|
| L1 | 桌面版 | 113,610 | `9c35d6e26a188644fbabe869306ca1c7` | 仅 Win-Box：封面走 9978 中继、图片自动弹内置画廊、不给图集线路 |
| L2 | 通用版 | 124,383 | `9ea5413f42d0930e0ef1918d96af948a` | 桌面/手机同一份：`MOBILE` 分流、`_share_dive` 重构、预填、状态锁 |
| L3 | 图集版 | 123,211 | `d814691aae4bfc9cea5ccb38665791c6` | 手机（壳有图集页）：Muse 00:37 底座 + 4 处 |
| L4 | 旧版 | 121,169 | `71991562ffe8f0bc16a56c6dca71cfbc` | 用户最初给我那份（你说过"手机端图片打不开"的那份），已退役 |

### 1.2 九个副本落在哪

| 路径 | 内容 | 谁在用 |
|---|---|---|
| `D:/TVbox/Win-Box/data/imports/py/85cda3fa-…/pikpak.py` | L1 | **桌面当前运行** |
| `D:/workbuddy/影视聚合/winbox-pikpak/pikpak.py` | L1 | 项目内桌面源（与上一份同内容） |
| `D:/workbuddy/影视聚合/mobile/pikpak.py` | L2 | 通用版主文件 |
| `D:/workbuddy/影视聚合/mobile/pikpak-图集版.py` | L3 | 图集版主文件 |
| `C:/Users/Administrator/Downloads/pikpak-phone.py` | L3 | **交给用户装到手机** |
| `C:/Users/Administrator/Downloads/pikpak-fixed.py` | L2 | 上一轮交付件（已被 L3 取代） |
| `C:/Users/Administrator/Downloads/pikpak.py` | L4 | 用户最初那份，退役 |
| `winbox-apk/mobile-update-2026-11-22/pikpak.py` | L2 | 交接给你 |
| `winbox-apk/mobile-update-2026-11-22/pikpak-gallery-edition.py` | L3 | 交接给你 |

> 另有一份 L2 打包在 `mobile/pikpak-懒人包.zip`（含订阅引导 JSON + 记录 CSV）。

---

## 2. 订阅链接清单

### 2.1 整包订阅（profile 这一层）

Win-Box 的 `user-config.json` 里有 **10 个 profile**，只有 2 个含 PikPak 源：

| profile 名 | apiUrl（整包订阅链接） | 源数 | 含 PikPak | PikPak 的 api |
|---|---|---|---|---|
| **本地PY源**（当前激活） | *（空，本地配置）* | 81 | ✅ | `file:///D:/TVbox/…/pikpak.py`（L1） |
| 自建 | `https://uzsub-n7k3q.pages.dev/sub-all.json` | 81 | ✅ | `https://uzsub-n7k3q.pages.dev/py81/pikpak.py` |
| 路漫漫动漫 | `https://9280.kstore.vip/newwex.json` | 100 | ❌ | — |
| 王二小 | `https://9280.kstore.vip/newwex.json` | 100 | ❌ | — |
| 摸鱼儿 | `http://www.y456y.com` | 100 | ❌ | — |
| 专属 | `https://uzsub-n7k3q.pages.dev/sub-my.json` | 51 | ❌ | — |
| hh-tvbox | `https://uzsub-n7k3q.pages.dev/sub-my.json` | 11 | ❌ | — |
| 嗷呜 | `https://1.wudaozhe.net/tvbox/南风.json` | 91 | ❌ | — |
| 新订阅 11-06 | `https://9763.kstore.vip/aowu.json` | 80 | ❌ | — |
| 蝴蝶 | `https://sub.lytvs.top/get?token=tk_b50d490b` | 340 | ❌ | — |

**关键**：当前激活的是「本地PY源」，`apiUrl` 为空 → **桌面现在不读订阅链接，读本地文件 L1**。
一旦切到「自建」，PikPak 就会变回 py81 那份老版。

`sub-my.json`（28 源）内**没有** PikPak 条目，所以「专属」「hh-tvbox」两个 profile 是没有 PikPak 源的。

### 2.2 源文件链接（L2 层）

```
https://uzsub-n7k3q.pages.dev/py81/pikpak.py
  97,054 B   md5 9ec62735f515b84b22c41fbe8ff10d3b
```

`sub-all.json` 里这一条的完整内容：

```json
{"key":"pikpak","name":"PikPak 网盘","type":3,
 "api":"https://uzsub-n7k3q.pages.dev/py81/pikpak.py",
 "searchable":1,"quickSearch":1,"changeable":1,"filterable":1,
 "timeout":60,"playerType":2,
 "ext":"{\"channels\":[\"longzhuu\",\"DF6D6FG5G6F\"],\"tg_pages\":3,\"use_token_restore\":false,\"share_proxy\":\"http://127.0.0.1:9978\"}"}
```

### 2.3 设备侧（订阅里 `api` 字段怎么写）

| 端 | api 值 | ext | 备注 |
|---|---|---|---|
| 桌面 | `file:///D:/TVbox/…/pikpak.py` | `{"proxy":"http://127.0.0.1:7890"}` | 绝对路径，带代理 |
| 手机 | `file://Download/pikpak.py` | `{}` | 相对 Download，靠源内自动探代理 |

---

## 3. 六份源的能力矩阵

`Y` = 有，`.` = 没有

| 文件 | 字节 | md5 前 8 | 下钻 | 手机分流 | 图片 | 缩略图 | 9978 | 画廊 | 历史记目录 | 预填 | 粘贴导入 | 账号Token | TG扫描 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 订阅链接 py81 | 97,054 | `9ec62735` | . | **.** | Y | Y | Y | Y | . | . | . | . | Y |
| 用户最初（L4） | 121,169 | `71991562` | . | Y | Y | Y | Y | Y | . | . | Y | . | Y |
| 通用版（L2） | 124,383 | `9ea5413f` | Y | Y | Y | Y | Y | Y | Y | Y | . | . | Y |
| 图集版（L3） | 123,211 | `d814691a` | ※ | Y | Y | Y | Y | Y | Y | . | Y | . | Y |
| 桌面版（L1） | 113,610 | `9c35d6e2` | Y | . | Y | Y | Y | Y | Y | Y | . | . | Y |
| Muse 00:37 APK 内 | 122,201 | `c7469e98` | ※ | Y | Y | Y | Y | Y | . | . | Y | . | Y |

- ※ = 有下钻，但不叫 `_share_dive`（是 Muse 的原生循环，范围限分享根）；L3 实测能钻 1 层（见第 5 节）
- 「账号Token」「TG扫描」「粘贴导入」三列对本次问题无关，只是用来说明血统：所有版本都源自同一个 uz panTools 底子

---

## 4. 订阅链接那份的具体问题（有行号）

| # | 位置 | 现象 | 影响 |
|---|---|---|---|
| 1 | `:1686` `:1736` `:1737` `:1775` | `vod_pic` / 剧集封面硬编码 `http://127.0.0.1:9978/img?u=…`，**无 `MOBILE` 分支** | 手机上 127.0.0.1 无人监听 → 封面全白 |
| 2 | 全文 | 无单链下钻 | 17/18 条记录点开只看到一个文件夹（数据见第 5 节） |
| 3 | `:1299` `:1747` | 依赖 `127.0.0.1:9978/import-csv`、`/galleryopen` | 手机端这两个接口不存在 |
| 4 | `sub-all.json` 的 ext | `"share_proxy":"http://127.0.0.1:9978"`、`"playerType":2` | 同样是桌面假设 |

这四条合起来就是一句话：**py81 是纯桌面源，手机不能直接用它。**

---

## 5. 顺手跑出的实测数据

### 5.1 单链下钻是刚需

用户本机 18 条真实解析记录逐条下钻：

| 下钻层数 | 记录数 |
|---|---|
| 1 层 | 16 |
| 2 层 | 1 |
| 0 层 | 1 |

**17/18 条记录在根目录只有一个文件夹。** 不下钻等于点开什么都没有。

### 5.2 `max_files = 300` 会截断

同批 18 条里 5 条末层正好取到 300 项：

| 分享 | 末层项数 | 图片 | 视频 | 目录 |
|---|---|---|---|---|
| `VP3BgtoY…` | 300 | 116 | 142 | 42 |
| `VOpS4nId…` | 300 | 124 | 176 | 0 |
| `VP3q7SUY…` | 300 | 0 | 0 | 300 |
| `VOuCvo1P…` | 300 | 131 | 167 | 2 |
| `VP5rYawq…` | 300 | 1 | 299 | 0 |

连播集数与图集张数都受它影响，所以图集上限跟着 `max_files` 走，改一个数两边一起变。

### 5.3 图集版：图片线路写死 40 张（已修）

```python
imgs = imgs[:40]      # 上游血统，Muse 最初那份 / 00:37 底座 / 图集版各 1 处
```

真实分享实测（数「图片」线路集数）：

| 分享 | 目录内图片 | 改前 | 改后 |
|---|---|---|---|
| `VOuCvo1P…` | 131 | 40 | **131** |
| `VOpS4nId…` | 124 | 40 | **124** |
| `VP3BgtoY…` | 116 | 40 | **116** |

改为 `imgs[:self.max_files]`（默认 300，与列表上限一致，不多发请求）。
首末集缩略图匿名 `GET` 均 `200 image/jpeg`（97,817 B / 149,520 B），无 UA、无 Referer、无鉴权。

---

## 6. 待你同步的信息（编号回答即可）

1. **订阅链接那份 py81（97,054 B / `9ec62735`）是你维护的吗？** 它现在还是 `sub-all.json` 里的正式条目，但没有任何近期修复。
2. 你那边「订阅同步用」那份 py 的 md5 是不是 `c7469e985e6b6de7c7eac066cb523564`（= 00:37 APK 内置那份）？
3. 手机侧加载优先级：`Download/pikpak.py` 与 APK 内置 `assets/python_sources/pikpak.py`，哪个赢？我推断 Download 优先。
4. `imgs[:40]` 是刻意的还是随手写的？画廊页 `PageView` + `InteractiveViewer` 吃 130+ 张有无性能问题？
5. `max_files = 300` 要不要提高？5 条记录已撞线。
6. 图集页代码推 fork 了吗？`main` 的 `player_page.dart` 我这边仍只看到 `looksLikeImageMediaUrl()` 拦截那段。
7. 你那边是否也维护「桌面版 / 手机版」两份 py？我这边四份，分叉点已经很多，想确认要不要收敛成「一份 + `MOBILE` 分流」。

---

## 7. 指纹与仓库变更

```
winbox-apk/mobile-update-2026-11-22/pikpak-gallery-edition.py
  123,211 B
  md5    d814691aae4bfc9cea5ccb38665791c6
  sha256 f0603c5d10e0564bfb7e198502aac89b5fdf9cf4c88029d66adfe309a1c59feb

winbox-apk/mobile-update-2026-11-22/pikpak.py
  124,383 B
  md5    9ea5413f42d0930e0ef1918d96af948a
```

| commit | 内容 |
|---|---|
| `ae043ba` | 图集版 `imgs[:40]` → `imgs[:self.max_files]`，CHANGELOG 同步指纹与实测表 |
| `d4e2316` | 第一版核对报告（只写了设备侧 api，漏了订阅链接层） |
| 本文件 | 补齐「本地 PY 清单 + 订阅链接三层 + 能力矩阵」 |

`git clone` 与 `raw.githubusercontent.com` 取到的字节一致（LF）。
