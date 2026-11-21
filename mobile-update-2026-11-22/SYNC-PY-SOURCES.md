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

---

## 8. 附：「本地PY源」profile 的 81 个源清单

桌面 `user-config.json` 里 `activeProfileId = 未命名配置-vj07`（显示名「本地PY源」，`apiUrl` 为空）。
该 profile 共 **81 条源**，本次逐条落地核对如下。

| 汇总项 | 值 |
|---|---|
| 源总数 | 81 |
| 本地文件源（`file://`） | **53**，全部存在，0 缺失 |
| 远程链接源（`https://`） | **28** |
| 本地文件合计 | 1,572,848 B（1.50 MB） |
| 本地内容重复 | 0 组（53 份 md5 互不相同） |
| 源类型 | 80 个 `type=3`（Python）+ 1 个 `type=1` |
| 落地位置 | `D:/TVbox/Win-Box/data/imports/py/<uuid>/<源名>.py`，一源一目录 |

### 8.1 本地文件源（53 条）

| # | 源名称 | 字节 | md5 前 8 | 落地路径（缩短） |
|---|---|---|---|---|
| 1 | 萝莉岛 | 11,587 | `c8e4db5e` | `a691062d/…/萝莉岛.py` |
| 2 | 全剧得-三端爬虫 | 28,691 | `d3f64d9c` | `9a277bb5/…/全剧得-三端爬虫.py` |
| 3 | 豆包篇庫-三端爬虫 | 43,639 | `08428926` | `268eedbc/…/豆包篇庫-三端爬虫.py` |
| 4 | 萝莉岛(luo li dao) | 11,285 | `7a2e3e89` | `0c9bea51/…/萝莉岛(luo li dao).py` |
| 5 | 少女印象 | 20,812 | `56c8b164` | `efec3a86/…/少女印象.py` |
| 6 | 99影視TV261030 | 22,268 | `0b1f4758` | `37aa5f23/…/99影視TV261030.py` |
| 7 | 听友小说 | 14,534 | `d6b59b2e` | `8fce3474/…/听友小说.py` |
| 8 | 稀饭动漫 | 26,963 | `a8def96a` | `35648cc4/…/稀饭动漫.py` |
| 9 | 4虎 | 28,636 | `89df05d8` | `3b957bf3/…/4虎.py` |
| 10 | 影探4K(新) | 15,702 | `84083923` | `2529ee85/…/影探4K(新).py` |
| 11 | 橘汁视频 | 34,323 | `8bf78bcc` | `b72d4b02/…/橘汁视频.py` |
| 12 | LibVIO | 32,707 | `9b30296a` | `c799eab6/…/LibVIO.py` |
| 13 | 山楂4K | 22,904 | `8ca05f30` | `f7abbf5d/…/山楂4K.py` |
| 14 | 可可 | 14,607 | `23e17349` | `a46bdde9/…/可可.py` |
| 15 | 暗网禁地-TVBox爬虫 | 31,920 | `1c58666c` | `a09e4d99/…/暗网禁地-TVBox爬虫.py` |
| 16 | 吃瓜网 | 26,253 | `34424c1f` | `0da37c78/…/吃瓜网.py` |
| 17 | 啊哈DJ | 14,012 | `a04d2969` | `6069ec53/…/啊哈DJ.py` |
| 18 | sweetpornos | 15,892 | `edecdd14` | `55d4c2f8/…/sweetpornos.py` |
| 19 | 埋堆堆_fixed | 36,028 | `4854f625` | `bad646e0/…/埋堆堆_fixed.py` |
| 20 | 埋堆堆加分类 | 55,689 | `1b301e49` | `5bdf31f3/…/埋堆堆加分类.py` |
| 21 | 51吃瓜修复v2 | 36,215 | `2c194392` | `a30bcd29/…/51吃瓜修复v2.py` |
| 22 | 听友听书 | 20,637 | `2319556e` | `4f8cb2e9/…/听友听书.py` |
| 23 | 蓝光资源 | 45,585 | `03ce7586` | `189bb066/…/蓝光资源.py` |
| 24 | 18koreannet | 12,595 | `c1d2b45a` | `a1aaded5/…/18koreannet.py` |
| 25 | 718吃瓜(代理)-阿飞修复 | 21,949 | `a3999685` | `d8b4cfc0/…/718吃瓜(代理)-阿飞修复.py` |
| 26 | 718吃瓜 | 22,590 | `32cd3a29` | `c3fe9ca7/…/718吃瓜.py` |
| 27 | AVGC9 | 10,097 | `ccbcea63` | `dd2e8187/…/AVGC9.py` |
| 28 | cat3movie | 16,445 | `ea502e82` | `38490136/…/cat3movie.py` |
| 29 | javguru | 31,616 | `868c214e` | `a278986a/…/javguru.py` |
| 30 | Korean porn av | 10,253 | `ba73147c` | `81451a85/…/Korean porn av.py` |
| 31 | krx18 | 23,085 | `1e5f2144` | `bbfdd55b/…/krx18.py` |
| 32 | one一个-阿飞修复 | 19,134 | `ede337f8` | `7c491111/…/one一个-阿飞修复.py` |
| 33 | paradise US | 29,229 | `b97c9973` | `8b49fd31/…/paradise US.py` |
| 34 | trendypornUS | 9,501 | `209e61cf` | `27e7e077/…/trendypornUS.py` |
| 35 | 菠菜园-阿飞修复 | 56,737 | `d05bb674` | `d7923208/…/菠菜园-阿飞修复.py` |
| 36 | 香蕉短剧(代理)-阿飞修复 | 9,572 | `c8e92775` | `b235c8f0/…/香蕉短剧(代理)-阿飞修复.py` |
| 37 | 影探网 | 12,520 | `00f81d70` | `5a34f71e/…/影探网.py` |
| 38 | AnimeSex | 10,655 | `5c101f52` | `0f22b9d4/…/AnimeSex.py` |
| 39 | av123 | 13,944 | `4c86f89c` | `29007baa/…/av123.py` |
| 40 | nhentai_分享版 | 28,348 | `b062f560` | `7d11a075/…/nhentai_分享版.py` |
| 41 | taiav | 16,752 | `3eda9467` | `41bee520/…/taiav.py` |
| 42 | youwu-fixed-v2 | 36,731 | `523c1094` | `0bc132df/…/youwu-fixed-v2.py` |
| 43 | 黄色仓库 | 28,268 | `4a23d109` | `5901f07e/…/黄色仓库.py` |
| 44 | 巨婴云播🔞 | 13,283 | `bc95eef4` | `e547eedd/…/巨婴云播🔞.py` |
| 45 | 埋堆堆修复版 | 239,582 | `a85b991f` | `97cbe4a0/…/埋堆堆修复版.py` |
| 46 | 小黄鸭 | 16,738 | `3940914e` | `ff993d6c/…/小黄鸭.py` |
| 47 | 演唱会 | 40,947 | `b18941c0` | `ab99be04/…/演唱会.py` |
| 48 | 直播 | 8,423 | `ce080760` | `9bb43162/…/直播.py` |
| 49 | Miss av高清修复版 | 42,643 | `c55537f1` | `f741a397/…/Miss av高清修复版.py` |
| 50 | UU视频安卓 | 25,703 | `d24cd0d5` | `9c8948f7/…/UU视频安卓.py` |
| 51 | PikPak 网盘 | 113,610 | `9c35d6e2` | `85cda3fa/…/pikpak.py` |
| 52 | 演唱会·B站(JS) | 23,601 | `1402fd9c` | `1c0a3923/…/演唱会.js` |
| 53 | 磁力 | 17,408 | `19cf1586` | `452f6c6a/…/magnet.py` |

