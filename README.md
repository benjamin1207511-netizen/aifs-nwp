# BAMC AIFS 华南预报

以 **ECMWF AIFS（AI 预报系统）Single 0.25°** 预报图为主的轻量网站：16 类产品，
每天 **00 / 12Z** 两个起报场，从 +24h 到 +240h 每 6 小时一帧，共 37 帧。

- 数据来自 **ECMWF Open Data**（GRIB2 直连），**不经过 Open-Meteo 或任何第三方 API**
- 华南区域：**16–28°N、105–121°E**（原生 0.25° 网格）
- 综合图与 500hPa 风场另有东亚版：**0–50°N、80–140°E**
- 纯 Python 标准库后端，无数据库；图像为预生成 PNG，更新后热切换，站点无需重启
- 内置反爬（会话 Cookie、限速、UA 过滤、扫描器拦截、IP 黑名单、图片防盗链）
- 内置自动更新器：只使用已经补齐到 +240h 的完整场次，半截场次自动跳过

界面、产品分类、色阶与地图风格与 `numerical-prediction`（ECMWF IFS / Open-Meteo 版）
保持一致，区别只在**数据源**和由此带来的诊断量计算方式。

## 快速开始

```bash
python server.py
```

浏览器打开 `http://127.0.0.1:8788`。Windows 也可双击 `start.bat`。

站点运行只需要 Python 标准库；`aifs/` 里是预生成的预报图。生成/更新图片需要：

```bash
python -m pip install numpy matplotlib cartopy eccodes
```

> `eccodes` 是 ECMWF 官方的 GRIB 解码库（`pip install eccodes` 自带二进制）。
> 首次出图时 cartopy 会自动下载 Natural Earth 的 10m 地图数据（约 20 MB）。

## 产品

| ID | 名称 |
| --- | --- |
| `aifs-southchina` | 华南 · 降雨 + 500hPa 高度 + 850hPa 风 |
| `aifs-southchina-ea` | 东亚 · 降雨 + 500hPa 高度 + 850hPa 风 |
| `aifs-t2m` | 2m 气温 |
| `aifs-h500` | 华南 · 500hPa 风场 |
| `aifs-h500-ea` | 东亚 · 500hPa 风场 |
| `aifs-rain24` | 24h 降水 |
| `aifs-mslp` | 海平面气压 |
| `aifs-wind10` | 地面风场 |
| `aifs-wind925` | 925hPa 风场 |
| `aifs-gust24` | 24h 阵风（按 10m 风 ×1.5 估算） |
| `aifs-cape` | CAPE（由温湿廓线计算） |
| `aifs-kindex` | K 指数 |
| `aifs-pwat` | PWAT |
| `aifs-rh2m` | 2m 相对湿度 |
| `aifs-rh500` | 500hPa 相对湿度 |
| `aifs-cloud` | 总云量（成） |

### 图像样式约定

- 时次：+24h 至 +240h，每 6 小时一帧，共 37 帧；只用 00Z / 12Z 且补齐到 +240h 的起报场。
- 空间精度：直接用 AIFS 原生 0.25° 网格绘图（不需要插值）。
- 综合图：588 dagpm 位势高度线用红色粗线描出，"588" 标签在红线之上，没有该线时不画；
  850hPa 风速 ≥ 24.5 m/s 的风矢标红，其余蓝色。
- 24h 阵风、地面风场：15 级风速色阶（0.3–56.1 m/s），>56.1 m/s 红色，<0.3 m/s 白色。
- 总云量：4–9 成蓝色渐变，<4 成白色，>9 成深蓝。
- 风矢取样：华南 0.5°、东亚 1.5°；风羽按 m/s 计（半羽 2.5、整羽 5、旗 25）。
- 广东省界用黄色描边。
- 顶栏本地时间在电脑端和手机端都显示。

## 数据来源与下载方式

ECMWF 把 AIFS 的 GRIB2 按“时次 + 变量”切片放在 Open Data 上：

