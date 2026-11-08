#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「华南 ECMWF AIFS 预报图」系列（16 类产品）：

  - combined: 500hPa 高度 + 850hPa 风 + 6 小时降雨
  - t2m:      2m 气温
  - h500:     500hPa 高度 + 500hPa 风
  - rain24:   24 小时降雨
  - mslp:     海平面气压
  - wind10:   10m 风场
  - wind925:  925hPa 风场
  - gust24:   24 小时极大阵风（由 10m 风按阵风系数估算）
  - cape:     CAPE（由 AIFS 温湿廓线计算）
  - kindex:   K 指数（由 850/700/500hPa 温湿计算）
  - pwat:     可降水量（PWAT）
  - rh2m:     2m 相对湿度
  - rh500:    500hPa 相对湿度
  - cloud:    总云量

数据来源：ECMWF Open Data 的 AIFS Single 0.25°（GRIB2），
  https://data.ecmwf.int/forecasts/{yyyymmdd}/{hh}z/aifs-single/0p25/oper/

与 open-meteo 版本的根本差别：
  1. 不经过任何第三方 API，直接读 ECMWF Open Data 的 GRIB2；
  2. 和 open-meteo 版一致，只取 00Z / 12Z 两个起报场（AIFS 其实 06Z/18Z 也有）；
  3. 只按 .index 里的字节区间下载需要的字段（HTTP Range），
     一步约 12 MB，整场约 500 MB，而不是整个 56 MB × 37 步；
  4. 拿到的是原生 0.25° 规则网格，不再需要 open-meteo 的点数据插值；
  5. AIFS 不直接输出阵风/CAPE/K 指数等，脚本用原始场诊断（见下方说明）。

用法：
  python aifs_chart.py                          # 自动选最新完整起报场
  python aifs_chart.py --run 2026-11-07T00:00   # 指定起报场
  python aifs_chart.py --products t2m,h500      # 只更新部分产品
  python aifs_chart.py --input npz目录 --run ...  # 用已下载的数据重画

依赖（仅生成时需要）：
  numpy matplotlib cartopy eccodes
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "aifs"
CACHE_DIR = BASE_DIR / "_cache" / "steps"
TZ_CN = timezone(timedelta(hours=8))
UTC = timezone.utc
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"

# ECMWF Open Data 的目录结构：
#   {base}/{yyyymmdd}/{hh}z/{model}/{resol}/{stream}/{yyyymmddHHMMSS}-{step}h-{stream}-fc.{ext}
OPEN_DATA_BASE = os.environ.get("AIFS_OPEN_DATA_BASE", "https://data.ecmwf.int/forecasts")
MODEL = "aifs-single"
RESOL = "0p25"
STREAM = "oper"

DOMAINS = {
    # 默认华南范围：16–28°N，105–121°E
    "sc": {"west": 105.0, "south": 16.0, "east": 121.0, "north": 28.0},
    # 东亚范围：0–50°N，80–140°E
    "ea": {"west": 80.0, "south": 0.0, "east": 140.0, "north": 50.0},
}
DEFAULT_DOMAIN = "sc"
# 下载时统一裁一块覆盖两个区域、再留 0.5° 余量的经纬度窗口。
BOX = {"west": 79.5, "south": -0.5, "east": 140.5, "north": 50.5}

LEADS = list(range(24, 241, 6))          # +24h … +240h，每 6 小时一帧，共 37 帧
EXTRA_STEPS = [0, 6, 12, 18]             # 算 6h/24h 降水和 24h 阵风需要的前置时次
RUN_HOURS = (0, 12)                      # 只用 00Z / 12Z 两个起报场
COMPLETE_STEP = 240                      # 有这个时次才算完整场次

# ---------------------------------------------------------------- 下载字段
# 单层场
SFC_PARAMS = ["10u", "10v", "2t", "2d", "msl", "tcc", "tp", "tcw", "sp"]
# 气压层场：层 -> 需要的变量
PL_PARAMS = {
    1000: ["t", "q"],
    925: ["t", "q", "u", "v"],
    850: ["t", "q", "u", "v"],
    700: ["t", "q"],
    600: ["t", "q"],
    500: ["t", "q", "gh", "u", "v"],
    400: ["t", "q"],
    300: ["t", "q"],
}
# 前置时次（0/6/12/18h）只用得上这些
EXTRA_PARAMS = {"sfc": ["tp", "10u", "10v"], "pl": {}}

PRODUCTS = {
    "combined": {
        "subdir": "combined",
        "prefix": "aifs_southchina_",
        "title": "500hPa Height & 850hPa Wind & 6h Rainfall",
        "kind": "combined",
        "domain": "sc",
    },
    "combined_ea": {
        "subdir": "combined-ea",
        "prefix": "aifs_southchina_ea_",
        "title": "East Asia · 500hPa Height & 850hPa Wind & 6h Rainfall",
        "kind": "combined",
        "domain": "ea",
    },
    "t2m": {
        "subdir": "t2m",
        "prefix": "aifs_t2m_",
        "title": "2m Temperature",
        "kind": "t2m",
    },
    "h500": {
        "subdir": "h500",
        "prefix": "aifs_h500_",
        "title": "Wind & Height at 500hPa",
        "kind": "h500",
        "domain": "sc",
    },
    "h500_ea": {
        "subdir": "h500-ea",
        "prefix": "aifs_h500_ea_",
        "title": "East Asia · Wind & Height at 500hPa",
        "kind": "h500",
        "domain": "ea",
    },
    "rain24": {
        "subdir": "rain24",
        "prefix": "aifs_rain24_",
        "title": "24h Precipitation (mm)",
        "kind": "rain24",
    },
    "mslp": {
        "subdir": "mslp",
        "prefix": "aifs_mslp_",
        "title": "Sea-level Pressure",
        "kind": "mslp",
    },
    "wind10": {
        "subdir": "wind10",
        "prefix": "aifs_wind10_",
        "title": "10m Wind",
        "kind": "wind10",
    },
    "wind925": {
        "subdir": "wind925",
        "prefix": "aifs_wind925_",
        "title": "925hPa Wind",
        "kind": "wind925",
    },
    "gust24": {
        "subdir": "gust24",
        "prefix": "aifs_gust24_",
        "title": "24h Max Wind Gust",
        "kind": "gust24",
        "note": "gust estimated as 1.5 × 10m wind",
    },
    "cape": {
        "subdir": "cape",
        "prefix": "aifs_cape_",
        "title": "CAPE",
        "kind": "cape",
        "note": "parcel-derived from AIFS T/q profiles",
    },
    "kindex": {
        "subdir": "kindex",
        "prefix": "aifs_kindex_",
        "title": "K Index",
        "kind": "kindex",
    },
    "pwat": {
        "subdir": "pwat",
        "prefix": "aifs_pwat_",
        "title": "PWAT",
        "kind": "pwat",
    },
    "rh2m": {
        "subdir": "rh2m",
        "prefix": "aifs_rh2m_",
        "title": "2m Relative Humidity",
        "kind": "rh2m",
    },
    "rh500": {
        "subdir": "rh500",
        "prefix": "aifs_rh500_",
        "title": "500hPa Relative Humidity",
        "kind": "rh500",
    },
    "cloud": {
        "subdir": "cloud",
        "prefix": "aifs_cloud_",
        "title": "Total Cloud Cover",
        "kind": "cloud",
    },
}
ALL_PRODUCTS = list(PRODUCTS)

