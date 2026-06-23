import { env, pipeline } from "/assets/transformers.min.js";

let generator = null;
let socket = null;
let config = null;
let activeDevice = null;
let warning = null;
let lastProgressPercent = -1;
let lastProgressSentAt = 0;

const CLOSE_WARNING =
  "이 HYTransWorker 창을 닫으면 HYTrans와 연결이 끊겨 번역이 중단될 수 있습니다. 닫는 대신 최소화하는 것을 권장합니다. 정말 닫을까요?";

const statusEl = document.getElementById("status");
const progressBarEl = document.getElementById("progress-bar");
const progressTextEl = document.getElementById("progress-text");
const progressFiles = new Map();
let modelBackupCacheInstalled = false;

function sendToServer(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

function setStatus(message, notifyServer = true) {
  statusEl.textContent = message;
  console.log("[HYTrans Worker]", message);
  if (notifyServer) {
    sendToServer({ type: "loading", message });
  }
}

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
  const loadingVerb = config?.modelMode === "local" ? "로컬 모델 읽는 중" : "다운로드 중";
  const doneVerb = config?.modelMode === "local" ? "로컬 모델 읽기 완료" : "다운로드 완료";
  const message =
    progress.status === "done"
      ? `다운로드 완료: ${fileLabel}`
      : `다운로드 중: ${fileLabel}${byteLabel}`;

  const displayMessage =
    progress.status === "done"
      ? `${doneVerb}: ${fileLabel}`
      : `${loadingVerb}: ${fileLabel}${byteLabel}`;

  setProgress(percent, displayMessage);
  if (shouldSendProgress(percent, progress.status)) {
    setStatus(`${displayMessage} (${percent}%)`);
  }
}

function installCloseWarning() {
  window.addEventListener("beforeunload", (event) => {
    event.preventDefault();
    event.returnValue = CLOSE_WARNING;
    return CLOSE_WARNING;
  });
}

function cleanTranslationOutput(text) {
  return text
    .replace(/^assistant\s*[:：]?\s*/i, "")
    .trimStart();
}

function buildPrompt(inputText) {
  const target = config?.target || "Korean";
  return `Translate the following segment into ${target}, without additional explanation.\n\n${inputText}`;
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

  if (runtimeConfig.modelMode === "local") {
    env.allowRemoteModels = false;
  } else {
    env.allowRemoteModels = true;
    installModelBackupCache();
  }

  if (runtimeConfig.hasLocalWasm && env.backends?.onnx?.wasm) {
    env.backends.onnx.wasm.wasmPaths = "/assets/wasm/";
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
    return "wasm";
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    return adapter ? "webgpu" : "wasm";
  } catch (err) {
    console.warn("WebGPU adapter probe failed, using wasm:", err);
    return "wasm";
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
      ? `${config.modelId} 로컬 모델을 불러오는 중...`
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
    console.warn("WebGPU failed, fallback to wasm:", err);
    warning = "webgpu failed, using wasm fallback";
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
  installCloseWarning();

  try {
    setStatus("설정을 불러오는 중...", false);
    config = await loadConfig();
    setupTransformersEnv(config);
    await connectWebSocket();
    generator = await createGeneratorWithFallback();
    if (config.modelMode !== "local") {
      await syncBrowserModelCacheToServer();
    }
    announceReady();
  } catch (err) {
    console.error(err);
    setStatus(`ERROR: ${err?.message ?? String(err)}`);
    sendToServer({
      type: "fatal",
      message: err?.message ?? String(err),
    });
  }
}

main();