### 8.2 远程链接源（28 条）

全部指向同一个 Cloudflare Pages 站点的 `/py/` 目录：

| # | 源名称 | 链接 |
|---|---|---|
| 1 | 淫花宫（直连） | `https://uzsub-n7k3q.pages.dev/py/yinhuagong.py` |
| 2 | 污漫漫画（直连） | `https://uzsub-n7k3q.pages.dev/py/wuman.py` |
| 3 | 黑料718（直连） | `https://uzsub-n7k3q.pages.dev/py/heiliao718.py` |
| 4 | 色花堂（直连） | `https://uzsub-n7k3q.pages.dev/py/sehuatang.py` |
| 5 | 91吃瓜影视（需梯子） | `https://uzsub-n7k3q.pages.dev/py/chigua91.py` |
| 6 | 小穴生（直连） | `https://uzsub-n7k3q.pages.dev/py/xiaoxuesheng.py` |
| 7 | 小淫虫（需梯子） | `https://uzsub-n7k3q.pages.dev/py/xiaoyinchong.py` |
| 8 | 91porn（直连） | `https://uzsub-n7k3q.pages.dev/py/porn91.py` |
| 9 | 桃花视频（直连） | `https://uzsub-n7k3q.pages.dev/py/taohua.py` |
| 10 | UU视频（直连） | `https://uzsub-n7k3q.pages.dev/py/uuvideo.py` |
| 11 | 蝴蝶·BestJavPorn（需梯子） | `https://uzsub-n7k3q.pages.dev/py/bestjav.py` |
| 12 | 九合一（需梯子） | `https://uzsub-n7k3q.pages.dev/py/jiuheyi.py` |
| 13 | 暗网禁地（需梯子） | `https://uzsub-n7k3q.pages.dev/py/anwang.py` |
| 14 | LibVIO（直连） | `https://uzsub-n7k3q.pages.dev/py/libvio.py` |
| 15 | MissAV（需梯子） | `https://uzsub-n7k3q.pages.dev/py/missav.py` |
| 16 | 奇优影院（直连） | `https://uzsub-n7k3q.pages.dev/py/qiyou.py` |
| 17 | Javday（需梯子） | `https://uzsub-n7k3q.pages.dev/py/javday.py` |
| 18 | 蝴蝶影院（直连） | `https://uzsub-n7k3q.pages.dev/py/hudie.py` |
| 19 | GetAV（直连） | `https://uzsub-n7k3q.pages.dev/py/getav-v2.py` |
| 20 | StripChat（直连） | `https://uzsub-n7k3q.pages.dev/py/stripchat-v2.py` |
| 21 | 路漫漫动漫（直连） | `https://uzsub-n7k3q.pages.dev/py/lmm.py` |
| 22 | 浮生影院（直连） | `https://uzsub-n7k3q.pages.dev/py/fusheng.py` |
| 23 | 911爆料（直连） | `https://uzsub-n7k3q.pages.dev/py/bao911.py` |
| 24 | 电影天堂（直连） | `https://uzsub-n7k3q.pages.dev/py/dytt.py` |
| 25 | 闪电影视（直连） | `https://uzsub-n7k3q.pages.dev/py/sd.py` |
| 26 | 非凡影视（直连） | `https://uzsub-n7k3q.pages.dev/py/ffzy.py` |
| 27 | TG媒体库·目标机（直连） | `https://uzsub-n7k3q.pages.dev/py/tgt.py` |
| 28 | TG 媒体库 · 24 频道 | `https://tgl.qq33200.dpdns.org/tvbox/vod` |

### 8.3 站点行为：404 会伪装成 200

`uzsub-n7k3q.pages.dev` 对**任意不存在的路径**返回：

```
HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
<!doctype html>
<title>静态订阅</title>   ← 8,579 B 的首页
```

实测 `https://uzsub-n7k3q.pages.dev/py/pikpak.py` 返回的就是这个 HTML，**该文件并不存在**；
站点上真正存在的 PikPak 源只有 `py81/pikpak.py`（97,054 B）。

影响：订阅里写错路径时，壳拿到的是 HTML 而不是 Python，报错信息会指向语法/加载异常，
很容易被误判成「源本身有 bug」。核对源时先看返回的 `Content-Type`。

### 8.4 文件归档

53 个本地文件已打包为本地归档（**未推送公网仓库**）：

```
D:/workbuddy/影视聚合/_inventory/local-py-sources.zip   553.0 KB（53 个文件 + MANIFEST.csv）
D:/workbuddy/影视聚合/_inventory/local-py-sources.csv   11,544 B（81 行清单，含 md5）
```

未推公网仓库的原因：仓库 `qq33200/winbox-apk` 是 **public**，而这 53 份是第三方站点爬虫代码，
不宜代他人分发。需要的话可以另开私有仓库或直接传压缩包。