RAIN_6H_LEVELS = [0.1, 1, 4, 13, 25, 60, 120, 250]
RAIN_6H_COLORS = [
    "#f4f9f0", "#b9e3ad", "#77c96d", "#36a35a",
    "#5d8bd9", "#294cb0", "#b2188f",
]
RAIN_24H_LEVELS = [0.1, 1, 10, 25, 50, 100, 250]
RAIN_24H_COLORS = ["#f7f7f7", "#b7e6b0", "#4daf4a", "#377eb8", "#00008b", "#ff00ff"]

TEMP_LEVELS = [-5, 0, 5, 10, 15, 20, 25, 27, 29, 31, 33, 35]
TEMP_COLORS = [
    "#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8", "#ffffbf",
    "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026",
]

HEIGHT_FILL_LEVELS = [5760, 5800, 5840, 5880, 5920, 5960]
HEIGHT_FILL_COLORS = ["#d9f0a3", "#ffffbf", "#fee391", "#fec44f", "#fe9929"]

MSLP_LEVELS = [985, 990, 992.5, 995, 997.5, 1000, 1002.5, 1005,
               1007.5, 1010, 1012.5, 1015, 1017.5, 1020, 1022.5,
               1025, 1027.5, 1030]

# 10m 风场 / 24h 阵风统一使用 15 级风速色阶。
WIND_SPEED_LEVELS = [0.3, 3.4, 8, 10.8, 13.9, 17.2, 20.8, 24.5,
                     28.5, 32.7, 37.2, 41.5, 46.2, 51, 56.1]
WIND_SPEED_LABELS = ["0.3", "3.4", "8", "10.8", "13.9", "17.2", "20.8",
                     "24.5", "28.5", "32.7", "37.2", "41.5", "46.2", "51", "56.1"]
WIND_SPEED_COLORS = [
    "#d6e2ff", "#b5c9ff", "#8eb2ff", "#7f96ff", "#6370f7",
    "#0063ff", "#009696", "#63ff00", "#96ff00", "#c6ff33",
    "#ffff00", "#ffc600", "#ffa000", "#ff7c00",
]
WIND925_LEVELS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50]
CAPE_LEVELS = [0, 100, 250, 500, 1000, 1500, 2000, 2500, 3000, 4000, 5000]
K_LEVELS = [-10, 0, 10, 15, 20, 25, 30, 35, 40, 45, 50]
PWAT_LEVELS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 70]
RH_LEVELS = [0, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 100]

# 总云量：0–100% 映射到 0–10 成，色阶与 open-meteo 版一致（4–9 成蓝色渐变）。
CLOUD_LEVELS = [40, 50, 60, 70, 80, 90]
CLOUD_LABELS = ["4", "5", "6", "7", "8", "9"]
CLOUD_COLORS = ["#EAFCFC", "#BAF6FC", "#96EAFC", "#78D2FC", "#54AEFC"]
CLOUD_OVER = "#3684FC"

# AIFS 没有阵风产品，这里用 10m 风乘一个阵风系数估算（强对流下取 1.5）。
GUST_FACTOR = 1.5

# AIFS 的 GRIB2 用 CCSDS 压缩、每值只有 9 位，2m 气温这类场的步长约 0.25K，
# 直接等值线会出现明显台阶。出图前做一次很轻的高斯平滑（σ≈25km），
# 只影响作图，不影响任何诊断量的计算。设 AIFS_SMOOTH=0 可关闭。
SMOOTH_SIGMA = float(os.environ.get("AIFS_SMOOTH", "1.0"))


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- HTTP
_SSL_LOCK = threading.Lock()
_SSL_CTX = None


def _ssl_context() -> ssl.SSLContext:
    """优先用 certifi 的根证书；没有 certifi 时用系统默认（Windows 走系统证书库）。"""
    global _SSL_CTX
    with _SSL_LOCK:
        if _SSL_CTX is None:
            try:
                import certifi

                _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
            except Exception:  # noqa: BLE001
                _SSL_CTX = ssl.create_default_context()
        return _SSL_CTX


def http_get(url: str, rng: list[tuple[int, int]] | None = None,
             timeout: int = 180, retries: int = 4) -> bytes:
    """GET，可带多段 Range；带退避重试。"""
    last: Exception | None = None
    for attempt in range(retries):
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if rng:
            headers["Range"] = "bytes=" + ",".join("%d-%d" % (a, b) for a, b in rng)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 404):
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(30, 3 * (attempt + 1)))
    raise RuntimeError("下载失败（%d 次）：%s：%s" % (retries, url, last))


def http_exists(url: str, timeout: int = 40) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 405:  # 有些对象存储不支持 HEAD
            try:
                http_get(url, timeout=timeout, retries=1)
                return True
            except Exception:  # noqa: BLE001
                return False
        return False
    except Exception:  # noqa: BLE001
        return False


def _run_prefix(run_dt: datetime) -> str:
    return "%s/%sz/%s/%s/%s" % (
        run_dt.strftime("%Y%m%d"), run_dt.strftime("%H"), MODEL, RESOL, STREAM,
    )


def _step_base_url(run_dt: datetime, step: int) -> str:
    stamp = run_dt.strftime("%Y%m%d%H%M%S")
    return "%s/%s/%s-%dh-%s-fc" % (
        OPEN_DATA_BASE, _run_prefix(run_dt), stamp, step, STREAM,
    )


def fetch_index(run_dt: datetime, step: int) -> list[dict]:
    raw = http_get(_step_base_url(run_dt, step) + ".index", timeout=90)
    records = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def run_state(run_dt: datetime) -> str:
    """missing（还没出）/ partial（还没到 +240h）/ full（到 +240h）。"""
    if not http_exists(_step_base_url(run_dt, COMPLETE_STEP) + ".index"):
        return "missing"
    if not http_exists(_step_base_url(run_dt, 0) + ".index"):
        return "partial"
    return "full"


def latest_run(now: datetime | None = None) -> str:
    """选最新且已经更新到 +240h 的 00Z / 12Z 起报场。

    AIFS 在 06Z / 18Z 也有场次，但本站和 open-meteo 版一致，只取 00Z / 12Z
    两个主时次；+240h 没补齐的场次跳过，避免出大量空白尾帧。
    """
    now = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    anchor = now.replace(hour=12) if now.hour >= 12 else now.replace(hour=0)
    fallback = ""
    for offset in range(0, 120, 12):
        candidate = anchor - timedelta(hours=offset)
        if candidate.hour not in RUN_HOURS:
            continue
        label = candidate.strftime("%Y-%m-%dT%H:%M")
        log("检查 AIFS 起报场 %s" % label)
        state = run_state(candidate)
        if state == "full":
            log("使用起报场 %s" % label)
            return label
        if state == "partial":
            log("起报场 %s 尚未更新到 +%dh，跳过" % (label, COMPLETE_STEP))
            if not fallback:
                fallback = label
    if fallback:
        log("没有完整场次，回退到最新可用起报场 %s（尾部时次可能偏短）" % fallback)
        return fallback
    raise RuntimeError("没有找到可用的 AIFS 起报场")


