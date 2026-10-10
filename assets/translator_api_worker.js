// Browser Translator API runs in this document, not in a Web Worker.
// API shape: https://learn.microsoft.com/en-us/microsoft-edge/web-platform/translator-api
const DEFAULT_PAIR = Object.freeze({ sourceLanguage: "ja", targetLanguage: "ko" });
const sessions = new Map();
const creations = new Map();

const button = document.getElementById("translator-start-button");
const statusEl = document.getElementById("status");
const progressBarEl = document.getElementById("progress-bar");
const progressTextEl = document.getElementById("progress-text");

let socket = null;
let disposed = false;
let ready = false;
let startupFailed = false;
let pendingGesturePair = DEFAULT_PAIR;
const gestureWaiters = new Map();
let workChain = Promise.resolve();
let lastProgressPercent = -1;
let lastProgressSentAt = 0;

function translatorApi() {
  // A bare `Translator` reference throws in browsers without this API.
  return typeof Translator === "undefined" ? null : Translator;
}

function pairKey(pair) {
  return `${pair.sourceLanguage}\u0000${pair.targetLanguage}`;
}

function pairLabel(pair) {
  return `${pair.sourceLanguage} → ${pair.targetLanguage}`;
}

function errorDetail(error) {
  return String(error?.message || error || "알 수 없는 오류")
    .replace(/\s+/g, " ").trim().slice(0, 400);
}

function actionableError(error, pair) {
  const detail = errorDetail(error);
  if (detail.startsWith("Translator API를 사용할 수 없습니다.") ||
      detail.includes("BCP 47 언어 코드") ||
      detail.includes("언어 쌍 또는 이 기기에서 Translator API 모델을 사용할 수 없습니다.")) {
    return detail;
  }
  if (error?.name === "NotAllowedError") {
    return `${pairLabel(pair)} 모델 다운로드에 사용자 클릭이 필요합니다. 이 창의 'Translator API 모델 시작' 버튼을 눌러 다시 시도하세요. (${detail})`;
  }
  if (error?.name === "SecurityError") {
    return `Translator API 접근이 차단되었습니다. localhost의 지원 브라우저에서 실행하고 브라우저 권한 설정을 확인하세요. (${detail})`;
  }
  return `${pairLabel(pair)} Translator API 모델을 준비하거나 번역하지 못했습니다. 인터넷 연결과 브라우저 저장 공간을 확인한 뒤 브라우저를 다시 시작해 보세요. (${detail})`;
}

function sendToServer(payload) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

function setStatus(message, notifyServer = true) {
  statusEl.textContent = message;
  if (notifyServer) {
    sendToServer({ type: "loading", message });
  }
}

function setProgress(percent, message) {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  progressBarEl.style.width = `${safePercent}%`;
  progressTextEl.textContent = `${safePercent}% - ${message}`;
}

function onDownloadProgress(pair, event) {
  const loaded = Number(event.loaded);
  const total = Number(event.total);
  const percent = Number.isFinite(loaded) && Number.isFinite(total) && total > 0
    ? Math.max(0, Math.min(100, Math.round(loaded / total * 100)))
    : 0;
  const message = `${pairLabel(pair)} 브라우저 모델 다운로드 중`;
  setProgress(percent, message);
  const now = Date.now();
  if (percent >= lastProgressPercent + 5 || now - lastProgressSentAt >= 2000) {
    lastProgressPercent = percent;
    lastProgressSentAt = now;
    setStatus(`${message} (${percent}%)`);
  }
}

function createSessionNow(pair) {
  const key = pairKey(pair);
  if (sessions.has(key)) {
    return Promise.resolve(sessions.get(key));
  }
  if (creations.has(key)) {
    return creations.get(key);
  }

  const api = translatorApi();
  if (!api || typeof api.create !== "function") {
    return Promise.reject(new Error("Translator API를 사용할 수 없습니다. 최신 데스크톱 Edge 또는 Chrome으로 실행하세요."));
  }

  lastProgressPercent = -1;
  lastProgressSentAt = 0;
  setProgress(0, `${pairLabel(pair)} 브라우저 모델 준비 중`);
  setStatus(`${pairLabel(pair)} 브라우저 모델을 준비하는 중...`);

  // Call create() before returning from a trusted click. Awaiting even one
  // promise first would lose the activation needed for an initial download.
  let creation;
  try {
    creation = api.create({
      sourceLanguage: pair.sourceLanguage,
      targetLanguage: pair.targetLanguage,
      monitor(monitor) {
        monitor.addEventListener("downloadprogress", (event) => onDownloadProgress(pair, event));
      },
    });
  } catch (error) {
    return Promise.reject(error);
  }

  const tracked = Promise.resolve(creation).then((session) => {
    if (!session || typeof session.translate !== "function") {
      throw new Error("브라우저가 올바른 Translator 세션을 반환하지 않았습니다.");
    }
    if (disposed) {
      session.destroy?.();
      throw new Error("worker page closed while loading the translator");
    }
    sessions.set(key, session);
    setProgress(100, `${pairLabel(pair)} 브라우저 모델 준비 완료`);
    return session;
  }).finally(() => {
    creations.delete(key);
  });
  creations.set(key, tracked);
  return tracked;
}

