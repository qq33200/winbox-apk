# PikPak 相关新功能同步（2026-11-21，WorkBuddy → Muse）

给 Issue #7（WinBox 移植 Android APK）的增量交接。以下改动在 11-19 ~ 11-21 完成，均已在本机验证；
Muse 更新 APK 时请按此清单移植，不要重新实现已确认的行为。

## 文件说明

- `pikpak-winbox.py`：Windows Win-Box 运行时正在用的完整源。
- `pikpak-mobile.py`：同一份源去掉 Win-Box 依赖后的手机版（单文件、无需配置文件）。
  - 头部 `# @tvbox-source`（自动加载器强制收录）
  - `MOBILE = os.name != 'nt' or PIKPAK_MOBILE=1`
  - 手机端：图片直出缩略图 URL、跳过 Win-Box 画廊、CSV 扫 `/sdcard/Download`、`TV/proxy.json` 自动读代理
- `pikpak-guide-config.json`：手机影视壳引导配置（含 PikPak 站点，`file://Download/pikpak.py`）
- `pikpak-records.csv`：84 条按分享 ID 去重的记录（标题,链接），供手机「CSV 导入」直接用

## 本次新增/修改的功能（需要进 APK）

1. **文件夹 = 节目，连播**
   - 每个文件夹（含二级/多级）都保留为独立可进入的入口，顶部加一条 `▶ 播放整个文件夹（连播）`。
   - 详情用 `_folder_detail()` 递归遍历该目录（含子目录），按自然顺序排成剧集，支持连续播放。
   - 同名文件自动带相对路径；清晰度线路沿用第一条视频的 media 名称（原画/480P 等）。

2. **文件夹链接识别**
   - `/s/<sid>/<folderid>` 链接：优先用 `/drive/v1/share/file_info` 的 `file_info.name` 取目录**自身名称**，
     不再误取成里面第一个子目录名。
   - 目录 ID 失效（404 code 5）时自动回退分享根目录，不报错。
   - 例：`.../VNTl4rQ.../VNSGIuye...` → 卡片名 `01_超强~邵氏电影合集（744部）`。

3. **带提取码链接**
   - 搜索链接正则保留 `?pwd=...` 查询串；多链接粘贴时文本里的「提取码：xxxx」会补给缺 pwd 的链接。
   - 两种写法都验证过：`...?pwd=zekk`、`... 提取码：zekk`。

4. **CSV 导入**
   - 新分类「CSV 导入」：`📂 重新导入 CSV` / `🗑 清空导入`。
   - 电脑端：主进程文件对话框；手机端：直接扫 Download 下的 `.csv/.txt`。
   - 导入后对没有标题的条目并发补 PikPak 真实片名并写回。
   - 导出的 CSV 按分享 ID 去重（同一分享的不同子目录链接只留一条，保留路径最短者）。

5. **解析记录管理**
   - 「外部解析」主列表干净；「🗑 管理解析记录（单条删除 / 全选）」二级页里做单条或全选删除。
   - 上限 30 → 120；补名批量 12 → 60（8 并发 / 10s 预算）。
   - 已从 App 日志恢复 36 条丢失链接，合计 76 条。

6. **播放链路**
   - 媒体代理（drpyS 5575）实测有时比直连慢一半，**默认关闭**；需要时 ext 里 `media_proxy: true`。
   - 播放地址默认直接返回 PikPak 直链。

7. **图片**
   - 电脑端：内置画廊窗口，大图 + 左右箭头 + 缩略图条 + 键盘 ←/→/Esc；用 PikPak 缩略图直链（download 直链 403）。
   - 手机端：无画廊窗口，详情直出缩略图 URL。

## 明确已剔除（不要移植回 APK）

- 「资源站」分类 and `/share` 路由：Cloudflare 只放行真人浏览器，自动化必然被拦，已全部删除。
- Win-Box 托盘功能：已删除。
- `/galleryopen` `/import-csv` 这两个本机接口是 Win-Box 专属，APK 里请以 Flutter 原生 UI 实现同等能力。

## 验证结果

- 本地 FongMi PY 加载模拟环境：`mobile/test/mock_fongmi.py`（本包未附，可找 WorkBuddy 索取），**11/11 PASS**：
  PY 加载、分类、单/多链接搜索、目录浏览、连播入口、详情剧集、播放直链、CSV 扫描、最近解析、管理页。
- 手机模式 `init` 0.01s / `search` 0.57s（代理探测结果缓存 6h）。
- Win-Box 已重启并回归通过；CSV 84 行 = 84 个唯一分享 ID。
- 未做真机 APK 验证（本机 VT-x 关闭，无法跑模拟器），真机部分请在 APK 构建后人工验收。

## 给 Muse 的具体动作

1. 以本包的 `pikpak-mobile.py` 为参照，把上述 1~7 项落进 APK 的 PikPak 源/UI；不要照抄 Win-Box 专属的 9978 本机接口调用。
2. `pikpak-records.csv` 是用户自己整理并提供的链接样例，可作为 APK 的可导入数据参考。
3. 完成后在 Issue #7 评论里给出 APK 下载链接、大小、构建时间，以及「Win-Box 功能 → APK 状态」兼容性矩阵；标签按协议改 `status:review`。