```text
https://data.ecmwf.int/forecasts/{yyyymmdd}/{hh}z/aifs-single/0p25/oper/
    {yyyymmddHHMMSS}-{step}h-oper-fc.index      # 每个变量在文件里的字节偏移
    {yyyymmddHHMMSS}-{step}h-oper-fc.grib2      # 该时次全部变量（约 57 MB）
```

`aifs_chart.py` 的做法：

1. 先取 `.index`（约 30 KB），列出该时次所有变量及其 `_offset` / `_length`；
2. 按需要的 32 个场，用 HTTP `Range` 一次请求多段字节，只下约 **12 MB/时次**；
3. `eccodes` 解码 GRIB2，裁出 **79.5–140.5°E、-0.5–50.5°N** 的公共窗口；
4. 诊断出各产品需要的二维场，缓存成 `_cache/steps/*.npz`（重跑不重复下载）；
5. 多进程渲染 16 类 × 37 帧。

一场完整更新约 **500 MB 下载、10–20 分钟**（4 路并发下载 + 6 进程渲染）。
AIFS 的 GRIB2 用 CCSDS 压缩、每值仅 9 位，出图前会做一次 σ≈25 km 的高斯平滑
（`AIFS_SMOOTH`，设 0 关闭），否则等值线会出现明显台阶。

## AIFS 原始场与派生量

AIFS Single 输出的是模式原始场，部分产品需要诊断，脚本里都有明确实现：

| 产品 | 来源 |
| --- | --- |
| 6h / 24h 降水 | `tp`（累积降水量，kg/m²）按时次相减 |
| 2m 气温 / 海平面气压 / 总云量 | `2t` / `msl` / `tcc` 直接使用 |
| 500hPa 高度、850/925/500hPa 风 | `gh@500`、`u/v@850/925/500` |
| PWAT | `tcw`（总柱水汽，kg/m² = mm） |
| 2m 相对湿度 | 由 `2t`、`2d` 计算 e/es |
| 500hPa 相对湿度 | 由 `t@500`、`q@500` 计算 |
| K 指数 | (T850−T500) + Td850 − (T700−Td700)，露点由 `q` 反算 |
| CAPE | 地面气块法：2m 温湿起步，干绝热到 LCL，再伪绝热积分到 150hPa，8 层环境廓线插值 |
| 24h 阵风 | AIFS 不输出阵风，用 10m 风 24h 最大值 × 1.5 估算（图上标注 est.） |

## 更新图片

```bash
python auto_update.py            # 常驻，每 30 分钟检查一次
python auto_update.py --once     # 只检查并更新一次
python auto_update.py --dry-run  # 只看有没有新场次
```

Windows 也可以：

- `start_auto_update.bat`：常驻窗口
- `install_auto_update_task.bat`：注册 Windows 计划任务（每 30 分钟静默运行）
- `uninstall_auto_update_task.bat`：卸载计划任务

更新器要点：

- 只使用已经补齐到 +240h 的完整场次，半截场次自动跳过；
- 每个时次的解码结果缓存到 `_cache/steps/`，断网后重跑不重复下载；
- 单次失败自动重试 3 次（`AIFS_BUILD_RETRIES` / `AIFS_RETRY_WAIT` 可调）；
- 图片先写入 `aifs_build/`，校验通过后整体替换，成功后清理缓存与旧图；
- 每次发布后自动删除旧场次图片，**磁盘上只保留最新一组 16×37 张**（`prune_live`）。

手动生成：

```bash
python aifs_chart.py                                  # 自动选最新完整场次
python aifs_chart.py --run 2026-11-07T00:00           # 指定场次
python aifs_chart.py --products cloud --jobs 1        # 只出云量，单进程
python aifs_chart.py --input _cache/steps --run ...   # 用已有缓存重画
```

### 加速

- 渲染默认 `min(6, CPU-1)` 个进程，可用 `--jobs N` 指定；
  多进程不可用时自动退回线程，再不行退回单进程。
- 下载默认 4 路并发，`AIFS_FETCH_WORKERS` / `--download-workers` 可调。
- 出图默认 **140 dpi**（1400×1008）。

## 反爬说明

`anti_bot.py` 已接入 `server.py`，不需要单独启动：