function waitForTrustedClick(pair) {
  const key = pairKey(pair);
  let waiter = gestureWaiters.get(key);
  if (!waiter) {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    waiter = { promise, resolve };
    gestureWaiters.set(key, waiter);
  }
  pendingGesturePair = pair;
  button.disabled = false;
  button.textContent = `${pairLabel(pair)} 모델 시작`;
  setStatus(`${pairLabel(pair)} 모델 다운로드를 시작하려면 위 버튼을 클릭하세요.`);
  sendToServer({
    type: "activation_required",
    sourceLanguage: pair.sourceLanguage,
    targetLanguage: pair.targetLanguage,
  });
  return waiter.promise;
}

async function checkAvailability(pair) {
  const api = translatorApi();
  if (!api || typeof api.availability !== "function" || typeof api.create !== "function") {
    throw new Error("Translator API를 사용할 수 없습니다. 최신 데스크톱 Edge 또는 Chrome으로 실행하고, 브라우저의 Translator API 지원 여부를 확인하세요.");
  }
  let availability;
  try {
    availability = await api.availability(pair);
  } catch (error) {
    throw new Error(`${pairLabel(pair)} Translator API 사용 가능 여부를 확인하지 못했습니다: ${errorDetail(error)}`);
  }
  if (availability === "unavailable") {
    throw new Error(`${pairLabel(pair)} 언어 쌍 또는 이 기기에서 Translator API 모델을 사용할 수 없습니다. 다른 지원 브라우저나 언어 쌍을 선택하세요.`);
  }
  if (!["available", "downloadable", "downloading"].includes(availability)) {
    throw new Error(`${pairLabel(pair)} Translator API가 알 수 없는 상태를 반환했습니다: ${String(availability)}`);
  }
  return availability;
}

async function getSession(pair) {
  const key = pairKey(pair);
  if (sessions.has(key)) {
    return sessions.get(key);
  }
  if (creations.has(key)) {
    return await creations.get(key);
  }
  const availability = await checkAvailability(pair);
  try {
    if (availability !== "available") {
      const clicked = await waitForTrustedClick(pair);
      return await clicked.creation;
    }
    return await createSessionNow(pair);
  } catch (error) {
    if (error?.name === "NotAllowedError") {
      pendingGesturePair = pair;
      button.disabled = false;
      button.textContent = `${pairLabel(pair)} 모델 시작`;
    }
    throw error;
  }
}

function normalizeLanguage(value, fallback) {
  if (value === undefined || value === null || value === "") {
    return fallback;
  }
  if (typeof value !== "string") {
    throw new Error("언어 코드는 BCP 47 문자열이어야 합니다.");
  }
  const code = value.trim();
  try {
    return Intl.getCanonicalLocales(code)[0];
  } catch (_error) {
    throw new Error(`잘못된 BCP 47 언어 코드: ${code.slice(0, 80)}`);
  }
}

function requestPair(request) {
  return {
    sourceLanguage: normalizeLanguage(request.sourceLanguage, DEFAULT_PAIR.sourceLanguage),
    targetLanguage: normalizeLanguage(request.targetLanguage, DEFAULT_PAIR.targetLanguage),
  };
}

