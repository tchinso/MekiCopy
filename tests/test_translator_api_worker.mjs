import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../assets/translator_api_worker.js", import.meta.url), "utf8");

function makePage(availability = "downloadable", apiPresent = true) {
  const listeners = new Map();
  const button = {
    dataset: {},
    disabled: false,
    textContent: "",
    addEventListener(type, listener) { listeners.set(type, listener); },
    click() { listeners.get("click")(); },
  };
  const elements = {
    "translator-start-button": button,
    status: { textContent: "" },
    "progress-bar": { style: { width: "0%" } },
    "progress-text": { textContent: "" },
  };
  const pageListeners = new Map();
  const sockets = [];
  const calls = [];
  const sessions = [];
  let userGesture = false;

  class FakeWebSocket {
    static OPEN = 1;
    constructor() {
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.onopen?.();
      });
    }
    send(json) { this.sent.push(JSON.parse(json)); }
    close() {
      this.readyState = 3;
      this.onclose?.();
    }
    receive(payload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  }

  const Translator = {
    async availability(pair) {
      calls.push(["availability", pair.sourceLanguage, pair.targetLanguage]);
      return typeof availability === "function" ? availability(pair) : availability;
    },
    create(options) {
      calls.push(["create", options.sourceLanguage, options.targetLanguage, userGesture]);
      const pairState = typeof availability === "function" ? availability(options) : availability;
      if (pairState !== "available" && !userGesture) {
        const error = new Error("user activation required");
        error.name = "NotAllowedError";
        return Promise.reject(error);
      }
      options.monitor({
        addEventListener(type, listener) {
          assert.equal(type, "downloadprogress");
          listener({ loaded: 1, total: 2 });
        },
      });
      const session = {
        pair: `${options.sourceLanguage}:${options.targetLanguage}`,
        destroyed: false,
        async translate(text) { return `${text} (${this.pair})`; },
        destroy() { this.destroyed = true; },
      };
      sessions.push(session);
      return Promise.resolve(session);
    },
  };
  const context = {
    document: { getElementById(id) { return elements[id]; } },
    window: { addEventListener(type, listener) { pageListeners.set(type, listener); } },
    location: { protocol: "http:", host: "127.0.0.1:8765" },
    WebSocket: FakeWebSocket,
    ...(apiPresent ? { Translator } : {}),
  };
  vm.runInNewContext(source, context, { filename: "translator_api_worker.js" });

  return {
    button, elements, sockets, calls, sessions,
    click() {
      userGesture = true;
      try { button.click(); } finally { userGesture = false; }
    },
    pagehide() { pageListeners.get("pagehide")(); },
  };
}

async function until(predicate) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail("worker did not reach expected state");
}

test("downloadable model waits for a trusted click, then translates and caches sessions", async () => {
  const page = makePage((pair) => pair.targetLanguage === "ko" ? "downloadable" : "available");
  assert.equal(page.button.dataset.armed, "true");
  await until(() => page.sockets[0]?.sent.some((message) => message.type === "loading"));
  assert.equal(page.calls.some((call) => call[0] === "create"), false);

  page.click();
  await until(() => page.sockets[0].sent.some((message) => message.type === "ready"));
  const ready = page.sockets[0].sent.find((message) => message.type === "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(ready)), {
    type: "ready", device: "browser", model: "Browser Translator API",
    dtype: "browser", modelMode: "on-device",
  });
  assert.deepEqual(page.calls.find((call) => call[0] === "create"), ["create", "ja", "ko", true]);

  const socket = page.sockets[0];
  socket.receive({ type: "translate", id: "one", text: "こんにちは" });
  socket.receive({ type: "translate", id: "two", text: "さようなら" });
  socket.receive({ type: "translate", id: "three", text: "hello", sourceLanguage: "en", targetLanguage: "ja" });
  await until(() => socket.sent.filter((message) => message.type === "result").length === 3);
  assert.equal(page.calls.filter((call) => call[0] === "create" && call[1] === "ja").length, 1);
  assert.equal(page.calls.filter((call) => call[0] === "create" && call[1] === "en").length, 1);
  assert.equal(socket.sent.find((message) => message.id === "three").text, "hello (en:ja)");
  assert.equal(page.elements["progress-bar"].style.width, "100%");

  page.pagehide();
  assert.equal(page.sessions.every((session) => session.destroyed), true);
});

test("unsupported browser reports an actionable fatal error", async () => {
  const page = makePage("available", false);
  await until(() => page.sockets[0]?.sent.some((message) => message.type === "fatal"));
  const fatal = page.sockets[0].sent.find((message) => message.type === "fatal");
  assert.match(fatal.message, /Translator API/);
  assert.match(fatal.message, /Edge|Chrome/);
});

test("later downloadable language pair asks the server for a fresh trusted click", async () => {
  const page = makePage((pair) => pair.sourceLanguage === "ja" ? "available" : "downloadable");
  await until(() => page.sockets[0]?.sent.some((message) => message.type === "ready"));

  const socket = page.sockets[0];
  socket.receive({ type: "translate", id: "other-pair", text: "hello", sourceLanguage: "en", targetLanguage: "ko" });
  await until(() => socket.sent.some((message) => message.type === "activation_required"));
  const activation = socket.sent.find((message) => message.type === "activation_required");
  assert.equal(activation.sourceLanguage, "en");
  assert.equal(activation.targetLanguage, "ko");
  assert.equal(page.calls.some((call) => call[0] === "create" && call[1] === "en"), false);
  assert.equal(page.button.disabled, false);

  page.click();
  await until(() => socket.sent.some((message) => message.id === "other-pair" && message.type === "result"));
  assert.deepEqual(page.calls.find((call) => call[0] === "create" && call[1] === "en"), ["create", "en", "ko", true]);
  assert.equal(socket.sent.find((message) => message.id === "other-pair").text, "hello (en:ko)");
});
