/* ============================================================
   BAMC AIFS 预报 · 前端
   图像仍由 /api/img 提供，本文件只负责界面和播放控制。
   ============================================================ */
(function () {
  "use strict";

  const IMG_VERSION = "20261108a";
  const AUTO_REFRESH_MS = 5 * 60 * 1000;
  const DEFAULT_PRODUCT = "aifs-southchina";

  const $ = (id) => document.getElementById(id);

  const els = {
    clock: $("clock"),
    status: $("status"),
    statusText: $("statusText"),
    btnRefresh: $("btnRefresh"),
    btnFullscreen: $("btnFullscreen"),
    btnHeroRefresh: $("btnHeroRefresh"),
    viewerCard: $("viewerCard"),
    imageTitle: $("imageTitle"),
    imageSub: $("imageSub"),
    imageStatus: $("imageStatus"),
    loadbar: $("loadbar"),
    viewer: $("viewer"),
    stage: $("stage"),
    layerA: $("layerA"),
    layerB: $("layerB"),
    regionBadge: $("regionBadge"),
    placeholder: $("placeholder"),
    errorBox: $("errorBox"),
    requestInfo: $("requestInfo"),
    slider: $("slider"),
    axisStart: $("axisStart"),
    axisEnd: $("axisEnd"),
    curTime: $("curTime"),
    frameIdx: $("frameIdx"),
    btnFirst: $("btnFirst"),
    btnPrev: $("btnPrev"),
    btnPlay: $("btnPlay"),
    playIcon: $("playIcon"),
    btnNext: $("btnNext"),
    btnLast: $("btnLast"),
    productSelect: $("productSelect"),
    timeSelect: $("timeSelect"),
    speedSelect: $("speedSelect"),
    btnLoop: $("btnLoop"),
    btnApply: $("btnApply"),
    btnDownload: $("btnDownload"),
    btnDownloadIcon: $("btnDownloadIcon"),
    productNote: $("productNote"),
    metaCoverage: $("metaCoverage"),
    metaLevel: $("metaLevel"),
    metaRun: $("metaRun"),
    btnZoomIn: $("btnZoomIn"),
    btnZoomOut: $("btnZoomOut"),
    btnZoomReset: $("btnZoomReset"),
    toast: $("toast"),
  };

  const state = {
    products: [],
    product: null,
    productMeta: null,
    frames: [],
    idx: 0,
    run: "",
    runUtc: 0,
    generated: "",
    playing: false,
    speed: 1,
    loop: true,
    timer: null,
    autoTimer: null,
    reqSeq: 0,
    loadSeq: 0,
    active: "a",
    currentStamp: "",
    hasImage: false,
    zoom: 1,
    panX: 0,
    panY: 0,
    toastTimer: null,
    loadbarTimer: null,
  };

  const pad = (n) => String(n).padStart(2, "0");

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function api(path) {
    return fetch(path, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    }).then((res) => {
      if (!res.ok) {
        if (res.status === 403) {
          throw new Error("会话校验失败，请刷新页面后重试");
        }
        throw new Error("请求失败（HTTP " + res.status + "）");
      }
      return res.json();
    });
  }

  function toast(message) {
    if (!els.toast) return;
    els.toast.textContent = message;
    els.toast.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => els.toast.classList.remove("show"), 2600);
  }

  function setStatus(text, kind) {
    if (!els.status) return;
    els.status.className = "status" + (kind ? " " + kind : "");
    els.statusText.textContent = text;
  }

  function setImageStatus(text, kind) {
    if (!els.imageStatus) return;
    els.imageStatus.className = "image-status" + (kind ? " " + kind : "");
    els.imageStatus.textContent = text;
  }

  function progress(value) {
    if (!els.loadbar) return;
    els.loadbar.style.width = Math.max(0, Math.min(100, value)) + "%";
  }

  function startLoadingBar() {
    clearTimeout(state.loadbarTimer);
    progress(14);
  }

  function finishLoadingBar() {
    progress(100);
    clearTimeout(state.loadbarTimer);
    state.loadbarTimer = setTimeout(() => progress(0), 700);
  }

  function updateClock() {
    const d = new Date();
    const text = "现在是 " + d.getFullYear() + "年" + (d.getMonth() + 1) + "月" +
      d.getDate() + "日 " + pad(d.getHours()) + ":" + pad(d.getMinutes()) +
      ":" + pad(d.getSeconds());
    if (els.clock) els.clock.textContent = text;
  }

  function parseStamp(stamp) {
    const s = String(stamp || "");
    if (s.length !== 10) return 0;
    return Date.UTC(
      Number(s.slice(0, 4)),
      Number(s.slice(4, 6)) - 1,
      Number(s.slice(6, 8)),
      Number(s.slice(8, 10)),
      0,
      0
    );
  }

  function parseRun(value) {
    const m = String(value || "").match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!m) return 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  }

  function utcText(stamp) {
    const s = String(stamp || "");
    if (s.length !== 10) return "—";
    return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8) + " " + s.slice(8, 10) + ":00 UTC";
  }

  function localText(frame) {
    if (frame && frame.ts) {
      const d = new Date(Number(frame.ts) * 1000);
      return d.getFullYear() + "/" + pad(d.getMonth() + 1) + "/" + pad(d.getDate()) +
        " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    }
    if (frame && frame.full) return String(frame.full);
    return "—";
  }

  function shortLocal(frame) {
    if (frame && frame.d && frame.t) return frame.d + " " + frame.t;
    return localText(frame).replace(/^\d{4}\//, "");
  }

  function leadHours(frame) {
    if (!state.runUtc) return 0;
    return Math.round((parseStamp(frame.s) - state.runUtc) / 3600000);
  }

  function imgUrl(frame) {
    return "/api/img?p=" + encodeURIComponent(state.product) + "&s=" + encodeURIComponent(frame.s) +
      "&v=" + encodeURIComponent(IMG_VERSION);
  }

  function downloadUrl() {
    const frame = state.frames[state.idx];
    if (!frame) return "";
    return imgUrl(frame) + "&dl=1";
  }

  function updateMeta(data) {
    const product = state.productMeta || {};
    const title = data.title || product.title || "AIFS 预报图";
    const coverage = data.coverage || product.coverage || "华南区域";
    const level = data.level || product.level || "";
    const sourceName = data.sourceName || "ECMWF AIFS";
    const interval = Number(data.interval || product.interval || 360);
    const intervalText = interval % 60 === 0 ? (interval / 60) + " 小时" : interval + " 分钟";

    els.imageTitle.textContent = title;
    els.imageSub.textContent = sourceName + " · " + coverage;
    els.regionBadge.textContent = (String(coverage).includes("0–50") ? "东亚区域" : "华南区域") + " · " + coverage;
    els.metaCoverage.textContent = coverage;
    els.metaLevel.textContent = level || "—";
    els.metaRun.textContent = data.run || "—";
    els.productNote.textContent = level
      ? level + " · 每 " + intervalText + "一帧 · 共 " + state.frames.length + " 帧"
      : "每 " + intervalText + "一帧";
  }

  function fillProducts() {
    if (!els.productSelect) return;
    els.productSelect.innerHTML = state.products.map((p) =>
      '<option value="' + escapeHtml(p.id) + '">' + escapeHtml(p.name || p.id) + "</option>"
    ).join("");
  }

  function fillTimeSelect() {
    if (!els.timeSelect) return;
    els.timeSelect.innerHTML = state.frames.map((frame, i) =>
      '<option value="' + i + '">' + escapeHtml(localText(frame)) +
      " · +" + leadHours(frame) + "h</option>"
    ).join("");
  }

  function updateTransport() {
    const total = state.frames.length;
    const last = Math.max(0, total - 1);
    els.btnFirst.disabled = !total || state.idx <= 0;
    els.btnPrev.disabled = !total || state.idx <= 0;
    els.btnNext.disabled = !total || state.idx >= last;
    els.btnLast.disabled = !total || state.idx >= last;
    els.slider.disabled = !total;
    els.timeSelect.disabled = !total;

    const icon = state.playing ? "#i-pause" : "#i-play";
    els.playIcon.innerHTML = '<use href="' + icon + '"/>';
    els.btnPlay.setAttribute("aria-label", state.playing ? "暂停" : "播放");
    els.btnPlay.title = state.playing ? "暂停" : "播放";

    if (state.loop) {
      els.btnLoop.classList.add("on");
      els.btnLoop.setAttribute("aria-pressed", "true");
      els.btnLoop.querySelector("span").textContent = "开启";
    } else {
      els.btnLoop.classList.remove("on");
      els.btnLoop.setAttribute("aria-pressed", "false");
      els.btnLoop.querySelector("span").textContent = "关闭";
    }
  }

  function updateTimeline() {
    const total = state.frames.length;
    const last = Math.max(0, total - 1);
    const frame = state.frames[state.idx];
    els.slider.max = String(last);
    els.slider.value = String(state.idx);
    const pct = last ? (state.idx / last) * 100 : 0;
    els.slider.style.setProperty("--p", pct + "%");
    els.curTime.textContent = frame ? frame.t : "--:--";
    els.frameIdx.textContent = total ? (state.idx + 1) + " / " + total : "0 / 0";
    els.axisStart.textContent = state.frames.length ? shortLocal(state.frames[0]) : "—";
    els.axisEnd.textContent = state.frames.length ? shortLocal(state.frames[state.frames.length - 1]) : "—";
    if (els.timeSelect && total) els.timeSelect.value = String(state.idx);
    els.requestInfo.textContent = frame
      ? localText(frame) + " 北京时间 · 起报 " + state.run + " · +" + leadHours(frame) + "h"
      : "—";
    updateTransport();
  }

  function setPlaceholder(text, gone) {
    if (!els.placeholder) return;
    els.placeholder.textContent = text || "";
    els.placeholder.classList.toggle("gone", Boolean(gone));
  }

  function showError(message) {
    if (!els.errorBox) return;
    els.errorBox.textContent = message;
    els.errorBox.hidden = false;
    setPlaceholder("", true);
    setImageStatus("加载失败", "err");
    setStatus("加载失败", "err");
  }

  function hideError() {
    if (!els.errorBox) return;
    els.errorBox.hidden = true;
    els.errorBox.textContent = "";
  }

  function resetImage() {
    state.hasImage = false;
    state.currentStamp = "";
    state.active = "a";
    els.layerA.classList.remove("show");
    els.layerB.classList.remove("show");
    els.layerA.removeAttribute("src");
    els.layerB.removeAttribute("src");
    hideError();
    setPlaceholder("正在请求预报图像…", false);
    progress(8);
  }

  function setLayerImage(frame, forceReload) {
    if (!frame) return;
    if (!forceReload && state.hasImage && state.currentStamp === frame.s) {
      updateTimeline();
      return;
    }

    const token = ++state.reqSeq;
    const next = state.active === "a" ? els.layerB : els.layerA;
    const current = state.active === "a" ? els.layerA : els.layerB;
    const url = imgUrl(frame) + (forceReload ? "&_r=" + Date.now() : "");

    startLoadingBar();
    setImageStatus("加载中", "warn");
    setStatus("加载中", "warn");
    hideError();

    next.classList.remove("show");
    next.onload = function () {
      if (token !== state.reqSeq) return;
      state.active = state.active === "a" ? "b" : "a";
      state.hasImage = true;
      state.currentStamp = frame.s;
      next.classList.add("show");
      current.classList.remove("show");
      setPlaceholder("", true);
      setImageStatus("已加载", "ok");
      setStatus("AIFS · 已连接", "ok");
      finishLoadingBar();
      updateTimeline();
      preloadNearby();
    };
    next.onerror = function () {
      if (token !== state.reqSeq) return;
      if (!state.hasImage) {
        setPlaceholder("图像加载失败", false);
        showError("图像加载失败，请稍后刷新重试。");
      } else {
        setImageStatus("加载失败", "err");
        setStatus("图像加载失败", "err");
        toast("图像加载失败，保留上一帧");
      }
      progress(0);
    };

    next.src = url;
    if (next.complete && next.naturalWidth) {
      next.onload();
    }
  }

  function preloadNearby() {
    for (let i = 1; i <= 3; i += 1) {
      const frame = state.frames[state.idx + i];
      if (!frame) break;
      const preload = new Image();
      preload.src = imgUrl(frame);
    }
  }

  function showFrame(index, forceReload) {
    if (!state.frames.length) return;
    const next = Math.max(0, Math.min(state.frames.length - 1, Number(index) || 0));
    state.idx = next;
    const frame = state.frames[next];
    updateTimeline();
    setLayerImage(frame, Boolean(forceReload));
    if (state.playing && next === state.frames.length - 1 && !state.loop) {
      pause();
    }
  }

  function frameForStamp(stamp) {
    if (!stamp) return -1;
    return state.frames.findIndex((frame) => frame.s === stamp);
  }

  function selectProduct(id) {
    const product = state.products.find((p) => p.id === id) || state.products[0];
    if (!product) return;
    state.product = product.id;
    state.productMeta = product;
    els.productSelect.value = product.id;
  }

  async function loadProduct(id, options) {
    const opts = options || {};
    const target = state.products.find((p) => p.id === id) || state.products[0];
    if (!target) return;

    const token = ++state.loadSeq;
    const previousStamp = opts.keepStamp || "";
    const isNewProduct = state.product !== target.id;

    pause();
    state.product = target.id;
    state.productMeta = target;
    els.productSelect.value = target.id;

    if (isNewProduct || opts.resetImage !== false) {
      resetImage();
    }
    setImageStatus("加载中", "warn");
    setStatus("加载中", "warn");
    startLoadingBar();

    try {
      const data = await api("/api/frames?p=" + encodeURIComponent(target.id));
      if (token !== state.loadSeq) return;

      state.frames = Array.isArray(data.frames) ? data.frames : [];
      state.run = data.run || "";
      state.runUtc = parseRun(data.run);
      state.generated = data.generated || "";
      state.productMeta = Object.assign({}, target, data);
      updateMeta(data);
      fillTimeSelect();

      let index = previousStamp ? frameForStamp(previousStamp) : -1;
      if (index < 0) index = state.frames.length - 1;
      showFrame(index, Boolean(opts.forceReload));

      if (!opts.silent) {
        // 首次加载不弹 toast，避免遮挡页面
      }
      setStatus("AIFS · 已连接", "ok");

      const url = new URL(window.location.href);
      url.searchParams.set("p", target.id);
      history.replaceState(null, "", url.pathname + "?" + url.searchParams.toString());
    } catch (error) {
      if (token !== state.loadSeq) return;
      console.error(error);
      showError(error.message || "数据加载失败");
      setStatus("连接失败", "err");
      setImageStatus("加载失败", "err");
      progress(0);
    }
  }

  function play() {
    if (state.playing || state.frames.length < 2) return;
    state.playing = true;
    updateTransport();
    scheduleTick();
  }

  function pause() {
    state.playing = false;
    clearTimeout(state.timer);
    state.timer = null;
    updateTransport();
  }

  function scheduleTick() {
    clearTimeout(state.timer);
    if (!state.playing) return;
    const delay = Math.max(220, Math.round(1000 / state.speed));
    state.timer = setTimeout(() => {
      if (!state.playing) return;
      if (state.idx >= state.frames.length - 1) {
        if (state.loop) {
          showFrame(0);
          scheduleTick();
        } else {
          pause();
        }
        return;
      }
      showFrame(state.idx + 1);
      scheduleTick();
    }, delay);
  }

  function togglePlay() {
    if (state.playing) pause();
    else play();
  }

  function download() {
    const url = downloadUrl();
    if (!url) {
      toast("当前没有可下载的图像");
      return;
    }
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("正在下载当前图像");
  }

  async function refreshCurrent() {
    if (!state.product) return;
    const stamp = state.frames[state.idx] ? state.frames[state.idx].s : "";
    await loadProduct(state.product, {
      keepStamp: stamp,
      resetImage: false,
      silent: true,
      forceReload: true,
    });
    toast("图像已刷新");
  }

  async function applySelection() {
    const id = els.productSelect.value;
    if (!id) return;
    if (id !== state.product) {
      await loadProduct(id, { resetImage: true });
      toast("已切换产品");
    } else {
      await refreshCurrent();
    }
  }

  /* ---------------- 缩放与拖动 ---------------- */

  function clampPan() {
    const rect = els.viewer.getBoundingClientRect();
    const maxX = Math.max(0, (rect.width * (state.zoom - 1)) / 2);
    const maxY = Math.max(0, (rect.height * (state.zoom - 1)) / 2);
    state.panX = Math.max(-maxX, Math.min(maxX, state.panX));
    state.panY = Math.max(-maxY, Math.min(maxY, state.panY));
  }

  function applyZoom() {
    state.zoom = Math.max(1, Math.min(4, state.zoom));
    if (state.zoom === 1) {
      state.panX = 0;
      state.panY = 0;
    }
    clampPan();
    els.stage.style.transform = "translate(" + state.panX + "px," + state.panY + "px) scale(" + state.zoom + ")";
  }

  function zoomBy(delta) {
    state.zoom += delta;
    applyZoom();
  }

  function resetZoom() {
    state.zoom = 1;
    state.panX = 0;
    state.panY = 0;
    applyZoom();
  }

  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      const promise = els.viewerCard.requestFullscreen && els.viewerCard.requestFullscreen();
      if (promise && promise.catch) promise.catch(() => {});
    } else {
      document.exitFullscreen();
    }
  }

  /* ---------------- 事件绑定 ---------------- */

  function bind() {
    els.btnPlay.addEventListener("click", togglePlay);
    els.btnFirst.addEventListener("click", () => { pause(); showFrame(0); });
    els.btnPrev.addEventListener("click", () => { pause(); showFrame(state.idx - 1); });
    els.btnNext.addEventListener("click", () => { pause(); showFrame(state.idx + 1); });
    els.btnLast.addEventListener("click", () => { pause(); showFrame(state.frames.length - 1); });

    els.slider.addEventListener("input", (event) => {
      pause();
      showFrame(Number(event.target.value));
    });

    els.timeSelect.addEventListener("change", () => {
      pause();
      showFrame(Number(els.timeSelect.value));
    });

    els.speedSelect.addEventListener("change", () => {
      state.speed = Number(els.speedSelect.value) || 1;
      if (state.playing) scheduleTick();
    });

    els.btnLoop.addEventListener("click", () => {
      state.loop = !state.loop;
      updateTransport();
      toast(state.loop ? "循环播放已开启" : "循环播放已关闭");
    });

    els.productSelect.addEventListener("change", () => {
      const product = state.products.find((p) => p.id === els.productSelect.value);
      if (product) {
        els.productNote.textContent = (product.level || "") + " · 点击“获取 / 刷新图像”加载";
      }
    });

    [els.btnRefresh, els.btnHeroRefresh].forEach((button) => {
      button.addEventListener("click", () => applySelection());
    });
    els.btnApply.addEventListener("click", () => applySelection());
    els.btnDownload.addEventListener("click", download);
    els.btnDownloadIcon.addEventListener("click", download);
    els.btnFullscreen.addEventListener("click", toggleFullscreen);

    els.btnZoomIn.addEventListener("click", () => zoomBy(0.25));
    els.btnZoomOut.addEventListener("click", () => zoomBy(-0.25));
    els.btnZoomReset.addEventListener("click", resetZoom);

    els.viewer.addEventListener("wheel", (event) => {
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 0.2 : -0.2);
    }, { passive: false });

    let drag = null;
    els.viewer.addEventListener("pointerdown", (event) => {
      if (event.target.closest("button")) return;
      drag = {
        x: event.clientX,
        y: event.clientY,
        panX: state.panX,
        panY: state.panY,
      };
      els.viewer.classList.add("grabbing");
      if (els.viewer.setPointerCapture) els.viewer.setPointerCapture(event.pointerId);
    });
    els.viewer.addEventListener("pointermove", (event) => {
      if (!drag) return;
      state.panX = drag.panX + event.clientX - drag.x;
      state.panY = drag.panY + event.clientY - drag.y;
      applyZoom();
    });
    const endDrag = () => {
      drag = null;
      els.viewer.classList.remove("grabbing");
    };
    els.viewer.addEventListener("pointerup", endDrag);
    els.viewer.addEventListener("pointercancel", endDrag);
    els.viewer.addEventListener("dblclick", (event) => {
      if (event.target.closest("button")) return;
      resetZoom();
    });

    document.addEventListener("fullscreenchange", () => {
      const active = Boolean(document.fullscreenElement);
      els.btnFullscreen.innerHTML = '<svg><use href="#i-' + (active ? "shrink" : "expand") + '"/></svg>';
    });

    document.addEventListener("keydown", (event) => {
      const tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "select" || tag === "textarea") return;
      if (event.code === "Space") {
        event.preventDefault();
        togglePlay();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        pause();
        showFrame(state.idx - 1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        pause();
        showFrame(state.idx + 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        pause();
        showFrame(0);
      } else if (event.key === "End") {
        event.preventDefault();
        pause();
        showFrame(state.frames.length - 1);
      } else if (event.key === "f" || event.key === "F") {
        toggleFullscreen();
      } else if (event.key === "r" || event.key === "R") {
        refreshCurrent();
      } else if (event.key === "0") {
        resetZoom();
      } else if (event.key === "+" || event.key === "=") {
        zoomBy(0.25);
      } else if (event.key === "-") {
        zoomBy(-0.25);
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pause();
    });
  }

  async function init() {
    bind();
    updateClock();
    startClock();
    setStatus("连接中", "warn");
    setImageStatus("等待加载", "warn");
    startLoadingBar();

    try {
      const meta = await api("/api/meta");
      state.products = Array.isArray(meta.products) ? meta.products : [];
      fillProducts();
      if (!state.products.length) throw new Error("没有可用的预报产品");

      const params = new URLSearchParams(window.location.search);
      const requested = params.get("p");
      const initial = state.products.some((p) => p.id === requested) ? requested : DEFAULT_PRODUCT;
      const exists = state.products.some((p) => p.id === initial);
      selectProduct(exists ? initial : state.products[0].id);
      await loadProduct(state.product, { resetImage: true });
      if (params.get("play") === "1") play();
    } catch (error) {
      console.error(error);
      showError(error.message || "初始化失败");
      setStatus("连接失败", "err");
      setImageStatus("加载失败", "err");
    }

    state.autoTimer = setInterval(() => {
      if (!document.hidden && state.product) {
        const stamp = state.frames[state.idx] ? state.frames[state.idx].s : "";
        loadProduct(state.product, {
          keepStamp: stamp,
          resetImage: false,
          silent: true,
        });
      }
    }, AUTO_REFRESH_MS);
  }

  function startClock() {
    updateClock();
    setInterval(updateClock, 1000);
  }

  init();
})();
