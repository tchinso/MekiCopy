import { env, pipeline } from "/assets/transformers.min.js";

let generator = null;
let socket = null;
let config = null;
let activeDevice = null;
let warning = null;

const statusEl = document.getElementById("status");

function setStatus(message) {
  statusEl.textContent = message;
  console.log("[HYTrans Worker]", message);
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "loading", message }));
  }
}

function cleanTranslationOutput(text) {
  return text
    .replace(/^assistant\s*[:：]\s*/i, "")
    .trimStart();
}

function buildPrompt(inputText) {
  return `Translate the following segment from Japanese into Korean, without additional explanation.\n\n${inputText}`;
}

function extractGeneratedText(result) {
  const item = Array.isArray(result) ? result[0] : result;
  const generated = item?.generated_text ?? item?.text ?? "";
  if (Array.isArray(generated)) {
    return generated.at(-1)?.content || "";
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

function setupTransformersEnv(runtimeConfig) {
  if (runtimeConfig.modelMode === "local") {
    env.allowRemoteModels = false;
    env.localModelPath = "/models/";
  } else {
    env.allowRemoteModels = true;
  }

  if (runtimeConfig.hasLocalWasm && env.backends?.onnx?.wasm) {
    env.backends.onnx.wasm.wasmPaths = "/assets/wasm/";
  }
}

async function createGeneratorWithFallback() {
  const preferredDevice = navigator.gpu ? "webgpu" : "wasm";
  try {
    setStatus(`loading model with ${preferredDevice}...`);
    const pipe = await pipeline("text-generation", config.modelId, {
      dtype: config.dtype,
      device: preferredDevice,
    });
    activeDevice = preferredDevice;
    return pipe;
  } catch (err) {
    if (preferredDevice !== "webgpu") {
      throw err;
    }
    console.warn("WebGPU failed, fallback to wasm:", err);
    warning = "webgpu failed, using wasm fallback";
    setStatus("WebGPU failed. fallback to wasm...");
    const pipe = await pipeline("text-generation", config.modelId, {
      dtype: config.dtype,
      device: "wasm",
    });
    activeDevice = "wasm";
    return pipe;
  }
}

function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws/worker`);

  socket.onopen = () => {
    socket.send(JSON.stringify({
      type: "ready",
      device: activeDevice,
      model: config.modelId,
      dtype: config.dtype,
      modelMode: config.modelMode,
      warning,
    }));
    setStatus(`ready: ${activeDevice}`);
  };

  socket.onmessage = async (event) => {
    const req = JSON.parse(event.data);
    if (req.type !== "translate") {
      return;
    }

    try {
      const result = await generator([{ role: "user", content: buildPrompt(req.text) }], {
        max_new_tokens: req.max_new_tokens ?? config.maxNewTokens ?? 2048,
        do_sample: false,
      });
      const rawText = extractGeneratedText(result);
      const translated = cleanTranslationOutput(rawText).trim();
      socket.send(JSON.stringify({
        type: "result",
        id: req.id,
        text: translated,
      }));
    } catch (err) {
      socket.send(JSON.stringify({
        type: "error",
        id: req.id,
        message: err?.message ?? String(err),
      }));
    }
  };

  socket.onclose = () => {
    setStatus("WebSocket closed");
  };

  socket.onerror = () => {
    setStatus("WebSocket error");
  };
}

async function main() {
  try {
    setStatus("loading config...");
    config = await loadConfig();
    setupTransformersEnv(config);
    generator = await createGeneratorWithFallback();
    setStatus("connecting websocket...");
    connectWebSocket();
  } catch (err) {
    console.error(err);
    setStatus(`ERROR: ${err?.message ?? String(err)}`);
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({
        type: "fatal",
        message: err?.message ?? String(err),
      }));
    }
  }
}

main();