# ---------------------------------------------------------------- GRIB 解包
GRID_LOCK = threading.Lock()
GRID_CACHE: dict = {}
LAST_GRID: dict | None = None


def _grid_geometry(gid) -> dict:
    ni = int(eccodes_get(gid, "Ni"))
    nj = int(eccodes_get(gid, "Nj"))
    lat1 = float(eccodes_get(gid, "latitudeOfFirstGridPointInDegrees"))
    lat2 = float(eccodes_get(gid, "latitudeOfLastGridPointInDegrees"))
    lon1 = float(eccodes_get(gid, "longitudeOfFirstGridPointInDegrees"))
    lon2 = float(eccodes_get(gid, "longitudeOfLastGridPointInDegrees"))
    return {"ni": ni, "nj": nj, "lat1": lat1, "lat2": lat2, "lon1": lon1, "lon2": lon2}


def eccodes_get(gid, key):
    import eccodes

    return eccodes.codes_get(gid, key)


def _grid_info(gid) -> dict:
    global LAST_GRID
    geo = _grid_geometry(gid)
    key = (geo["ni"], geo["nj"], geo["lat1"], geo["lon1"])
    with GRID_LOCK:
        hit = GRID_CACHE.get(key)
        if hit:
            return hit
    ni, nj = geo["ni"], geo["nj"]
    dlat = (geo["lat1"] - geo["lat2"]) / (nj - 1)
    dlon = (geo["lon2"] - geo["lon1"]) / (ni - 1)
    if dlon < 0:
        dlon += 360.0 / (ni - 1)
    lat_full = geo["lat1"] - dlat * np.arange(nj)
    lon_full = (geo["lon1"] + dlon * np.arange(ni)) % 360.0

    rows = np.where((lat_full >= BOX["south"]) & (lat_full <= BOX["north"]))[0]
    cols = np.where((lon_full >= BOX["west"]) & (lon_full <= BOX["east"]))[0]
    if not len(rows) or not len(cols):
        raise RuntimeError("GRIB 网格与目标区域没有交集")
    r0, r1 = int(rows.min()), int(rows.max()) + 1
    c0, c1 = int(cols.min()), int(cols.max()) + 1
    info = {
        "ni": ni, "nj": nj, "r0": r0, "r1": r1, "c0": c0, "c1": c1,
        "lat": np.ascontiguousarray(lat_full[r0:r1][::-1]),   # 南→北
        "lon": np.ascontiguousarray(lon_full[c0:c1]),
    }
    with GRID_LOCK:
        GRID_CACHE[key] = info
        LAST_GRID = info
    return info


def _extract_messages(blob: bytes) -> list[bytes]:
    """从（可能带 multipart 边界的）响应里切出所有 GRIB2 消息。"""
    messages = []
    pos = 0
    while True:
        start = blob.find(b"GRIB", pos)
        if start < 0:
            break
        if start + 16 > len(blob):
            break
        total = int.from_bytes(blob[start + 8:start + 16], "big")
        if total <= 0 or start + total > len(blob):
            break
        messages.append(blob[start:start + total])
        pos = start + total
    return messages


def _decode_subset(message: bytes) -> tuple[tuple[str, int], "np.ndarray"]:
    import eccodes

    gid = eccodes.codes_new_from_message(message)
    try:
        short = str(eccodes.codes_get(gid, "shortName"))
        type_of_level = str(eccodes.codes_get(gid, "typeOfLevel"))
        level = int(eccodes.codes_get(gid, "level")) if type_of_level == "isobaricInhPa" else 0
        grid = _grid_info(gid)
        values = eccodes.codes_get_values(gid)
    finally:
        eccodes.codes_release(gid)
    field = values.reshape(grid["nj"], grid["ni"])[grid["r0"]:grid["r1"], grid["c0"]:grid["c1"]]
    # GRIB 的行序通常是北→南，这里翻成南→北，和下垫面绘图顺序一致。
    field = field[::-1]
    return (short, level), np.ascontiguousarray(field, dtype=np.float32)


def _wanted_records(records: list[dict], sfc: list[str], pl: dict[int, list[str]]) -> list[dict]:
    wanted = set()
    for param in sfc:
        wanted.add((param, 0))
    for level, params in pl.items():
        for param in params:
            wanted.add((param, int(level)))
    selected = []
    for rec in records:
        param = str(rec.get("param") or "")
        levtype = str(rec.get("levtype") or "")
        if levtype == "sfc" or not rec.get("levelist"):
            key = (param, 0)
        else:
            try:
                key = (param, int(rec["levelist"]))
            except (TypeError, ValueError):
                continue
        if key in wanted:
            selected.append(rec)
    return selected


def fetch_fields(run_dt: datetime, step: int, sfc: list[str],
                 pl: dict[int, list[str]]) -> dict[tuple[str, int], "np.ndarray"]:
    """按 .index 的字节区间只下载需要的字段，返回 {(shortName, level): 2D array}。"""
    records = fetch_index(run_dt, step)
    selected = _wanted_records(records, sfc, pl)
    if not selected:
        raise RuntimeError("AIFS %s +%dh 没有匹配的字段" % (run_dt.strftime("%Y-%m-%d %H:%M"), step))
    ranges = [(int(r["_offset"]), int(r["_offset"]) + int(r["_length"]) - 1) for r in selected]
    blob = http_get(_step_base_url(run_dt, step) + ".grib2", rng=ranges, timeout=300)
    fields = {}
    for message in _extract_messages(blob):
        key, field = _decode_subset(message)
        fields[key] = field
    expected = {(p, 0) for p in sfc} | {(p, lv) for lv, ps in pl.items() for p in ps}
    missing = sorted(expected - set(fields))
    if missing:
        raise RuntimeError("AIFS +%dh 缺少字段：%s" % (step, missing))
    return fields


# ---------------------------------------------------------------- 物理诊断
RD = 287.04
CPD = 1004.64
G0 = 9.80665
EPS = 0.622
KAPPA = RD / CPD
LV0 = 2.501e6


def sat_vapor_pressure(temp_k):
    """Bolton(1980) 饱和水汽压，返回 Pa。"""
    tc = temp_k - 273.15
    return 611.2 * np.exp(17.67 * tc / (tc + 243.5))


def vapor_pressure_from_q(q, pressure_pa):
    q = np.clip(q, 0.0, 0.2)
    return np.clip(pressure_pa * q / (EPS + (1.0 - EPS) * q), 0.1, None)


def dewpoint_from_vapor(e_pa):
    ln = np.log(np.clip(e_pa, 0.1, None) / 611.2)
    return 243.5 * ln / (17.67 - ln) + 273.15


def dewpoint_from_q(q, pressure_pa):
    return dewpoint_from_vapor(vapor_pressure_from_q(q, pressure_pa))


def relative_humidity_from_q(q, pressure_pa, temp_k):
    e = vapor_pressure_from_q(q, pressure_pa)
    return np.clip(100.0 * e / sat_vapor_pressure(temp_k), 0.0, 100.0)


