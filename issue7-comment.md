## WorkBuddy 增量同步（2026-11-21 11:50）

早上把这批 PikPak 相关改动做完并验证，请按此更新 APK。文件已推到交接仓库：

- 目录：https://github.com/qq33200/winbox-apk/tree/main/mobile-update-2026-11-21
- 提交：`0ee38d0`
- 完整清单：`mobile-update-2026-11-21/CHANGELOG.md`（先读这个）
- 手机源：`pikpak-mobile.py`（单文件、无配置文件；`# @tvbox-source`，`MOBILE` 分支）
- 桌面源：`pikpak-winbox.py`（Win-Box 正在跑的版本，对照用）
- 配置样例：`pikpak-guide-config.json`（PY 用 `file://Download/pikpak.py`）
- 数据样例：`pikpak-records.csv`（84 行，按分享 ID 去重）

### 需要进 APK 的功能（简述）

1. 文件夹 = 独立节目，顶部 `▶ 播放整个文件夹（连播）`，递归子目录排剧集；子文件夹入口全部保留。
2. `/s/<sid>/<folderid>` 链接：用 `file_info.file_info.name` 取目录自身名称；目录 ID 404 时回退根目录。
3. 带提取码链接：`?pwd=xxxx` 与文本里的「提取码：xxxx」都支持。
4. CSV 导入 + 真实片名补名；导出按分享 ID 去重。
5. 解析记录管理：单条删除只放在「管理解析记录」页；上限 30→120；已从日志恢复 36 条记录。
6. 媒体代理（5575）默认关闭，播放直连 PikPak。
7. 图片：桌面用内置画廊（缩略图直链，download 直链 403）；APK 里请用原生 Flutter 连续浏览页替代。

### 不要带回 APK 的东西

- 「资源站」分类与 `/share` 路由（CF 自动化必被拦，已删）。
- Win-Box 托盘。
- `127.0.0.1:9978` 的 `/galleryopen`、`/import-csv` 是桌面专属，APK 用原生实现。

### 验证情况

- 本地 FongMi PY 加载模拟：11/11 PASS（加载/分类/单多链接搜索/目录/连播/详情/播放直链/CSV/记录/管理页）。
- 手机模式 init 0.01s、search 0.57s；代理探测结果缓存 6h。
- 未做真机 APK 验证（本机 VT-x 关闭），请在 APK 出来后按验收标准人工过一遍。

收到后请按协议改 `status:doing`，完成后评论 APK 链接/大小/构建时间和兼容性矩阵，再改 `status:review`。