- 页面签发 `bamc_sid` 会话 Cookie（HMAC 签名，12 小时）；
- `/api/*` 必须带有效 Cookie；
- 每 IP 滑动窗口限速：全局 420 / HTML 60 / API 180 / 图片 360 次每 60 秒；
- 拦截 requests / curl / scrapy / puppeteer / 无头浏览器，以及 AI / SEO 爬虫 UA；
- 扫描器路径（`/wp-admin`、`/.env`、`/phpmyadmin`、`/.git` 等）直接 404；
- `blocklist.txt` 可写 IP 或 CIDR，保存 30 秒内生效；
- 图片带外站 Referer 时拒绝；
- 拦截日志写入 `anti_bot.log`（超 2 MB 轮转）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RATE_GLOBAL` / `RATE_HTML` / `RATE_API` / `RATE_IMAGE` | 420 / 60 / 180 / 360 | 每 60 秒限额 |
| `ANTIBOT_LOG` | `1` | 设为 `0` 关闭拦截日志 |
| `ANTIBOT_SECRET` | 随机 | 固定 Cookie 签名密钥（生产环境建议设置） |

## 目录结构

```text
aifs-south-china/
├─ server.py             # 网站后端
├─ anti_bot.py           # 反爬模块
├─ auto_update.py        # 自动更新器
├─ aifs_chart.py         # GRIB 下载 / 诊断 / 出图
├─ blocklist.txt         # 反爬 IP/CIDR 黑名单
├─ start.bat             # 启动网站（默认 8788 端口）
├─ start_auto_update.bat # 常驻自动更新
├─ install_auto_update_task.bat / uninstall_auto_update_task.bat
├─ run_auto_update_once.bat      # 计划任务调用的单次更新
├─ README.md
├─ public/               # 前端（index.html / style.css / app.js / robots.txt）
└─ aifs/                 # 16 类产品，每类 37 张 PNG + manifest.json
   ├─ combined/  combined-ea/  h500/  h500-ea/ …
   └─ cloud/
```

## 让局域网 / 公网访问

```bash
python server.py --host 0.0.0.0 --port 8788
```

同一 Wi-Fi 下访问 `http://你的电脑IP:8788`。公网可用 Cloudflare Tunnel：

```bash
cloudflared tunnel --url http://127.0.0.1:8788
```

用 Nginx / Cloudflare / Render 反向代理时务必设置 `TRUST_PROXY=1`，
否则所有访客会被识别成同一个 IP。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | 8788 | 监听端口 |
| `SITE_HOST` | 本地 `127.0.0.1`，有 `PORT` 时 `0.0.0.0` | 监听地址 |
| `TRUST_PROXY` | `0` | 反向代理后设为 `1` |
| `ANTIBOT_SECRET` | 随机生成 | Cookie 签名密钥 |
| `ANTIBOT_LOG` | `1` | `0` 关闭拦截日志 |
| `AIFS_BUILD_RETRIES` | `3` | 自动更新失败重试次数 |
| `AIFS_RETRY_WAIT` | `120` | 重试间隔秒数 |
| `AIFS_FETCH_WORKERS` | `4` | 下载并发数 |
| `AIFS_RENDER_MODE` | `auto` | `auto` / `process` / `thread` / `serial` |
| `AIFS_SMOOTH` | `1.0` | 出图前的高斯平滑 σ（网格数），`0` 关闭 |
| `AIFS_OPEN_DATA_BASE` | `https://data.ecmwf.int/forecasts` | 可换 AWS/Azure/GCP 镜像 |

AWS / Azure / Google 上都有 Open Data 的镜像，国内访问慢时可以把
`AIFS_OPEN_DATA_BASE` 指向 `https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com`
等地址（目录结构相同）。

## 数据与声明

- 预报数据：**ECMWF AIFS Single 0.25°**（ECMWF Open Data，CC BY 4.0）
- 图像：本地 Python 脚本生成，右下角落款 `www.bamc.top`
- 图像仅作预报参考；AIFS 为机器学习模型输出，与业务 IFS 存在差异，
  阵风、CAPE 等诊断量仅供参考