_ENV_LEVELS = np.asarray(sorted(PL_PARAMS.keys(), reverse=True), dtype=float)  # 1000…300 hPa


def _interp_weights(targets_hpa):
    """把 1000–300hPa 的 8 层廓线线性插值（对 ln p）到任意目标层，返回 (n, 8) 权重矩阵。"""
    levels = _ENV_LEVELS
    n_lev = len(levels)
    w = np.zeros((len(targets_hpa), n_lev))
    ln_levels = np.log(levels)
    for i, p in enumerate(targets_hpa):
        lp = math.log(max(p, 1.0))
        if lp >= ln_levels[0]:
            w[i, 0] = 1.0
        elif lp <= ln_levels[-1]:
            # 顶外：按最上两层的斜率线性外推
            left = (ln_levels[-2] - lp) / (ln_levels[-2] - ln_levels[-1])
            w[i, -1] = left
            w[i, -2] = 1.0 - left
        else:
            idx = int(np.searchsorted(-ln_levels, -lp))
            lo, hi = idx - 1, idx
            t = (lp - ln_levels[lo]) / (ln_levels[hi] - ln_levels[lo])
            w[i, lo] = 1.0 - t
            w[i, hi] = t
    return w


_P_FIXED = np.arange(1000.0, 149.9, -5.0)                       # hPa，自下而上
_W_ENV = _interp_weights(_P_FIXED)


def _moist_dtdp(temp_k, p_hpa):
    """伪绝热温度对气压的导数 dT/dp（K/hPa，负值）。"""
    es = sat_vapor_pressure(temp_k)
    p_pa = p_hpa * 100.0
    rs = EPS * es / np.maximum(p_pa - es, 1.0)
    lv = LV0 - 2300.0 * (temp_k - 273.15)
    num = RD * temp_k + lv * rs
    den = p_pa * (CPD + lv * lv * rs * EPS / (RD * temp_k * temp_k))
    return 100.0 * num / np.maximum(den, 1e-9)


