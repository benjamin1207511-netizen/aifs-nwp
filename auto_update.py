#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIFS 图片自动更新器。

ECMWF AIFS Single 每天有 4 个起报场（00/06/12/18 UTC），Open Data 上先出
前面几个时次、几小时后才补齐到 +240h。本程序每 30 分钟检查一次，发现新的
完整场次后：

  1. 调用 aifs_chart.py 生成 16 类 × 37 帧到临时目录；
  2. 校验每个产品和 manifest；
  3. 先复制新图片，再原子替换 manifest，最后清理旧 PNG；
  4. 写状态文件，网站无需重启即可在 30 秒内读到新图。

用法：
  python auto_update.py                  # 常驻，每 30 分钟检查
  python auto_update.py --once           # 检查并更新一次后退出（适合任务计划）
  python auto_update.py --dry-run        # 只检查最新起报场，不生成
  python auto_update.py --force          # 强制重新生成当前最新场
  python auto_update.py --interval 1800

依赖（生成图片时需要）：
  numpy matplotlib cartopy eccodes
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aifs_chart

BASE_DIR = Path(__file__).resolve().parent
LIVE_DIR = BASE_DIR / "aifs"
BUILD_DIR = BASE_DIR / "aifs_build"
STATE_FILE = BASE_DIR / ".auto_update_state.json"
LOCK_FILE = BASE_DIR / ".auto_update.lock"
LOG_FILE = BASE_DIR / "auto_update.log"
TZ_CN = timezone(timedelta(hours=8))
UTC = timezone.utc
POLL_SECONDS = 30 * 60
# 单次更新失败（多半是网络抖动）后的重试次数与等待秒数，可用环境变量覆盖。
BUILD_RETRIES = max(1, int(os.environ.get("AIFS_BUILD_RETRIES", "3")))
RETRY_WAIT = max(10, int(os.environ.get("AIFS_RETRY_WAIT", "120")))


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"), msg)
    # pythonw / 后台任务可能没有 stdout，或者控制台管道已被关闭。
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (BrokenPipeError, OSError):
            pass
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def parse_run(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace(" UTC", "").strip().replace("T", " ")
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def live_run() -> datetime | None:
    manifest = LIVE_DIR / "combined" / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return parse_run(data.get("run"))
    except (OSError, ValueError):
        return None


def read_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(data: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def pid_running(pid: int) -> bool:
    """判断锁文件里的 PID 是否仍存在（Windows 用 OpenProcess）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # 5 = ACCESS_DENIED：进程存在但当前用户没有查询权限。
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_lock() -> None:
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return
    except FileExistsError:
        try:
            pid_text = LOCK_FILE.read_text(encoding="ascii").strip()
            pid = int(pid_text)
        except (OSError, ValueError):
            pid = 0

        if pid and pid_running(pid):
            raise RuntimeError("已有 auto_update 在运行（PID %d）" % pid)
        if pid:
            log("旧锁 PID %d 已不存在，自动清理" % pid)
        else:
            log("锁文件没有有效 PID，自动清理")
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
        return acquire_lock()


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def validate_build(build_dir: Path, run_text: str) -> None:
    if not build_dir.is_dir():
        raise RuntimeError("构建目录不存在：%s" % build_dir)

    expected = {spec["subdir"]: spec for spec in aifs_chart.PRODUCTS.values()}
    actual = {p.name for p in build_dir.iterdir() if p.is_dir()}
    missing = set(expected) - actual
    extra = actual - set(expected)
    if missing or extra:
        raise RuntimeError("构建产品不完整：缺少 %s，多出 %s" % (sorted(missing), sorted(extra)))

    for subdir in expected:
        manifest_path = build_dir / subdir / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("缺少 manifest：%s" % manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if parse_run(manifest.get("run")) != parse_run(run_text):
            raise RuntimeError("%s 的起报场不正确" % subdir)
        frames = manifest.get("frames") or []
        if len(frames) != len(aifs_chart.LEADS):
            raise RuntimeError("%s 的帧数不是 %d" % (subdir, len(aifs_chart.LEADS)))
        for frame in frames:
            path = build_dir / subdir / str(frame.get("file") or "")
            if not path.is_file() or path.stat().st_size < 1000:
                raise RuntimeError("图片缺失或为空：%s" % path)
    log("构建校验通过：%d 个产品，每产品 %d 帧" % (len(expected), len(aifs_chart.LEADS)))


def publish_build(build_dir: Path) -> None:
    """先复制新图，再原子切换 manifest；最后删除旧图。"""
    subdirs = [p for p in build_dir.iterdir() if p.is_dir()]
    manifests: dict[str, dict] = {}

    # 第一阶段：复制图片。此时线上仍使用旧 manifest，页面不会受影响。
    for src_dir in subdirs:
        live_dir = LIVE_DIR / src_dir.name
        live_dir.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((src_dir / "manifest.json").read_text(encoding="utf-8"))
        manifests[src_dir.name] = manifest
        for frame in manifest.get("frames") or []:
            name = str(frame.get("file") or "")
            src = src_dir / name
            if not src.is_file():
                raise RuntimeError("待发布图片不存在：%s" % src)
            shutil.copy2(src, live_dir / name)
        log("已复制 %s 的 %d 张图片" % (src_dir.name, len(manifest.get("frames") or [])))

    # 第二阶段：逐产品原子替换 manifest。
    for subdir, manifest in manifests.items():
        live_dir = LIVE_DIR / subdir
        tmp = live_dir / ".manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, live_dir / "manifest.json")
        log("已切换 %s/manifest.json" % subdir)

    # 第三阶段：清理不再引用的旧图（服务端只认 manifest，旧 URL 不会再用）。
    for subdir, manifest in manifests.items():
        live_dir = LIVE_DIR / subdir
        keep = {str(frame.get("file") or "") for frame in manifest.get("frames") or []}
        for old in live_dir.glob("*.png"):
            if old.name not in keep:
                try:
                    old.unlink()
                except OSError:
                    pass
    log("旧图片清理完成")


def prune_live() -> int:
    """兜底清理：删除 aifs/ 下所有不属于当前 manifest 的 PNG，保证只保留最新场次。"""
    removed = 0
    if not LIVE_DIR.is_dir():
        return 0
    for sub in LIVE_DIR.iterdir():
        if not sub.is_dir():
            continue
        manifest = sub / "manifest.json"
        if not manifest.is_file():
            for png in sub.glob("*.png"):
                try:
                    png.unlink()
                    removed += 1
                except OSError:
                    pass
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        keep = {str(frame.get("file") or "") for frame in data.get("frames") or []}
        for png in sub.glob("*.png"):
            if png.name not in keep:
                try:
                    png.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        log("清理旧场次图片 %d 张（只保留最新场次）" % removed)
    return removed


def run_once(force: bool = False, dry_run: bool = False) -> bool:
    state = read_state()
    current = live_run()
    log("当前线上起报场：%s" % (current.strftime("%Y-%m-%d %H:%M UTC") if current else "无"))

    latest_text = aifs_chart.latest_run()
    latest = parse_run(latest_text)
    if not latest:
        raise RuntimeError("无法解析最新起报场：%s" % latest_text)
    log("ECMWF Open Data 最新完整起报场：%s" % latest.strftime("%Y-%m-%d %H:%M UTC"))

    if not force and current and latest <= current:
        log("没有新起报场，跳过生成")
        state.update({
            "last_checked": datetime.now(TZ_CN).isoformat(),
            "live_run": current.isoformat(),
            "latest_seen": latest.isoformat(),
        })
        write_state(state)
        return False

    if dry_run:
        log("dry-run：有新场，但未生成")
        return True

    cmd = [
        sys.executable,
        str(BASE_DIR / "aifs_chart.py"),
        "--run",
        latest_text,
        "--out",
        str(BUILD_DIR),
    ]
    log("开始生成：%s" % " ".join(cmd))
    started = time.time()

    # 网络偶尔会中断；失败后清掉半成品重试，已下载的分块有本地缓存，不必重下。
    last_error: Exception | None = None
    for attempt in range(1, BUILD_RETRIES + 1):
        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR)
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        log("第 %d/%d 次生成尝试" % (attempt, BUILD_RETRIES))
        result = subprocess.run(cmd, cwd=str(BASE_DIR))
        if result.returncode == 0:
            last_error = None
            break
        last_error = RuntimeError("aifs_chart.py 退出码 %d" % result.returncode)
        log("生成失败：%s" % last_error)
        if attempt < BUILD_RETRIES:
            log("等待 %d 秒后重试" % RETRY_WAIT)
            time.sleep(RETRY_WAIT)

    if last_error is not None:
        raise last_error

    validate_build(BUILD_DIR, latest_text)
    publish_build(BUILD_DIR)
    prune_live()

    state.update({
        "last_checked": datetime.now(TZ_CN).isoformat(),
        "last_success": datetime.now(TZ_CN).isoformat(),
        "live_run": latest.isoformat(),
        "latest_seen": latest.isoformat(),
        "duration_sec": round(time.time() - started, 1),
    })
    write_state(state)
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    cache_dir = getattr(aifs_chart, "CACHE_DIR", None)
    if cache_dir and Path(cache_dir).is_dir():
        shutil.rmtree(cache_dir, ignore_errors=True)
    log("更新完成，耗时 %.1f 秒；网站无需重启" % (time.time() - started))
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="AIFS 图片自动更新器")
    ap.add_argument("--once", action="store_true", help="检查并更新一次后退出")
    ap.add_argument("--force", action="store_true", help="强制重新生成当前最新场")
    ap.add_argument("--dry-run", action="store_true", help="只检查最新场，不生成")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS, help="轮询秒数，默认 1800")
    args = ap.parse_args()

    interval = max(300, args.interval)
    try:
        acquire_lock()
    except Exception as exc:  # noqa: BLE001
        log("启动失败：%s" % exc)
        raise SystemExit(1)

    log("自动更新器启动；轮询间隔 %d 秒；ECMWF AIFS 每天 4 个起报场" % interval)
    try:
        while True:
            try:
                run_once(force=args.force, dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001
                log("本轮失败：%s" % exc)
                log(traceback.format_exc())
            if args.once:
                break
            log("等待 %d 秒后再次检查" % interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
