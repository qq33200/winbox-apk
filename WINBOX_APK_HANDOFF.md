# WinBox → Android APK · 开发交接说明

> 交接日期：2026-11-20
> 交接方：WorkBuddy（本地资产整理）
> 接收方：Muse
> 任务状态：待 Muse 接手（GitHub 任务池 Issue 见 qq33200/muse-workbuddy-pool）

---

## 0. 一句话结论

用户明确要求“把 WinBox 做成 APK”，此前“用现成 FongMi 壳 + py 懒人包替代自研 APK”的结论已被本次要求取代。

**推荐路线：直接基于上游作者自己的 Flutter 版 MisaTV 打 Android 包，再把我们在 Win-Box 上做的补丁/源适配逐项移植进去。**

理由：
- [Win-box](https://github.com/yagami-hue/Win-box)（MIT）是 Electron 桌面应用，只有 Windows 源，`src/engine`、`src/main`、`src/renderer`、`src/shared` 是完整 TypeScript 源码；
- 同一作者已用 Flutter 重写并维护 successor：[MisaTV](https://github.com/yagami-hue/MisaTV)（GPL-3.0，最近推送 2026-11-19），README 明确写“Android（可构建，未做重点调优）”，仓库已有完整 `android/` 工程和 `.github/workflows/build-android.yml`，能产出 arm64 release APK；
- 业务规则（TVBox/CatVod 订阅、jar/js/py 三类蜘蛛、网盘、磁力、弹幕等）在两条线里保持一致，不需要从零重写播放器与爬虫引擎。

**不推荐路线**：改 Electron `app.asar` 再套 Android WebView，或在 Windows 上用打包器转换 exe；这等于重做壳且拿不到原生能力。

---

## 1. Win-Box 本体情况

| 项 | 值 |
|---|---|
| 上游 | https://github.com/yagami-hue/Win-box （MIT，TypeScript，Electron） |
| 本机安装 | `D:/TVbox/Win-Box/Win-Box.exe`，版本 1.22.1 |
| 用户数据 | `D:/TVbox/Win-Box/data/`（订阅、py 缓存、插件、凭据等） |
| 打包资源 | `D:/TVbox/Win-Box/resources/app.asar`（约 72 MB） |
| 解包只读副本 | `D:/workbuddy/_winbox-asar-inspect/`（`dist/main.cjs`、renderer JS/CSS、unpacked 资源） |
| 播放内核 | mpv 0.41.0（命名管道）、HTML5 备用；ffmpeg 8.0、aria2 |
| 蜘蛛运行时 | 嵌入式 CPython 3.11.6 + `runner.py` + `base/spider.py`（位于 `data/winbox-cache/python/3.11.6/`），JVM/d2j、node:vm JS |
| 本机中继端口 | `127.0.0.1:9978`（播放/图片/导入等），`127.0.0.1:5575`（drpyS 媒体代理） |

Win-Box 的 `package.json` 自述：“Windows 桌面移植版 — 导入源规则与安卓版保持一致”。

---

## 2. 我们在 Win-Box 上做过的事（完整资产清单）

### 2.1 已进 asar 的补丁

| # | 功能 | 脚本 / 文件 | 状态 |
|---|---|---|---|
| A | 修复 py 源下载丢 `tk=` 鉴权参数导致缓存 28 字节拒绝页 | `D:/workbuddy/WIN-TVBOX/work/winbox_patch.py`（命令 `patch/status/launch/watch/restore`） | 已生效；改 `dist/main.cjs`，新增 `looksLikePySource` 判断、下载用完整 api、缓存有效性校验；守护每 5~10 s 自愈，App 更新后会重打 |
| B | 横封面 16:9 + 卡片 200px | `D:/workbuddy/WIN-TVBOX/work/winbox_landscape.py` | 已生效（CSS 里 `16/9` 7 处、`minmax(200px,1fr)` 1 处，`2/3` 0 处） |
| D1 | PikPak 搜索范围过滤 + 搜索结果横幅 | `D:/workbuddy/影视聚合/winbox-pikpak/patch-search-scope.py` | 已生效；改 `searchAll()` 里的 `pikpakLink` 判断和 renderer 横幅 |
| D2 | 本地 `.py` / `.js` 源导入与 `file://` 加载 | `D:/workbuddy/影视聚合/winbox-pikpak/patch-winbox-js.py` | 已生效；改 `extensions:["py","js"]` 和 `js-worker.cjs` 的 `doLoad` |
| D3 | `/share` 分享站路由 + 启动 helper | `D:/workbuddy/影视聚合/winbox-pikpak/patch-winbox-share.py`、`share-helper/` | **已删除/弃用**（11-18），不要移植 |
| — | 当前 asar | `D:/TVbox/Win-Box/resources/app.asar`，72,224,310 B，MD5 `ce95ae035800aa34ea71db99e6777ef3` | 上述 A+B+D1+D2 叠加态；`winbox_patch.log` 最后一次自动重打 2026-11-19 09:46 |

备份：`D:/workbuddy/WIN-TVBOX/work/app.asar.orig`、`app.asar.bak.20261116-landscape`、`D:/workbuddy/影视聚合/_backup-winbox-app/app.asar.*`（10 份）、`D:/TVbox/Win-Box/resources/app.asar.bak-*`（约 30 份）。
注意：`work/` 下 `app.patched.asar`、`test*.asar`、`app.asar.1221.orig` 是 0 字节空壳，无内容。

### 2.2 外部常驻服务 / 插件

| # | 功能 | 目录与关键文件 | 依赖 / 端口 | 当前状态 |
|---|---|---|---|---|
| B2 | 封面自适应：完整竖海报居中 + 两侧模糊，640×360，不裁内容 | `D:/workbuddy/WIN-TVBOX/winbox-auto-crop/crop_service.py`、`patch_runner.py`、`runner.py.bak.20261116`、`README-使用方法.md` | PIL；服务 `0.0.0.0:9917`；挂钩 `runner.py` 的 `_serialize` / `_serialize_raw` 改写 `vod_pic`，URL 带 `?v=2` | 钩子已被 App 更新覆盖（`AUTO_CROP_BEGIN` 计数 0），服务当前不参与；LAN IP 变化需同步 `CROP_BASE` |
| C | 给猫爪/AinanPlayer 补 Python 引擎 | `D:/workbuddy/WIN-TVBOX/py-runtime/py_gateway.py`、`py_patch.py`、`python/`（67 MB CPython 3.11） | 网关 `127.0.0.1:9977`；VBS 常驻 | 已于 10-24 还原，未启用 |
| D | PikPak 分享链接解析 / 网盘 / TG / 表格 / CSV 源 | `D:/workbuddy/影视聚合/winbox-pikpak/pikpak.py`（97 KB，纯标准库）、`影视.json`、`test-pikpak.py` + 上面 D1/D2 两个补丁 | 大陆访问 PikPak 需代理；Win-Box 自带 9978/5575 | 已装进 Win-Box `data/imports/py/` |
| E | 磁力 / `.torrent` 解析，交内置 BT 引擎边下边播 | `D:/workbuddy/影视聚合/winbox-magnet/magnet.py`（17 KB） | infoHash=SHA1(info bencode) → magnet；无额外端口 | 已装进 `data/imports/py/` |
| F | Cloudflare 后暴露 share.mepikpak.com 列表 | `D:/workbuddy/影视聚合/share-helper/share-helper.js`、`patch-winbox-helper.py` | Node + playwright-core + 本机 Chrome，`127.0.0.1:8788` | **弃用，可删除**；站点改版后路由已移除 |
| G | 实时语音转中文字幕 | `D:/workbuddy/2026-11-11-winbox-asr-subtitle/winbox_live_sub.py`（91 KB）、`winbox_guard.py`、`proc_loopback.py`、`asr_to_srt.py`、`bcl_subtitle.py`、`plugin/ai-subtitle/`、`dist/ai-subtitle.winbox`、`models/`、`_asr_env/` | sherpa-onnx + SenseVoice-Small int8（228 MB）+ Silero VAD；读 mpv 命名管道 → ffmpeg 抽音 → SRT → `sub-add/sub-reload`；翻译 Key 由环境变量/本地文件注入 | 本机已跑通；插件沙箱无音频能力，必须外部服务 + 插件入口；Android 端需重新设计（建议 P2） |
| H | 各站点 py 源修复 | `D:/workbuddy/2026-11-06-winbox-bluray/`、`2026-11-07-winbox-{4hu,chigua,uuvideo,yingtan4k,pornavhd}/` 下 `fixed-*.py`、`apply.py`、`*-修复报告.md` | 部分需代理 / CDP Chrome 诊断 | 已一次性部署到 `data/imports/py/`；无 asar 改动 |
| I | 体检脚本 | `D:/workbuddy/WIN-TVBOX/py_source_check.py`、`D:/workbuddy/winbox-tools/health/` | 纯脚本 | 按需运行 |

### 2.3 手机侧已有资源

- `D:/workbuddy/影视聚合/mobile/pikpak.py`（101 KB，纯标准库）：手机端 PikPak py 源，带 `_auto_proxy()`（内置常量 → `/sdcard/TV/proxy.json` → 常见本地端口 → 直连）。
- `D:/workbuddy/影视聚合/mobile/智能接口（引导）-含PikPak.json`、`pikpak-config.json`、`pikpak-懒人包.zip`。
- 最终懒人包形态：py 放 `/sdcard/Download/`，站点 `api: file://Download/pikpak.py`。
- `D:/workbuddy/2026-11-11-winbox-asr-subtitle/_poc/apk_list.py`、`apk_pick.py`：用 HTTP Range 只读 APK ZIP 中央目录，判断 `assets/chaquopy/build.json` 是否带 Python 运行时的方法。曾经静态分析用户手机壳为 FongMi + Chaquopy Python 3.10（支持 `file://`、getSpider、SourceFileLoader、本机 9978）。**APK 本体未保留，无 adb/模拟器。**

---

## 3. 移植到 APK 的功能映射（给 Muse 的实施建议）

| 优先级 | Win-Box 现状 | MisaTV / Android 对应做法 |
|---|---|---|
| P0 | 工程骨架 | Fork `yagami-hue/MisaTV`（GPL-3.0），基于 `android/` + `.github/workflows/build-android.yml` 产出 arm64 release APK；先跑通原样构建，再改功能 |
| P0 | py 源兼容 | MisaTV 已支持本地 `.py` 蜘蛛和按需下载嵌入式 Python 3.11；验证 `pikpak.py` / `magnet.py` 在 Android 运行时可直接加载，`file:///sdcard/Download/...` 路径可用 |
| P0 | PikPak 分享解析 | 把 `winbox-pikpak/pikpak.py` 与 `mobile/pikpak.py` 合流为一份，适配 Android 网络/存储权限与代理设置；搜索范围过滤逻辑参考 `patch-search-scope.py` 的 `pikpakLink` 判断 |
| P0 | 横封面 16:9 | 在 Flutter 渲染层实现（对应 `winbox_landscape.py` 的 CSS 效果）：完整海报居中、两侧模糊、卡片宽度约 200px；不要复用 CSS 字符串 |
| P1 | py 下载保 tk | 核对 MisaTV 自身 py 抓取是否保留订阅 URL 的完整 query；若仍有 Windows 版同款 bug，按 `winbox_patch.py` 的锚点逻辑在 Dart 侧修 |
| P1 | 磁力解析 | 移植 `magnet.py` 的 infoHash 计算与 magnet 拼装；MisaTV 已有 aria2 sidecar + `/bt` Range 取流，确认 Android 上 aria2c 是否随包/可下载 |
| P1 | 封面自适应 | 用 Dart 图像库或原生插件实现 640×360 合成（居中完整海报 + 模糊背景），替换 Windows `crop_service.py` + 9917 端口方案；Android 不应依赖 LAN IP 和固定端口 |
| P2 | 实时字幕 | Windows 方案依赖 mpv 命名管道 + 常驻 Python + ffmpeg，Android 无 mpv IPC；需在 Flutter 播放层接音频帧或改云端 ASR，先做可行性评估再决定是否实现 |
| P2 | 站点修复 py 源 | 逐个验证 `2026-11-0{6,7}-winbox-*` 的 `fixed-*.py` 在 Android Python 运行时可用；只搬需要的，不整包复制 |
| — | 明确不移植 | `/share` 路由、share-helper（8788）、py-runtime 网关（9977）、Windows 开机自启 VBS、asar 守护脚本 |

---

## 4. 验收标准

1. 有一个 Muse 创建/维护的公开 Git 仓库（或 fork 分支），包含可复现源码与构建说明；仓库里不放任何 PAT/API Key/Cookie。
2. GitHub Actions 能成功构建 release APK（arm64），产物链接可下载；记录 APK 大小与构建时间。
3. APK 可安装到 Android 9+ arm64 设备（用户真机验收；Muse 如无设备，需在 Issue 里写明“仅构建通过，未真机验证”）。
4. 至少完成 P0 四项： Flutter 壳构建、py 源加载、PikPak 分享解析、横封面 16:9。
5. 提供一份兼容性矩阵：原 Win-Box 功能 → APK 状态（可用 / 部分可用 / 不移植），并列出已知限制。
6. 若选择 MisaTV，遵守 GPL-3.0；在 README/NOTICE 里保留上游署名与许可证说明（Win-box MIT、MisaTV GPL-3.0、TVBox/CatVod 致谢）。
7. 不主动抓取任何订阅 URL 或 py 源 URL 做验证（用户明确禁止，见第 6 节）；只做静态检查、本地测试与用户手动验收。

---

## 5. 重要坑位

- Windows 端很多能力靠“本地端口 + 常驻服务 + 自启守护”，Android 端不要照搬；尽量做成 App 内组件或可配置地址。
- embed Python 的 `_pth` 隔离导致 `import PIL` 失败，必须先注入 site-packages；MisaTV 的 Android Python 运行时若有同类问题需注意。
- MisaTV 的 release 构建跑 R8，CatVod 宿主类（`Spider`/`SpiderApi`/`SpiderHostPlugin`、OkHttp 3.12.x、Gson、Jsoup、fastjson）必须 keep；上游 workflow 已有校验步骤，改造后不要删。
- 上游 MisaTV 的 Android 构建目前用 debug 签名（build.gradle.kts 里 `signingConfig = debug`）；正式分发前需与用户确认签名策略。
- py 源里可能有代理配置/订阅 token；交接包里只放源码样例，不放任何真实凭据或用户订阅文件。

---

## 6. 用户规则（必须遵守）

- 禁止 curl / fetch 订阅接口本身，也禁止实拉 py URL 做验证；会触发门禁封号。只做本地静态取证。
- 不在 Issue、代码、README、提交记录里写任何 PAT、API Key、Cookie、订阅 token。
- 用户希望“直接开发”，不要再交付“分析完建议用现成壳”作为最终结论；可以把结论写进风险评估，但必须推进到 APK 产物或明确阻塞点。
- 内容合规：项目是通用播放工具，不内置任何资源、不提供内容服务；交接文档与代码注释保持中立技术表述。

---

## 7. 需要 Muse 在 Issue 里回答的问题

1. 选哪条路线：fork MisaTV 构建 Android / 用 Win-box TypeScript 源重写移动壳 / 其它？给理由和许可证影响。
2. 是否能直接复用上游 `.github/workflows/build-android.yml` 产出 APK？如不能，最小改动是什么？
3. P0 四项的实现顺序与预计里程碑；哪些项需要用户提供真机或账号/凭据（凭据不要在 Issue 里发）。
4. P2 语音字幕是否在本次范围内；若否，明确标注为后续 Issue。

---

## 8. 随交接上传的源码样例

仓库路径：`qq33200/winbox-apk` → `source-samples/`

| 文件 | 来源 | 用途 |
|---|---|---|
| `pikpak-mobile.py` | `影视聚合/mobile/pikpak.py` | 手机端 PikPak py 源，带代理探测 |
| `pikpak-winbox.py` | `影视聚合/winbox-pikpak/pikpak.py` | Win-Box 端 PikPak py 源（对照） |
| `magnet.py` | `影视聚合/winbox-magnet/magnet.py` | 磁力解析 |
| `winbox_patch.py` | `WIN-TVBOX/work/winbox_patch.py` | py 下载保 token 的原始补丁逻辑 |
| `winbox_landscape.py` | `WIN-TVBOX/work/winbox_landscape.py` | 横封面 CSS 的原始补丁逻辑 |
| `patch-search-scope.py` | `影视聚合/winbox-pikpak/patch-search-scope.py` | PikPak 搜索范围过滤 |
| `patch-winbox-js.py` | `影视聚合/winbox-pikpak/patch-winbox-js.py` | 本地 py/js 导入与 file:// 加载 |
| `crop_service.py` | `WIN-TVBOX/winbox-auto-crop/crop_service.py` | 封面合成服务参考 |
| `patch_runner.py` | `WIN-TVBOX/winbox-auto-crop/patch_runner.py` | runner 挂钩参考 |
| `winbox_live_sub.py` | `2026-11-11-winbox-asr-subtitle/winbox_live_sub.py` | 实时字幕服务参考（P2） |
| `winbox_guard.py` | `2026-11-11-winbox-asr-subtitle/winbox_guard.py` | 守护逻辑参考（P2，不建议照搬） |

未上传：二进制运行时、ONNX 模型、app.asar 备份、用户订阅与凭据文件。需要时由 Muse 从上游仓库/Release 获取。
