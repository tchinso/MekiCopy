import { env, pipeline } from "/assets/transformers.min.js";

let generator = null;
let socket = null;
let config = null;
let activeDevice = null;
let activeDeviceDetail = null;
let selectedWebGpuAdapter = null;
let warning = null;
let lastProgressPercent = -1;
let lastProgressSentAt = 0;

const statusEl = document.getElementById("status");
const progressBarEl = document.getElementById("progress-bar");
const progressTextEl = document.getElementById("progress-text");
const progressFiles = new Map();
let modelBackupCacheInstalled = false;

async function sendClientLog(level, stage, message) {
  if (level !== "error" && level !== "fatal" && !config?.debugLog) {
    return;
  }
  try {
    await fetch("/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, stage, message: String(message) }),
      keepalive: true,
    });
  } catch (_err) {
    // The HYTrans server may already be shutting down.
  }
}

function sendToServer(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

function setStatus(message, notifyServer = true) {
  statusEl.textContent = message;
  console.log("[HYTrans Worker]", message);
  void sendClientLog("debug", "status", message);
  if (notifyServer) {
    sendToServer({ type: "loading", message });
  }
}

function compactErrorDetail(error) {
  const detail = error?.message ?? String(error ?? "unknown error");
  return String(detail).replace(/\s+/g, " ").trim().slice(0, 300);
}

function clearSelectedWebGpuAdapter() {
  selectedWebGpuAdapter = null;
  if (env.backends?.onnx?.webgpu) {
    // Do not leave a rejected adapter attached if this page retries its model
    // initialization. ONNX Runtime Web otherwise reuses it silently.
    env.backends.onnx.webgpu.adapter = undefined;
  }
}

function selectWasmFallback(reason) {
  clearSelectedWebGpuAdapter();
  activeDeviceDetail = null;
  warning = `WebGPU를 사용할 수 없어 CPU(WASM)로 실행합니다: ${reason}`;
  console.warn("[HYTrans Worker]", warning);
  // The warning is included in /ready even when debug logging is off.  Keep a
  // full diagnostic in the optional worker debug log without creating an
  // error-log entry for a computer that simply has no WebGPU-capable adapter.
  void sendClientLog("debug", "webgpu_fallback", warning);
  return "wasm";
}

function inspectWebGpuAdapter(adapter) {
  // GPUAdapterInfo is intentionally privacy-limited, so every field may be
  // empty.  Its fallback flag is the reliable signal when Chromium exposes it;
  // known software-renderer names are retained as a diagnostic for older
  // Chromium builds that did not expose that flag yet.
  const info = adapter?.info || null;
  const isFallback = info?.isFallbackAdapter ?? adapter?.isFallbackAdapter;
  const details = [
    info?.vendor,
    info?.architecture,
    info?.device,
    info?.description,
  ]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  const detail = details.join(" / ");
  const looksSoftware = /swiftshader|software|warp|llvmpipe|lavapipe|basic render/i.test(detail);
  return {
    detail: detail || "browser did not expose adapter details",
    software: isFallback === true || looksSoftware,
  };
}

window.addEventListener("error", (event) => {
  const detail = `${event.message || "worker window error"}\n${event.filename || ""}:${event.lineno || 0}:${event.colno || 0}`;
  void sendClientLog("error", "window_error", detail);
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  const detail = reason?.stack || reason?.message || String(reason);
  void sendClientLog("error", "unhandled_rejection", detail);
});

function setProgress(percent, message) {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  progressBarEl.style.width = `${safePercent}%`;
  progressTextEl.textContent = message ? `${safePercent}% - ${message}` : `${safePercent}%`;
}

function resetProgress(message = "모델 다운로드 준비 중...") {
  progressFiles.clear();
  lastProgressPercent = -1;
  lastProgressSentAt = 0;
  setProgress(0, message);
}

function shortFileName(file) {
  const parts = String(file).split("/");
  return parts[parts.length - 1] || String(file);
}

function requestUrl(request) {
  if (typeof request === "string") {
    return request;
  }
  return request?.url || String(request || "");
}

function isModelRequest(url) {
  return Boolean(config?.modelId && String(url).includes(config.modelId));
}

function modelRelativePath(url) {
  const pathname = decodeURIComponent(new URL(url, location.href).pathname)
    .replaceAll("\\", "/")
    .split("/")
    .filter(Boolean);
  const modelParts = String(config?.modelId || "").split("/").filter(Boolean);
  for (let index = 0; index <= pathname.length - modelParts.length; index += 1) {
    if (!modelParts.every((part, offset) => pathname[index + offset] === part)) {
      continue;
    }
    let remainder = pathname.slice(index + modelParts.length);
    if (["resolve", "raw"].includes(remainder[0]) && remainder.length >= 3) {
      remainder = remainder.slice(2);
    }
    return remainder.join("/");
  }
  return "";
}

function expectedModelFileSize(url) {
  const relative = modelRelativePath(url);
  return Number(config?.modelFiles?.[relative] || 0);
}

function completeResponseSize(response) {
  const contentRange = response.headers.get("content-range") || "";
  const rangeMatch = contentRange.match(/^bytes\s+(\d+)-(\d+)\/(\d+)$/i);
  if (rangeMatch) {
    const start = Number(rangeMatch[1]);
    const end = Number(rangeMatch[2]);
    const total = Number(rangeMatch[3]);
    return start === 0 && end + 1 === total ? total : 0;
  }
  return response.status === 200
    ? Number(response.headers.get("content-length") || 0)
    : 0;
}

function formatBytes(bytes) {
  if (!bytes) {
    return "";
  }
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function overallProgress() {
  let loaded = 0;
  let total = 0;

  for (const info of progressFiles.values()) {
    loaded += info.loaded || 0;
    total += info.total || 0;
  }

  return total > 0 ? Math.min(100, (loaded / total) * 100) : 0;
}

function shouldSendProgress(percent, status) {
  const now = Date.now();
  if (status === "initiate" || status === "done") {
    lastProgressPercent = percent;
    lastProgressSentAt = now;
    return true;
  }
  if (percent >= lastProgressPercent + 5 || now - lastProgressSentAt > 2000) {
    lastProgressPercent = percent;
    lastProgressSentAt = now;
    return true;
  }
  return false;
}

function onProgress(progress) {
  if (!progress || !progress.file) {
    return;
  }

  const file = progress.file;
  if (!progressFiles.has(file)) {
    progressFiles.set(file, { loaded: 0, total: 0 });
  }

  const info = progressFiles.get(file);
  if (progress.status === "initiate") {
    info.loaded = 0;
    info.total = progress.total || 0;
  } else if (progress.status === "progress") {
    info.loaded = progress.loaded || 0;
    info.total = progress.total || info.total || 0;
  } else if (progress.status === "done") {
    info.total = info.total || progress.total || progress.loaded || 0;
    info.loaded = info.total || progress.loaded || 0;
  }

  const percent = Math.round(overallProgress());
  const fileLabel = shortFileName(file);
  const byteLabel = info.total ? ` (${formatBytes(info.loaded)} / ${formatBytes(info.total)})` : "";
  const loadingVerb = config?.modelMode === "local" ? "로컬 모델 로드 중" : "다운로드 중";
  const doneVerb = config?.modelMode === "local" ? "로컬 모델 로드 완료" : "다운로드 완료";
  const displayMessage =
    progress.status === "done"
      ? `${doneVerb}: ${fileLabel}`
      : `${loadingVerb}: ${fileLabel}${byteLabel}`;

  setProgress(percent, displayMessage);
  if (shouldSendProgress(percent, progress.status)) {
    setStatus(`${displayMessage} (${percent}%)`);
  }
}

function cleanTranslationOutput(text) {
  return text
    .replace(/^assistant\s*[:：]?\s*/i, "")
    .trimStart();
}

function buildPrompt(inputText) {
  const target = config?.target || "Korean";
  const template =
    config?.promptTemplate ||
    "Translate the following segment into {target}, without additional explanation.\n\n{text}";
  return String(template)
    .replaceAll("{target}", () => target)
    .replaceAll("{text}", () => inputText);
}

function extractGeneratedText(result) {
  const item = Array.isArray(result) ? result[0] : result;
  const generated = item?.generated_text ?? item?.text ?? "";
  if (Array.isArray(generated)) {
    const last = generated.at(-1);
    return typeof last === "string" ? last : last?.content || last?.text || "";
  }
  if (generated && typeof generated === "object") {
    return generated.content || generated.text || "";
  }
  return typeof generated === "string" ? generated : "";
}

async function loadConfig() {
  const response = await fetch("/config", { cache: "no-store" });
  if (!response.ok) {
    throw new Error("failed to load /config");
  }
  return await response.json();
}

async function prepareLocalModel() {
  if (config.modelMode === "local") {
    return;
  }

  resetProgress("모델 다운로드 준비 중...");
  const startResponse = await fetch("/model/prepare", { method: "POST" });
  if (!startResponse.ok) {
    throw new Error(`model download start failed: HTTP ${startResponse.status}`);
  }

  while (true) {
    const response = await fetch("/model/status", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`model download status failed: HTTP ${response.status}`);
    }
    const status = await response.json();
    if (status.state === "READY") {
      setProgress(100, "다운로드 완료");
      setStatus("모델 다운로드 완료. 로컬 모델을 준비합니다...");
      config = await loadConfig();
      if (config.modelMode !== "local") {
        throw new Error("downloaded model was not recognized as a complete local model");
      }
      return;
    }
    if (status.state === "ERROR") {
      throw new Error(status.error || "model download failed");
    }

    const loaded = Number(status.downloadedBytes || 0);
    const total = Number(status.totalBytes || 0);
    const percent = total > 0 ? Math.min(99, Math.round((loaded / total) * 100)) : 0;
    const file = shortFileName(status.currentFile || "모델 파일");
    const byteLabel = total ? ` (${formatBytes(loaded)} / ${formatBytes(total)})` : "";
    const message = `다운로드 중: ${file}${byteLabel}`;
    setProgress(percent, message);
    setStatus(`${message} (${percent}%)`);
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
}

async function getServerModelCacheStatus(url) {
  const response = await fetch(`/model-cache-status?url=${encodeURIComponent(url)}`, {
    cache: "no-store",
  });
  if (!response.ok) {
    return { exists: false };
  }
  return await response.json();
}

async function uploadModelResponseToServer(url, response) {
  if (!response?.ok || !isModelRequest(url)) {
    return;
  }

  const status = await getServerModelCacheStatus(url);
  const responseSize = completeResponseSize(response);
  const expectedSize = expectedModelFileSize(url);
  if (response.status === 206 && responseSize <= 0) {
    // Transformers.js may probe a large external-data file with a byte range.
    // A partial response must never replace a complete server-side model file.
    return;
  }
  if (expectedSize > 0 && responseSize > 0 && responseSize !== expectedSize) {
    console.warn("refusing to cache an unexpected model file size", {
      url,
      expectedSize,
      responseSize,
    });
    return;
  }
  if (status.exists && Number(status.size || 0) > 0) {
    // A server-cache file is published only after an atomic, complete upload.
    // Some browser cache responses omit Content-Length; treating that as a
    // mismatch rewrote every already-cached model file on each worker launch.
    if (responseSize <= 0 || Number(status.size) === responseSize) {
      return;
    }
  }

  const fileLabel = shortFileName(new URL(url, location.href).pathname);
  setStatus(`모델 파일 백업 중: ${fileLabel}`);

  let upload = null;
  if (response.body) {
    try {
      upload = await fetch(`/model-cache?url=${encodeURIComponent(url)}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
        },
        body: response.clone().body,
        duplex: "half",
      });
    } catch (err) {
      console.warn("streaming model backup failed, retrying with Blob:", err);
    }
  }

  if (!upload) {
    const blob = await response.clone().blob();
    if (status.exists && Number(status.size || 0) === blob.size) {
      return;
    }
    upload = await fetch(`/model-cache?url=${encodeURIComponent(url)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
      },
      body: blob,
    });
  }

  if (!upload.ok) {
    throw new Error(`model backup failed: HTTP ${upload.status}`);
  }
  setStatus(`모델 파일 백업 완료: ${fileLabel}`);
}

async function storeBrowserModelResponse(request, response) {
  if (!("caches" in window) || !response?.ok || response.status === 206) {
    return;
  }
  try {
    const cache = await caches.open(env.cacheKey || "transformers-cache");
    await cache.put(request, response.clone());
  } catch (err) {
    // The server-side cache remains the durable fallback when browser quota is
    // too small for the 1.4 GB external-data file.
    console.warn("browser model cache write failed:", err);
  }
}

async function findBrowserCachedModelResponse(request) {
  if (!("caches" in window)) {
    return undefined;
  }

  const names = await caches.keys();
  const preferredName = env.cacheKey || "transformers-cache";
  const orderedNames = [
    preferredName,
    ...names.filter((name) => name !== preferredName),
  ].filter((name, index, all) => all.indexOf(name) === index);

  for (const name of orderedNames) {
    try {
      const cache = await caches.open(name);
      const response = await cache.match(request);
      if (response?.ok) {
        return response;
      }
    } catch (err) {
      console.warn("browser cache lookup failed:", err);
    }
  }

  return undefined;
}

async function syncBrowserModelCacheToServer() {
  if (!("caches" in window)) {
    return;
  }

  const names = await caches.keys();
  for (const name of names) {
    const cache = await caches.open(name);
    const requests = await cache.keys();
    for (const request of requests) {
      const url = requestUrl(request);
      if (!isModelRequest(url)) {
        continue;
      }
      const response = await cache.match(request);
      if (response?.ok) {
        await uploadModelResponseToServer(url, response);
      }
    }
  }
}

function installModelBackupCache() {
  if (modelBackupCacheInstalled) {
    return;
  }

  env.useCustomCache = true;
  env.customCache = {
    async match(request) {
      const url = requestUrl(request);
      if (!isModelRequest(url)) {
        return undefined;
      }

      try {
        const response = await fetch(`/model-cache?url=${encodeURIComponent(url)}`, {
          cache: "no-store",
        });
        if (response.ok) {
          return response;
        }
      } catch (err) {
        console.warn("server model cache lookup failed:", err);
      }

      const browserResponse = await findBrowserCachedModelResponse(request);
      if (browserResponse?.ok) {
        try {
          await uploadModelResponseToServer(url, browserResponse);
        } catch (err) {
          console.warn("browser cache backup failed:", err);
        }
        return browserResponse;
      }

      return undefined;
    },

    async put(request, response) {
      const sourceUrl = requestUrl(request);
      const url = isModelRequest(sourceUrl) ? sourceUrl : response?.url || sourceUrl;
      if (!isModelRequest(url)) {
        return;
      }
      const serverCopy = response.clone();
      await storeBrowserModelResponse(request, response);
      try {
        await uploadModelResponseToServer(url, serverCopy);
      } catch (err) {
        console.warn("server model backup failed:", err);
      }
    },
  };

  modelBackupCacheInstalled = true;
}

function setupTransformersEnv(runtimeConfig) {
  env.allowLocalModels = true;
  env.localModelPath = "/models/";
  env.useWasmCache = true;

  if (runtimeConfig.modelMode === "local") {
    env.allowRemoteModels = false;
  } else {
    env.allowRemoteModels = true;
    installModelBackupCache();
  }

  if (runtimeConfig.hasLocalWasm && env.backends?.onnx?.wasm) {
    // Transformers.js 4.2.0 with ONNX Runtime Web 1.27 selects the asyncify
    // runtime for Chromium. Keep
    // the pair explicit so its preloader/cache never falls back to the CDN.
    env.backends.onnx.wasm.wasmPaths = {
      mjs: "/assets/wasm/ort-wasm-simd-threaded.asyncify.mjs",
      wasm: "/assets/wasm/ort-wasm-simd-threaded.asyncify.wasm",
    };
  }

  // Prefer the discrete adapter on dual-GPU laptops. Transformers.js currently
  // applies the same hint, but set it here as part of this worker's contract so
  // an upstream default change cannot quietly move inference to a
  // power-saving adapter.
  if (env.backends?.onnx?.webgpu) {
    env.backends.onnx.webgpu.powerPreference = "high-performance";
    env.backends.onnx.webgpu.forceFallbackAdapter = false;
    clearSelectedWebGpuAdapter();
  }
}

async function createPipeline(device) {
  return await pipeline("text-generation", config.modelId, {
    dtype: config.dtype,
    device,
    revision: config.revision,
    progress_callback: onProgress,
  });
}

async function preferredDevice() {
  if (!navigator.gpu) {
    return selectWasmFallback("navigator.gpu를 사용할 수 없습니다");
  }
  try {
    const adapter = await navigator.gpu.requestAdapter({
      powerPreference: "high-performance",
      forceFallbackAdapter: false,
    });
    if (!adapter) {
      return selectWasmFallback("WebGPU 어댑터를 찾지 못했습니다");
    }
    const adapterInfo = inspectWebGpuAdapter(adapter);
    if (adapterInfo.software) {
      return selectWasmFallback(
        `하드웨어 대신 소프트웨어 WebGPU 어댑터가 선택되었습니다 (${adapterInfo.detail})`,
      );
    }
    if (!env.backends?.onnx?.webgpu) {
      return selectWasmFallback("Transformers.js WebGPU 백엔드 설정을 찾지 못했습니다");
    }
    // Transformers.js/ONNX Runtime would otherwise request a second adapter
    // while constructing the pipeline. Reuse the adapter that was checked
    // above so the ready state cannot claim a hardware GPU while inference is
    // actually performed by a different software or low-power adapter.
    selectedWebGpuAdapter = adapter;
    env.backends.onnx.webgpu.adapter = selectedWebGpuAdapter;
    activeDeviceDetail = adapterInfo.detail;
    void sendClientLog("debug", "webgpu_adapter", activeDeviceDetail);
    return "webgpu";
  } catch (err) {
    return selectWasmFallback(
      `WebGPU 어댑터 확인 실패 (${compactErrorDetail(err)})`,
    );
  }
}

async function createGeneratorWithFallback() {
  // navigator.gpu can exist even when no adapter is available (notably in
  // headless/remote sessions). Starting a failed WebGPU pipeline first can
  // leave the model session cached with that provider, preventing a clean
  // WASM retry, so probe the adapter before constructing the pipeline.
  const device = await preferredDevice();
  const loadMessage =
    config.modelMode === "local"
      ? `${config.modelId} 로컬 모델 로드 중...`
      : `${config.modelId} 자동 다운로드를 시작합니다...`;
  resetProgress(loadMessage);

  try {
    setStatus(`모델을 ${device}로 불러오는 중...`);
    const pipe = await createPipeline(device);
    activeDevice = device;
    return pipe;
  } catch (err) {
    if (device !== "webgpu") {
      throw err;
    }
    const detail = compactErrorDetail(err);
    warning = `WebGPU 모델 초기화에 실패해 CPU(WASM)로 전환했습니다: ${detail}`;
    clearSelectedWebGpuAdapter();
    activeDeviceDetail = null;
    console.warn("WebGPU failed, fallback to wasm:", err);
    // An adapter was found but ONNX Runtime could not create its WebGPU
    // sessions. Persist this distinct failure for a useful bug report instead
    // of silently appearing as an ordinary CPU-only configuration.
    void sendClientLog("error", "webgpu_pipeline_fallback", warning);
    setStatus("WebGPU 로드 실패. CPU(wasm)로 다시 시도합니다...");
    const pipe = await createPipeline("wasm");
    activeDevice = "wasm";
    return pipe;
  }
}

function announceReady() {
  sendToServer({
    type: "ready",
    device: activeDevice,
    deviceDetail: activeDeviceDetail,
    model: config.modelId,
    dtype: config.dtype,
    modelMode: config.modelMode,
    warning,
  });
  setProgress(100, "모델 준비 완료");
  setStatus(`ready: ${activeDevice}`, false);
}

async function handleTranslateRequest(req) {
  if (!generator) {
    sendToServer({
      type: "error",
      id: req.id,
      message: "model is not ready",
    });
    return;
  }

  try {
    const result = await generator([{ role: "user", content: buildPrompt(req.text) }], {
      max_new_tokens: req.max_new_tokens ?? config.maxNewTokens ?? 2048,
      do_sample: false,
    });
    const rawText = extractGeneratedText(result);
    const translated = cleanTranslationOutput(rawText).trim();
    if (!translated) {
      throw new Error("translation model returned an empty result");
    }
    sendToServer({
      type: "result",
      id: req.id,
      text: translated,
    });
  } catch (err) {
    sendToServer({
      type: "error",
      id: req.id,
      message: err?.message ?? String(err),
    });
  }
}

function connectWebSocket() {
  return new Promise((resolve, reject) => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    let opened = false;

    socket = new WebSocket(`${protocol}//${location.host}/ws/worker`);

    socket.onopen = () => {
      opened = true;
      setStatus("HYTrans에 연결되었습니다. 모델을 준비합니다.");
      resolve();
    };

    socket.onmessage = async (event) => {
      const req = JSON.parse(event.data);
      if (req.type !== "translate") {
        return;
      }
      await handleTranslateRequest(req);
    };

    socket.onclose = () => {
      setStatus("WebSocket closed. HYTrans 연결이 끊겼습니다.", false);
      if (!opened) {
        reject(new Error("websocket closed before connection"));
      }
    };

    socket.onerror = () => {
      setStatus("WebSocket error", false);
      if (!opened) {
        reject(new Error("failed to connect websocket"));
      }
    };
  });
}

async function main() {
  try {
    setStatus("설정을 불러오는 중...", false);
    config = await loadConfig();
    await connectWebSocket();
    await prepareLocalModel();
    setupTransformersEnv(config);
    generator = await createGeneratorWithFallback();
    announceReady();
  } catch (err) {
    console.error(err);
    void sendClientLog("fatal", "main", err?.stack || err?.message || String(err));
    setStatus(`ERROR: ${err?.message ?? String(err)}`);
    sendToServer({
      type: "fatal",
      message: err?.message ?? String(err),
    });
  }
}

main();