def calc_cape(sp_pa, t2m_k, td2m_k, t_prof, q_prof):
    """地面气块抬升法（SB-CAPE），返回 J/kg。

    气块从 2m 温湿出发：干绝热抬到抬升凝结高度（LCL），之后按伪绝热
    过程积分到 150hPa；环境温湿廓线由 1000–300hPa 的 8 层按 ln p 线性插值。
    积分用固定 5hPa 网格，全程向量化。
    """
    shp = t2m_k.shape
    p0 = np.clip(sp_pa / 100.0, 500.0, 1050.0)
    t0 = np.asarray(t2m_k, dtype=np.float64)
    td0 = np.asarray(td2m_k, dtype=np.float64)
    e0 = np.clip(sat_vapor_pressure(td0) / 100.0, 0.1, None)   # hPa
    ln_t0 = np.log(np.maximum(t0, 150.0))
    t_lcl = 2840.0 / (3.5 * ln_t0 - np.log(e0) - 4.805) + 55.0
    t_lcl = np.clip(t_lcl, 150.0, t0)
    p_lcl = np.clip(p0 * np.power(t_lcl / t0, 1.0 / KAPPA), 100.0, p0)

    npts = int(np.prod(shp))
    p0f = p0.reshape(-1)
    t0f = t0.reshape(-1)
    t_lclf = t_lcl.reshape(-1)
    p_lclf = p_lcl.reshape(-1)

    nlev = len(_P_FIXED)
    parcel = np.full((nlev, npts), np.nan)
    env_t = (_W_ENV @ np.asarray(t_prof, dtype=np.float64).reshape(len(_ENV_LEVELS), npts))
    env_q = (_W_ENV @ np.asarray(q_prof, dtype=np.float64).reshape(len(_ENV_LEVELS), npts))
    env_tv = env_t * (1.0 + 0.608 * np.clip(env_q, 0, 0.05))

    for k, pk in enumerate(_P_FIXED):
        above = pk <= p0f
        if not above.any():
            continue
        ratio = np.where(above, pk / np.maximum(p0f, 1e-6), 1.0)
        dry = above & (pk >= p_lclf)
        parcel[k, dry] = t0f[dry] * np.power(ratio[dry], KAPPA)
        if k == 0:
            continue
        prev_pk = _P_FIXED[k - 1]
        # 刚穿过 LCL 的第一层：从 LCL 干绝热到该层
        trans = above & (pk < p_lclf) & (prev_pk >= p_lclf)
        if trans.any():
            parcel[k, trans] = t_lclf[trans] * np.power(pk / p_lclf[trans], KAPPA)
        # 已经在湿绝热段的层：由上一层伪绝热递推
        moist = above & (pk < p_lclf) & (prev_pk < p_lclf)
        if moist.any():
            prev_t = parcel[k - 1, moist]
            parcel[k, moist] = prev_t + _moist_dtdp(prev_t, prev_pk - 0.5 * 5.0) * (pk - prev_pk)

    es_p = sat_vapor_pressure(np.where(np.isnan(parcel), 250.0, parcel))
    p_pa = (_P_FIXED[:, None] * 100.0) * np.ones((1, npts))
    rs = EPS * es_p / np.maximum(p_pa - es_p, 1.0)
    parcel_tv = parcel * (1.0 + 0.608 * np.clip(rs, 0.0, 0.05))

    dz = np.zeros_like(parcel)
    dz[1:] = (RD * env_tv[1:] / G0) * np.log(_P_FIXED[:-1][:, None] / _P_FIXED[1:][:, None])
    buoy = G0 * (parcel_tv - env_tv) / np.maximum(env_tv, 100.0)

    valid = ~np.isnan(parcel_tv)
    pos = np.where(valid & (buoy > 0), buoy * dz, 0.0)
    cape = pos.sum(axis=0).reshape(shp)
    cape = np.where(cape < 25.0, 0.0, cape)
    cape = np.nan_to_num(cape, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(cape, 0.0, 8000.0)


# ---------------------------------------------------------------- 派生各产品
def derive_frame(fields, step: int, history: dict, grid: dict) -> dict:
    """把一个时次的原始 GRIB 场加工成各产品需要的 2D 数组。"""
    def arr(param, level=0):
        return fields[(param, level)].astype(np.float64)

    t2m = arr("2t") - 273.15
    td2m = arr("2d") - 273.15
    mslp = arr("msl") / 100.0
    tcc = np.clip(arr("tcc"), 0.0, 100.0)
    tcw = np.clip(arr("tcw"), 0.0, 500.0)
    gh500 = arr("gh", 500)

    u10, v10 = arr("10u"), arr("10v")
    u925, v925 = arr("u", 925), arr("v", 925)
    u850, v850 = arr("u", 850), arr("v", 850)
    u500, v500 = arr("u", 500), arr("v", 500)

    t850, q850 = arr("t", 850), arr("q", 850)
    t700, q700 = arr("t", 700), arr("q", 700)
    t500, q500 = arr("t", 500), arr("q", 500)

    rh2m = np.clip(
        100.0 * sat_vapor_pressure(td2m + 273.15) / np.maximum(sat_vapor_pressure(t2m + 273.15), 1.0),
        0.0, 100.0,
    )
    rh500 = relative_humidity_from_q(q500, 50000.0, t500)
    td850 = dewpoint_from_q(q850, 85000.0) - 273.15
    td700 = dewpoint_from_q(q700, 70000.0) - 273.15
    kindex = (t850 - 273.15) - (t500 - 273.15) + td850 - ((t700 - 273.15) - td700)

    tp = arr("tp")  # kg/m² == mm
    tp_prev6 = history["tp"].get(step - 6)
    tp_prev24 = history["tp"].get(step - 24)
    rain6 = tp - tp_prev6 if tp_prev6 is not None else np.zeros_like(tp)
    rain24 = tp - tp_prev24 if tp_prev24 is not None else np.zeros_like(tp)
    rain6 = np.clip(rain6, 0.0, None)
    rain24 = np.clip(rain24, 0.0, None)

    spd10 = np.hypot(u10, v10)
    history["wind"].setdefault(step, spd10)
    window = [history["wind"][s] for s in (step - 18, step - 12, step - 6, step)
              if s in history["wind"]]
    gust24 = GUST_FACTOR * np.maximum.reduce(window) if window else np.zeros_like(spd10)

    t_prof = np.stack([arr("t", lv) for lv in sorted(PL_PARAMS, reverse=True)])
    q_prof = np.stack([arr("q", lv) for lv in sorted(PL_PARAMS, reverse=True)])
    cape = calc_cape(arr("sp"), arr("2t"), arr("2d"), t_prof, q_prof)

    return {
        "rain6": rain6,
        "rain24": rain24,
        "gh500": gh500,
        "u850": u850, "v850": v850,
        "u500": u500, "v500": v500,
        "u925": u925, "v925": v925,
        "u10": u10, "v10": v10,
        "t2m": t2m,
        "mslp": mslp,
        "gust24": gust24,
        "cape": cape,
        "kindex": kindex,
        "pwat": tcw,
        "rh2m": rh2m,
        "rh500": rh500,
        "tcc": tcc,
        # 辅助量：只用于下一次派生（缓存重跑时恢复降水/阵风的历史）
        "tp": tp,
        "spd10": spd10,
    }


# ---------------------------------------------------------------- 构建数据
def _step_cache_path(run_dt: datetime, step: int) -> Path:
    return CACHE_DIR / ("%s_%03d.npz" % (run_dt.strftime("%Y%m%d%H"), step))


def _save_step(path: Path, arrays: dict, grid: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(
        tmp,
        **{k: v.astype(np.float32) for k, v in arrays.items()},
        lat=grid["lat"].astype(np.float64),
        lon=grid["lon"].astype(np.float64),
    )
    os.replace(tmp, path)


def load_step(path: Path) -> tuple[dict, dict]:
    data = np.load(path)
    arrays = {k: data[k] for k in data.files if k not in ("lat", "lon")}
    grid = {"lat": data["lat"], "lon": data["lon"]}
    return arrays, grid


def build_dataset(run_dt: datetime, workers: int = 4, reuse: bool = True) -> dict[int, Path]:
    """下载 + 派生整场数据，返回 {lead: npz 路径}。"""
    steps = sorted(set(EXTRA_STEPS) | set(LEADS))
    pending = [(s, _step_cache_path(run_dt, s)) for s in steps]
    cached = {s for s, p in pending if reuse and p.is_file()}
    todo = [s for s, _ in pending if s not in cached]
    log("时次共 %d 个：缓存命中 %d，需要下载 %d" % (len(pending), len(cached), len(todo)))

    def fetch_one(step: int):
        if step == 0:
            return step, fetch_fields(run_dt, step, ["tp"], {})
        if step in LEADS:
            return step, fetch_fields(run_dt, step, SFC_PARAMS, PL_PARAMS)
        return step, fetch_fields(run_dt, step, EXTRA_PARAMS["sfc"], EXTRA_PARAMS["pl"])

    # 阶段一：下载（窗口并发，避免一次性把 40 多个时次都排进线程池）
    fetched: dict[int, dict] = {}
    started = time.time()
    queue = list(todo)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        while queue or futures:
            while queue and len(futures) < max(1, workers) * 2:
                step = queue.pop(0)
                futures[pool.submit(fetch_one, step)] = step
            finished = next(as_completed(list(futures)))
            step = futures.pop(finished)
            _, fields = finished.result()
            fetched[step] = fields
            log("AIFS +%dh 下载完成（%.0fs，队列剩余 %d）"
                % (step, time.time() - started, len(queue) + len(futures)))

    # 阶段二：按时次顺序派生，保证 6h/24h 降水、24h 阵风的前置量已经就绪
    paths: dict[int, Path] = {}
    history: dict = {"tp": {}, "wind": {}}
    grid = None
    for step, path in pending:
        if step in fetched:
            fields = fetched.pop(step)
            if grid is None:
                grid = _grid_from_array(next(iter(fields.values())))
            if step in LEADS:
                arrays = derive_frame(fields, step, history, grid)
                history["tp"][step] = arrays["tp"]
                history["wind"][step] = arrays["spd10"]
                _save_step(path, arrays, grid)
                paths[step] = path
            else:
                history["tp"][step] = fields[("tp", 0)].astype(np.float64)
                if ("10u", 0) in fields:
                    history["wind"][step] = np.hypot(
                        fields[("10u", 0)].astype(np.float64),
                        fields[("10v", 0)].astype(np.float64),
                    )
            continue
        arrays, g = load_step(path)
        grid = grid or g
        if "tp" in arrays:
            history["tp"][step] = arrays["tp"].astype(np.float64)
        if "spd10" in arrays:
            history["wind"][step] = arrays["spd10"].astype(np.float64)
        if step in LEADS:
            paths[step] = path

    log("数据准备完成：%d 帧，用时 %.0f 秒" % (len(paths), time.time() - started))
    return paths


def _grid_from_array(field) -> dict:
    """取当前 GRIB 网格（已裁好的经纬度）；没有解包过 GRIB 时按固定 0.25° 网格反推。"""
    if LAST_GRID is not None:
        return LAST_GRID
    # AIFS 固定 0.25° 全球网格，首点 90N/180E，用与 _grid_info 相同的比较方式裁剪
    ni, nj = 1440, 721
    lat_full = 90.0 - 0.25 * np.arange(nj)
    lon_full = (180.0 + 0.25 * np.arange(ni)) % 360.0
    rows = np.where((lat_full >= BOX["south"]) & (lat_full <= BOX["north"]))[0]
    cols = np.where((lon_full >= BOX["west"]) & (lon_full <= BOX["east"]))[0]
    lat = np.ascontiguousarray(lat_full[rows.min():rows.max() + 1][::-1])
    lon = np.ascontiguousarray(lon_full[cols.min():cols.max() + 1])
    return {"lat": lat, "lon": lon}


# ---------------------------------------------------------------- 绘图
def base_map(ax, ccrs, cfeature, domain: str = DEFAULT_DOMAIN):
    import numpy as np

    d = DOMAINS[domain]
    ax.set_extent([d["west"], d["east"], d["south"], d["north"]], crs=ccrs.PlateCarree())
    ax.add_feature(cfeature.OCEAN.with_scale("10m"), facecolor="#f4f8fb", edgecolor="none", zorder=0)
    ax.add_feature(cfeature.LAND.with_scale("10m"), facecolor="#f5f0e4", edgecolor="none", zorder=0)
    try:
        ax.add_feature(
            cfeature.LAKES.with_scale("10m"),
            facecolor="#f4f8fb", edgecolor="#7e8b93", linewidth=0.25, zorder=1,
        )
    except Exception:
        pass
    ax.add_feature(
        cfeature.BORDERS.with_scale("10m"),
        linewidth=0.55, linestyle=":", edgecolor="#9d978b", zorder=4,
    )
    try:
        ax.add_feature(
            cfeature.STATES.with_scale("50m"),
            linewidth=0.32, edgecolor="#aaa49a", zorder=4,
        )
    except Exception:
        pass
    ax.coastlines("10m", linewidth=0.6, color="#4d5a52", zorder=5)

    # 广东省界单独加粗标黄
    try:
        from cartopy.feature import ShapelyFeature
        from cartopy.io import shapereader

        provinces = shapereader.natural_earth(
            resolution="50m", category="cultural", name="admin_1_states_provinces",
        )
        guangdong = []
        for record in shapereader.Reader(provinces).records():
            attrs = record.attributes
            labels = [str(attrs.get(key) or "")
                      for key in ("name", "name_en", "name_local", "iso_3166_2")]
            if any("guangdong" in label.lower() or label == "CN-GD" for label in labels):
                guangdong.append(record.geometry)
        if guangdong:
            ax.add_feature(ShapelyFeature(
                guangdong, ccrs.PlateCarree(), facecolor="none",
                edgecolor="#6b5a00", linewidth=4.0, alpha=0.35, zorder=9,
            ))
            ax.add_feature(ShapelyFeature(
                guangdong, ccrs.PlateCarree(), facecolor="none",
                edgecolor="#FFE000", linewidth=2.4, zorder=10,
            ))
    except Exception:
        pass

    step = 1 if domain == "sc" else (10 if (d["east"] - d["west"]) > 30 else 5)
    xlocs = np.arange(np.ceil(d["west"] / step) * step, d["east"] + 0.1, step)
    ylocs = np.arange(np.ceil(d["south"] / step) * step, d["north"] + 0.1, step)
    gl = ax.gridlines(
        draw_labels=True, xlocs=xlocs, ylocs=ylocs,
        linewidth=0.35, color="#9aa4ad", linestyle="--", alpha=0.5, zorder=2,
    )
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 8}
    gl.ylabel_style = {"size": 8}
    cities = {
        "Guangzhou": (113.26, 23.13),
        "Hong Kong": (114.17, 22.32),
        "Macau": (113.54, 22.20),
        "Haikou": (110.20, 20.04),
    }
    if domain == "ea":
        cities.update({
            "Beijing": (116.40, 39.90),
            "Shanghai": (121.47, 31.23),
            "Seoul": (126.98, 37.57),
            "Tokyo": (139.69, 35.68),
            "Taipei": (121.56, 25.03),
            "Manila": (120.98, 14.60),
            "Hanoi": (105.85, 21.03),
        })
    for name, (x, y) in cities.items():
        ax.plot(x, y, "o", ms=3, color="#222", transform=ccrs.PlateCarree(), zorder=11)
        ax.text(x + 0.06, y + 0.06, name, fontsize=6.5, color="#222",
                transform=ccrs.PlateCarree(), zorder=11)
    return ax


def finish_figure(fig, ax, cf, colorbar_ticks, colorbar_labels, colorbar_title,
                  title: str, valid: datetime, run_dt: datetime, lead: int,
                  out_path: Path, note: str = "") -> None:
    fig.text(0.065, 0.955, title, fontsize=16.5, fontweight="bold", ha="left", va="top")
    fig.text(0.955, 0.955, valid.strftime("%Y/%m/%d %H(UTC)"),
             fontsize=11, ha="right", va="top")
    fig.text(0.065, 0.923, "ECMWF AIFS 0.25°" + ("  ·  " + note if note else ""),
             fontsize=9.5, color="#555", ha="left", va="top")
    fig.text(0.065, 0.032,
             "Init: %s  ·  Lead +%d h" % (run_dt.strftime("%Y-%m-%d %H UTC"), lead),
             fontsize=8, color="#666", ha="left", va="bottom")
    fig.text(0.955, 0.032, "www.bamc.top", fontsize=11, color="#000000",
             ha="right", va="bottom")
    cax = fig.add_axes([0.885, 0.16, 0.022, 0.62])
    cb = fig.colorbar(cf, cax=cax, ticks=colorbar_ticks)
    cb.ax.tick_params(labelsize=7.5, length=2)
    cb.set_ticklabels(colorbar_labels)
    cb.set_label(colorbar_title, fontsize=8, labelpad=6)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, facecolor="white")