async function handleTranslateRequest(request) {
  if (!ready) {
    sendToServer({ type: "error", id: request.id, message: "Translator API 모델이 아직 준비되지 않았습니다." });
    return;
  }
  if (typeof request.text !== "string") {
    sendToServer({ type: "error", id: request.id, message: "번역할 텍스트가 문자열이 아닙니다." });
    return;
  }

  let pair = DEFAULT_PAIR;
  try {
    pair = requestPair(request);
    const session = await getSession(pair);
    const translated = await session.translate(request.text);
    if (typeof translated !== "string" || !translated.trim()) {
      throw new Error("Translator API가 빈 번역 결과를 반환했습니다.");
    }
    sendToServer({ type: "result", id: request.id, text: translated.trim() });
  } catch (error) {
    sendToServer({ type: "error", id: request.id, message: actionableError(error, pair) });
  }
}

function connectWebSocket() {
  return new Promise((resolve, reject) => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const current = new WebSocket(`${protocol}//${location.host}/ws/worker`);
    socket = current;
    let opened = false;

    current.onopen = () => {
      opened = true;
      setStatus("HYTrans에 연결되었습니다. Translator API를 확인합니다.");
      resolve();
    };
    current.onmessage = (event) => {
      let request;
      try {
        request = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      if (request?.type === "translate") {
        workChain = workChain.then(() => handleTranslateRequest(request));
      }
    };
    current.onerror = () => {
      if (!opened) {
        reject(new Error("HYTrans WebSocket에 연결하지 못했습니다."));
      }
    };
    current.onclose = () => {
      if (!opened) {
        reject(new Error("HYTrans WebSocket 연결이 열리기 전에 종료되었습니다."));
      } else if (socket === current && !startupFailed && !disposed) {
        ready = false;
        setStatus("HYTrans 연결이 끊겼습니다. worker 창을 다시 열어 주세요.", false);
      }
    };
  });
}

function announceReady() {
  ready = true;
  startupFailed = false;
  button.disabled = true;
  button.textContent = "Translator API 준비 완료";
  setProgress(100, "Translator API 모델 준비 완료");
  sendToServer({
    type: "ready",
    device: "browser",
    model: "Browser Translator API",
    dtype: "browser",
    modelMode: "on-device",
  });
  setStatus("Translator API 준비 완료", false);
}

function failStartup(error) {
  ready = false;
  startupFailed = true;
  pendingGesturePair = DEFAULT_PAIR;
  const message = actionableError(error, DEFAULT_PAIR);
  setStatus(`ERROR: ${message}`, false);
  sendToServer({ type: "fatal", message });
  button.disabled = false;
  button.textContent = "Translator API 다시 시도";
}

async function initialize() {
  try {
    await connectWebSocket();
    const availability = await checkAvailability(DEFAULT_PAIR);
    if (availability === "available") {
      await createSessionNow(DEFAULT_PAIR);
    } else {
      // A CDP click can arrive as soon as the script is armed, before the
      // availability check settles. Reuse the session it started in that case.
      if (sessions.has(pairKey(DEFAULT_PAIR)) || creations.has(pairKey(DEFAULT_PAIR))) {
        await createSessionNow(DEFAULT_PAIR);
      } else {
        const clicked = await waitForTrustedClick(DEFAULT_PAIR);
        await clicked.creation;
      }
    }
    announceReady();
  } catch (error) {
    failStartup(error);
  }
}

button.addEventListener("click", () => {
  if (disposed) {
    return;
  }
  const pair = pendingGesturePair;
  button.disabled = true;
  const creation = createSessionNow(pair);
  // Attach a rejection handler immediately even if availability() is still
  // pending and initialize() has not yet reached its click wait.
  void creation.catch(() => {});
  const key = pairKey(pair);
  const waiter = gestureWaiters.get(key);
  if (waiter) {
    gestureWaiters.delete(key);
    waiter.resolve({ creation });
  }

  if (startupFailed) {
    void creation.then(async () => {
      socket?.close();
      await connectWebSocket();
      announceReady();
    }).catch(failStartup);
  } else if (ready) {
    void creation.then(() => {
      button.disabled = true;
      button.textContent = "Translator API 준비 완료";
      setStatus(`${pairLabel(pair)} 모델 준비 완료`, false);
    }).catch((error) => {
      button.disabled = false;
      setStatus(`ERROR: ${actionableError(error, pair)}`, false);
    });
  }
});
button.dataset.armed = "true";

window.addEventListener("pagehide", () => {
  disposed = true;
  ready = false;
  for (const session of sessions.values()) {
    try { session.destroy?.(); } catch (_error) { /* Page is closing. */ }
  }
  sessions.clear();
  gestureWaiters.clear();
  socket?.close();
});

void initialize();
