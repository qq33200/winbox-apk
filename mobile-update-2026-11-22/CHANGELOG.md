# PikPak 手机端图片兼容修复（2026-11-22，WorkBuddy → Muse）

给 Issue #7（WinBox 移植 Android APK）的增量交接。
本次只改 `pikpak.py` 一个文件，**不含壳改动**，替换后重新打包即可。

## 文件说明

- `pikpak.py`：手机 / 桌面通用的完整源，约 124KB，`# @tvbox-source`。
  直接覆盖 APK 里的 `assets/python_sources/pikpak.py`（Muse 的 `py_spider.dart` 走 `asset://` 加载）。
- 相对 APK 里现在那份，本文件额外包含三部分：**手机端图片兼容修复**、
  **单链自动下钻**（APK 那份还没有）、**`pikpak-config.json` 空值清空代理的修复**。

## 一、手机端图片打不开：根因

两个原因叠加，缺一不可。

### 1. 壳侧主动拦截（`lib/ui/pages/player_page.dart`）

```dart
// 源站把海报 / 截图当播放地址返回：fvp/libmdk 在图像解码路径上可能整进程闪退，
// 打开前直接拦下并提示换线路。
if (target.playable && looksLikeImageMediaUrl(target.url)) {
  _error = '该线路返回的是图片地址（源站解析异常），请改用其他线路';
```

所以只要源的播放线路返回图片地址，一定播不了，而且会被当成「源站解析异常」。
这条**不要放宽**——注释里写的闪退风险是真的（libmdk 解码图片）。

### 2. 源侧

APK 里那份 `playerContent` 只有 `s. / d. / v. / f.` 四个分支，没有 `i.`，
图片条目点播放会掉到最后的「无法识别的 PikPak 播放标识」。
同时 `_share_image_detail()` 又把同目录图片做成了「图片」播放线路（地址是缩略图 URL），
正好撞上壳的拦截。

## 二、源侧改动清单（都在本次的 pikpak.py 里）

1. **图片详情页不再给播放线路**
   `_share_image_detail()` 返回 `vod_play_from = ''` / `vod_play_url = ''`，
   封面用 `thumbnail_link`，文案提示「手机上点上方海报即可放大查看（支持双指缩放）」，
   并附一条可长按复制的直链。
   - 依据：MisaTV 详情页简介是 `SelectableText`，可选中复制；海报点击走 `_openCover()`
     的 `InteractiveViewer`（maxScale 4），能看全图。
   - 缩略图实测：`https://sg-thumbnail-drive.mypikpak.com/v0/thumbnails/<hash>/720/2048`
     无 UA、无 Referer、无鉴权，直接 `200 image/jpeg`，约 70KB。壳的 `Image.network` 能加载。

2. **`playerContent` 加图片兜底**
   `i.` 前缀，或 id 本身是图片 URL（按扩展名判断）时，返回
   `{'parse': 0, 'url': '', 'msg': '这是图片，不是视频：回到详情页点上方海报即可放大查看（可双指缩放）'}`。
   不让播放器去解图片，也不再出现「无法识别」。

3. **图片卡片备注**改成「图片 · 点开看大图」（桌面版是「图片 · 画廊」）。

4. **不再用原图当封面**
   删掉了 `_share_medias()` 取 `web_content_link` 的逻辑——原图要鉴权，直接 GET 403，
   拿它当封面只会得到空白。统一用缩略图。

5. **修掉一个会清空代理的真 bug（APK 也受影响）**

   ```python
   if isinstance(obj, dict):
       ext = dict(ext)
       ext.update(obj)     # ← 模板里 "proxy": "" 会把源里配好的代理覆盖成空串
   ```

   同目录若存在 `pikpak-config.json`（模板默认写 `"proxy": ""`），
   ext 里配好的代理会被清掉，手机上表现为「PikPak 分享功能在当前地区不可用」。
   本机已复现：`proxy=''` → `share_status=PROHIBITED`。
   改为**空值（`'' / None / [] / {}`）不覆盖**。如果打包时 assets 旁带了这个模板文件，
   这条必须带上。

6. **合并 `_heal_stored_passwords()`**
   旧记录链接里没 `pwd=` 但标题写着「提取码：xxxx」时，自动补到链接并写回 state，不用重新导入。

## 三、顺带带入：单链自动下钻（APK 那份还没有）

规则：打开分享时，当前目录**只有一个文件夹、且没有其它文件**就直接进去（不判断名字），
最多 5 层；多文件夹或目录里已有视频则停下。

- `_share_dive()` 收敛了「列表 → 判断 → 再列表」，`_share_browse()` 不再自己写循环。
- `_remember()` 记 `dir` / `dn`，「最近解析」和「CSV 导入」的卡片带 `fid=dir`，下次点开直达最深层。
- `_prefill_recent_dirs()` 在启动 12s 后后台补齐老记录，带跨进程锁
  （`pikpak-prefill.lock`，15 分钟），避免常驻池多个 Python 进程重复打接口。
- 本机实跑：78 条历史 → 68 条补上直达目录、6 条确认无需下钻、4 条需提取码/已删除；CSV 17 条全补。

## 四、验证

`mobile/test/mock_fongmi.py`（本地 PY 加载回归）**20/20 通过**，其中新增 5 项图片用例：
图片封面存在、详情页无播放线路、提示点海报、误点播放有明确提示、视频标识不误伤。

真机链路（`PIKPAK_MOBILE=1` + 真实分享，116 张图的目录）：

| 环节 | 实测 |
|---|---|
| 目录浏览 | 3 页 80 项，图片卡 20 项，备注「图片 · 点开看大图」 |
| 图片详情 | 播放线路 0 条，封面为真实缩略图，文案含「同目录共 116 张图片」 |
| 误点播放 | 返回「这是图片，不是视频…」，非「无法识别」 |
| 缩略图 | 200 `image/jpeg`，70KB |

## 五、需要 Muse 做的

1. **必做**：用本目录的 `pikpak.py` 覆盖 `assets/python_sources/pikpak.py`，重新打包。
   壳侧不用改一行。
2. **可选**：做「图集页」，让手机端能连续翻看同目录图片。规格见下。
   这个不做也不影响看图，只是要多点两下（详情页 → 点海报）。

## 六、图集页规格（如果安排）

现状：手机端只能一张一张看，且必须先点进详情页、再点海报。同目录 100+ 张图时体验很差。

建议改动（很小）：

- **触发**：详情页发现 `vod_play_from` 含「图片」这一路，不进播放器，改开图集页。
- **页面**：`PageView` + 每页 `InteractiveViewer`（复用 `_openCover` 那套），
  底部显示「3 / 116」和文件名，支持左右滑、双指缩放。
- **数据**：直接用 `vod_play_url` 里这一路的剧集，格式 `名字$缩略图地址`，不需要新字段。
- **源侧配合**：壳改好后告我一声，我在 `_share_image_detail()` 里把同目录图片填进「图片」线路
  （现在**故意没填**——先埋线路会让用户点到一个必然报错的入口，比现在更差）。

## 七、其他

- 桌面版 Win-Box 运行时已同步（图片兜底、空 config 不覆盖代理、单链下钻），不影响 APK。
- 手机懒人包（`pikpak-懒人包.zip`）已按本文件重打。
- 本次未改动 `magnet.py`、`winbox_spider.py`、搜索范围过滤等 M1/M2 逻辑。