def smooth(field, sigma: float | None = None):
    """遮挡 GRIB 量化台阶用的轻平滑（仅用于绘图）。"""
    sigma = SMOOTH_SIGMA if sigma is None else sigma
    if sigma <= 0:
        return np.asarray(field, dtype=np.float64)
    data = np.asarray(field, dtype=np.float64)
    try:
        from scipy.ndimage import gaussian_filter

        return gaussian_filter(data, sigma=sigma, mode="nearest")
    except Exception:  # noqa: BLE001
        k = 3
        pad = np.pad(data, k // 2, mode="edge")
        acc = np.zeros_like(data)
        for i in range(k):
            for j in range(k):
                acc += pad[i:i + data.shape[0], j:j + data.shape[1]]
        return acc / (k * k)


def render_frame(arrays: dict, grid: dict, run_dt: datetime, lead: int,
                 spec: dict, out_path: Path) -> None:
    import cartopy
    cartopy.config["data_dir"] = str(BASE_DIR / ".cartopy")

    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    valid = run_dt + timedelta(hours=lead)
    domain = spec.get("domain", DEFAULT_DOMAIN)
    barb_step = 6 if domain == "ea" else 2   # 东亚 1.5°、华南 0.5°
    barb_slice = (slice(None, None, barb_step), slice(None, None, barb_step))
    LAT, LON = np.meshgrid(grid["lat"], grid["lon"], indexing="ij")
    kind = spec["kind"]

    fig = Figure(figsize=(10, 7.2), dpi=140, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.065, 0.075, 0.80, 0.80], projection=ccrs.PlateCarree())
    base_map(ax, ccrs, cfeature, domain)
    note = spec.get("note", "")

    if kind == "combined":
        rain = smooth(arrays["rain6"], 0.8)
        height = smooth(arrays["gh500"])
        u, v = smooth(arrays["u850"]), smooth(arrays["v850"])
        cf = ax.contourf(LON, LAT, rain, levels=RAIN_6H_LEVELS, colors=RAIN_6H_COLORS,
                         extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, height,
                        levels=[lv for lv in np.arange(5640, 5941, 20) if lv != 5880],
                        colors="#1b1b1b", linewidths=1.15,
                        transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=7, fmt=lambda x: f"{x / 10:.0f}", colors="#1b1b1b")

        if np.nanmin(height) <= 5880 <= np.nanmax(height):
            cs588 = ax.contour(LON, LAT, height, levels=[5880], colors="#d62728",
                               linewidths=2.4, transform=ccrs.PlateCarree(), zorder=8)
            ax.clabel(cs588, inline=False, fontsize=8.5, fmt=lambda x: "588", colors="#d62728")

        # 850hPa 风速 ≥ 24.5 m/s 的风矢标红
        speed = np.hypot(u, v)
        high_wind = speed >= 24.5
        barb_style = dict(transform=ccrs.PlateCarree(), length=5.8, linewidth=0.38,
                          barb_increments={"half": 2.5, "full": 5, "flag": 25}, zorder=7)
        u_barb, v_barb = u[barb_slice], v[barb_slice]
        high_barb = high_wind[barb_slice]
        lon_barb, lat_barb = LON[barb_slice], LAT[barb_slice]
        ax.barbs(lon_barb, lat_barb,
                 np.ma.masked_where(high_barb, u_barb), np.ma.masked_where(high_barb, v_barb),
                 color="#2847c7", **barb_style)
        ax.barbs(lon_barb, lat_barb,
                 np.ma.masked_where(~high_barb, u_barb), np.ma.masked_where(~high_barb, v_barb),
                 color="#e31a1c", **barb_style)
        finish_figure(fig, ax, cf, RAIN_6H_LEVELS,
                      ["0.1", "1", "4", "13", "25", "60", "120", "250"],
                      "6h rainfall (mm)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "t2m":
        temp = smooth(arrays["t2m"])
        cf = ax.contourf(LON, LAT, temp, levels=TEMP_LEVELS, colors=TEMP_COLORS,
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, temp, levels=np.arange(0, 36, 2), colors="#7a5b34",
                        linewidths=0.55, transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#684c2b")
        finish_figure(fig, ax, cf, TEMP_LEVELS, [str(x) for x in TEMP_LEVELS],
                      "2m temperature (°C)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "h500":
        height = smooth(arrays["gh500"])
        u, v = smooth(arrays["u500"]), smooth(arrays["v500"])
        cf = ax.contourf(LON, LAT, height, levels=HEIGHT_FILL_LEVELS, colors=HEIGHT_FILL_COLORS,
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, height, levels=np.arange(5700, 6001, 20), colors="#1b1b1b",
                        linewidths=1.0, transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=7, fmt=lambda x: f"{x / 10:.0f}", colors="#1b1b1b")
        ax.barbs(LON[barb_slice], LAT[barb_slice], u[barb_slice], v[barb_slice],
                 transform=ccrs.PlateCarree(), color="#14a3b8", length=5.8, linewidth=0.38,
                 barb_increments={"half": 2.5, "full": 5, "flag": 25}, zorder=7)
        finish_figure(fig, ax, cf, [5800, 5840, 5880, 5920, 5960],
                      ["580", "584", "588", "592", "596"],
                      "500hPa height (dam)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "rain24":
        rain = smooth(arrays["rain24"], 0.8)
        cf = ax.contourf(LON, LAT, rain, levels=RAIN_24H_LEVELS, colors=RAIN_24H_COLORS,
                         extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, rain, levels=[1, 10, 25, 50, 100], colors="#385a3b",
                        linewidths=0.55, transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%g", colors="#244426")
        finish_figure(fig, ax, cf, RAIN_24H_LEVELS,
                      ["0.1", "1", "10", "25", "50", "100", "250"],
                      "24h rainfall (mm)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "mslp":
        pressure = smooth(arrays["mslp"])
        cf = ax.contourf(LON, LAT, pressure, levels=MSLP_LEVELS, cmap="RdYlBu",
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, pressure, levels=MSLP_LEVELS, colors="#2a2a2a",
                        linewidths=0.7, transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%g", colors="#222222")
        finish_figure(fig, ax, cf, [990, 995, 1000, 1005, 1010, 1015, 1020, 1025, 1030],
                      ["990", "995", "1000", "1005", "1010", "1015", "1020", "1025", "1030"],
                      "MSLP (hPa)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind in ("wind10", "wind925"):
        if kind == "wind10":
            u, v = smooth(arrays["u10"]), smooth(arrays["v10"])
            levels, labels, unit = WIND_SPEED_LEVELS, WIND_SPEED_LABELS, "10m wind (m/s)"
        else:
            u, v = smooth(arrays["u925"]), smooth(arrays["v925"])
            levels, labels, unit = WIND925_LEVELS, [str(x) for x in WIND925_LEVELS], "925hPa wind (m/s)"
        speed = np.hypot(u, v)
        if kind == "wind10":
            cf = ax.contourf(LON, LAT, speed, levels=levels, colors=WIND_SPEED_COLORS,
                             extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
            cf.cmap.set_over("#ff1900")
            cf.cmap.set_under("#ffffff")
        else:
            cf = ax.contourf(LON, LAT, speed, levels=levels, cmap="YlGnBu",
                             extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        ax.barbs(LON[barb_slice], LAT[barb_slice], u[barb_slice], v[barb_slice],
                 transform=ccrs.PlateCarree(), color="#08306b", length=5.8, linewidth=0.38,
                 barb_increments={"half": 2.5, "full": 5, "flag": 25}, zorder=7)
        finish_figure(fig, ax, cf, levels, labels, unit,
                      spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "gust24":
        gust = smooth(arrays["gust24"])
        cf = ax.contourf(LON, LAT, gust, levels=WIND_SPEED_LEVELS, colors=WIND_SPEED_COLORS,
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cf.cmap.set_over("#ff1900")
        cf.cmap.set_under("#ffffff")
        finish_figure(fig, ax, cf, WIND_SPEED_LEVELS, WIND_SPEED_LABELS,
                      "24h gust (m/s)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "cape":
        cape = smooth(arrays["cape"])
        cf = ax.contourf(LON, LAT, cape, levels=CAPE_LEVELS, cmap="YlOrRd",
                         extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, cape, levels=[250, 500, 1000, 2000, 3000, 4000],
                        colors="#5a2b16", linewidths=0.45,
                        transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=5.5, fmt="%d", colors="#4a220f")
        finish_figure(fig, ax, cf, CAPE_LEVELS, [str(x) for x in CAPE_LEVELS],
                      "CAPE (J/kg)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "kindex":
        k_index = smooth(arrays["kindex"])
        cf = ax.contourf(LON, LAT, k_index, levels=K_LEVELS, cmap="Spectral_r",
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, k_index, levels=[20, 25, 30, 35, 40],
                        colors="#3b2a26", linewidths=0.5,
                        transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#2c1f1c")
        finish_figure(fig, ax, cf, K_LEVELS, [str(x) for x in K_LEVELS],
                      "K index (°C)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "pwat":
        pwat = smooth(arrays["pwat"])
        cf = ax.contourf(LON, LAT, pwat, levels=PWAT_LEVELS, cmap="YlGnBu",
                         extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, pwat, levels=[20, 30, 40, 50, 60],
                        colors="#174f5c", linewidths=0.5,
                        transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#123f49")
        finish_figure(fig, ax, cf, PWAT_LEVELS, [str(x) for x in PWAT_LEVELS],
                      "PWAT (mm)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind in ("rh2m", "rh500"):
        rh = smooth(arrays["rh2m"] if kind == "rh2m" else arrays["rh500"])
        cf = ax.contourf(LON, LAT, rh, levels=RH_LEVELS, cmap="YlGnBu",
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cs = ax.contour(LON, LAT, rh, levels=[50, 70, 80, 90, 95],
                        colors="#234b55", linewidths=0.45,
                        transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#173942")
        finish_figure(fig, ax, cf, RH_LEVELS, [str(x) for x in RH_LEVELS],
                      "relative humidity (%)", spec["title"], valid, run_dt, lead, out_path, note)

    elif kind == "cloud":
        cloud = smooth(arrays["tcc"])
        cf = ax.contourf(LON, LAT, cloud, levels=CLOUD_LEVELS, colors=CLOUD_COLORS,
                         extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True)
        cf.cmap.set_under("#ffffff")
        cf.cmap.set_over(CLOUD_OVER)
        cs = ax.contour(LON, LAT, cloud, levels=[40, 50, 60, 70, 80, 90],
                        colors="#2f5bd0", linewidths=0.55,
                        transform=ccrs.PlateCarree(), zorder=6)
        ax.clabel(cs, inline=True, fontsize=6, fmt=lambda x: f"{x / 10:.0f}", colors="#1b3fa8")
        finish_figure(fig, ax, cf, CLOUD_LEVELS, CLOUD_LABELS,
                      "total cloud cover (tenths)", spec["title"], valid, run_dt, lead, out_path, note)


# ---------------------------------------------------------------- 并行渲染
def _render_step_job(npz_path: str, run_iso: str, out_dir_str: str,
                     products: list[str]) -> str:
    arrays, grid = load_step(Path(npz_path))
    run_dt = datetime.fromisoformat(run_iso)
    out_dir = Path(out_dir_str)
    lead = int(Path(npz_path).stem.rsplit("_", 1)[-1])
    valid = run_dt + timedelta(hours=lead)
    for product_id in products:
        spec = PRODUCTS[product_id]
        name = spec["prefix"] + valid.strftime("%Y%m%d%H") + ".png"
        render_frame(arrays, grid, run_dt, lead, spec, out_dir / spec["subdir"] / name)
    return "step %d" % lead


def render_all(run_dt: datetime, steps: dict[int, Path], out_dir: Path,
               products: list[str], jobs: int = 0) -> None:
    cpu = os.cpu_count() or 4
    jobs = jobs if jobs > 0 else max(1, min(6, cpu - 1))
    jobs = max(1, min(jobs, len(steps)))
    mode = os.environ.get("AIFS_RENDER_MODE", "auto").strip().lower()
    if jobs <= 1:
        mode = "serial"

    items = [(str(steps[lead]), run_dt.isoformat(), str(out_dir), products)
             for lead in sorted(steps)]
    log("开始渲染 %d 帧 × %d 个产品（%d 进程）" % (len(items), len(products), jobs))
    started = time.time()
    done = 0

    if mode in ("auto", "process"):
        try:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                for result in pool.map(_render_step_job, *zip(*items)):
                    done += 1
                    if done % 5 == 0:
                        log("渲染进度 %d/%d（%.0fs）" % (done, len(items), time.time() - started))
            log("渲染完成：%d 帧，%.0f 秒" % (done, time.time() - started))
            return
        except (PermissionError, OSError) as exc:
            log("多进程渲染不可用（%s），改用线程" % exc)

    if mode in ("auto", "thread"):
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_render_step_job, *item_) for item_ in items]
            for future in as_completed(futures):
                future.result()
                done += 1
                if done % 5 == 0:
                    log("渲染进度 %d/%d（%.0fs）" % (done, len(items), time.time() - started))
        log("渲染完成：%d 帧，%.0f 秒" % (done, time.time() - started))
        return

    for item_ in items:
        _render_step_job(*item_)
        done += 1
        if done % 5 == 0:
            log("渲染进度 %d/%d（%.0fs）" % (done, len(items), time.time() - started))
    log("渲染完成：%d 帧，%.0f 秒" % (done, time.time() - started))


def write_manifests(run_dt: datetime, out_dir: Path, products: list[str],
                    generated: str) -> None:
    for product_id in products:
        spec = PRODUCTS[product_id]
        domain_name = spec.get("domain", DEFAULT_DOMAIN)
        subdir = out_dir / spec["subdir"]
        frames = []
        for lead in LEADS:
            valid = run_dt + timedelta(hours=lead)
            stamp = valid.strftime("%Y%m%d%H")
            frames.append({
                "s": stamp,
                "valid_utc": valid.strftime("%Y-%m-%d %H:%M UTC"),
                "lead": lead,
                "file": spec["prefix"] + stamp + ".png",
            })
        manifest = {
            "product": product_id,
            "run": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "run_iso": run_dt.isoformat(),
            "generated": generated,
            "source": "ECMWF AIFS Single 0.25° (Open Data)",
            "region": {
                "west": DOMAINS[domain_name]["west"],
                "south": DOMAINS[domain_name]["south"],
                "east": DOMAINS[domain_name]["east"],
                "north": DOMAINS[domain_name]["north"],
            },
            "frames": frames,
        }
        (subdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        log("已更新 %s" % (subdir / "manifest.json"))


def main() -> None:
    ap = argparse.ArgumentParser(description="生成华南 ECMWF AIFS 预报图")
    ap.add_argument("--run", default="", help="起报场，例如 2026-11-07T00:00")
    ap.add_argument("--input", default="", help="已下载的 npz 缓存目录（跳过下载）")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    ap.add_argument("--products", default=",".join(ALL_PRODUCTS), help="要生成的产品，逗号分隔")
    ap.add_argument("--jobs", type=int, default=0, help="并行渲染进程数；0=自动，1=单进程")
    ap.add_argument("--download-workers", type=int, default=int(os.environ.get("AIFS_FETCH_WORKERS", "4")))
    args = ap.parse_args()

    run = args.run or latest_run()
    run_dt = datetime.strptime(run, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    out_dir = Path(args.out)
    selected = [x.strip() for x in args.products.split(",") if x.strip()]
    unknown = [x for x in selected if x not in PRODUCTS]
    if unknown:
        raise SystemExit("未知产品：%s" % ", ".join(unknown))
    generated = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M HKT")

    if args.input:
        src = Path(args.input)
        steps = {}
        for lead in LEADS:
            path = src / _step_cache_path(run_dt, lead).name
            if not path.is_file():
                raise SystemExit("缓存缺少 +%dh：%s" % (lead, path))
            steps[lead] = path
        log("使用已有 npz 缓存：%s（%d 帧）" % (src, len(steps)))
    else:
        steps = build_dataset(run_dt, workers=max(1, args.download_workers))

    render_all(run_dt, steps, out_dir, selected, jobs=args.jobs)
    write_manifests(run_dt, out_dir, selected, generated)
    log("完成：起报场 %s，%d 个产品" % (run_dt.strftime("%Y-%m-%d %H:%M UTC"), len(selected)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
