import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../assets/worker.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?\n/, "")
  .replace(/main\(\);\s*$/, "");

test("local prompt substitution preserves placeholders inside replacement values", () => {
  const context = vm.createContext({
    initialEnv: {},
    initialPipeline: null,
    document: { getElementById() { return {}; } },
    window: { addEventListener() {} },
  });
  vm.runInContext(source, context);
  const result = vm.runInContext(`
    config = {source: "Japanese", target: "Korean {text}", promptTemplate: "{source} to {target}: {text}"};
    buildPrompt("literal {target} and $&");
  `, context);
  assert.equal(result, "Japanese to Korean {text}: literal {target} and $&");
});

test("local model loading bypasses Chromium cache before constructing the pipeline", async () => {
  const env = { useBrowserCache: true, backends: { onnx: { wasm: {} } } };
  let calls = 0;
  const context = vm.createContext({
    initialEnv: env,
    initialPipeline: async (_task, _model, options) => {
      assert.equal(env.useBrowserCache, false);
      assert.equal(env.allowRemoteModels, false);
      assert.equal(options.device, "wasm");
      calls += 1;
      return "pipeline";
    },
    document: { getElementById() { return {}; } },
    window: { addEventListener() {} },
  });
  vm.runInContext(source, context);
  context.wasmRuntime = { env, pipeline: context.initialPipeline };
  assert.equal(await vm.runInContext(`
    loadWasmTransformers = async () => wasmRuntime;
    config = { modelMode: "local", modelId: "local-model", dtype: "q4" };
    setupTransformersEnv(config);
    createPipeline("wasm");
  `, context), "pipeline");
  assert.equal(calls, 1);
});

function makeRuntime({ adapter = null, rejectWebGpu = false } = {}) {
  const env = { backends: { onnx: { wasm: {}, webgpu: {} } } };
  const wasmEnv = { backends: { onnx: { wasm: {}, webgpu: {} } } };
  const calls = [];
  const elements = {
    status: { textContent: "" },
    "progress-bar": { style: {} },
    "progress-text": { textContent: "" },
  };
  const makePipeline = (runtimeEnv) => async (_task, _model, options) => {
    calls.push({ device: options.device, adapter: runtimeEnv.backends.onnx.webgpu.adapter });
    if (options.device === "webgpu" && rejectWebGpu) {
      throw new Error("unsupported WebGPU operator");
    }
    return "pipeline";
  };
  const context = vm.createContext({
    initialEnv: env,
    navigator: { gpu: { async requestAdapter() { return adapter; } } },
    initialPipeline: makePipeline(env),
    fetch: async () => ({ ok: true }),
    document: { getElementById(id) { return elements[id]; } },
    window: { addEventListener() {} },
  });
  vm.runInContext(source, context);
  context.wasmRuntime = { env: wasmEnv, pipeline: makePipeline(wasmEnv) };
  vm.runInContext(`
    loadWasmTransformers = async () => wasmRuntime;
    config = { modelMode: "local", modelId: "local-model", dtype: "q4", hasLocalWasm: true };
    setupTransformersEnv(config);
  `, context);
  return { context, env, wasmEnv, calls };
}

test("WebGPU initialization uses a matching bundled asyncify factory and binary", () => {
  const { env } = makeRuntime();
  assert.equal(env.useWasmCache, true);
  assert.equal(env.backends.onnx.wasm.wasmPaths.mjs, "/assets/wasm/ort-wasm-simd-threaded.asyncify.mjs");
  assert.equal(env.backends.onnx.wasm.wasmPaths.wasm, "/assets/wasm/ort-wasm-simd-threaded.asyncify.wasm");
});

test("missing and software WebGPU adapters load the model with WASM", async () => {
  for (const adapter of [null, { info: { vendor: "Google", description: "SwiftShader" } }]) {
    const { context, wasmEnv, calls } = makeRuntime({ adapter });
    assert.equal(await vm.runInContext("createGeneratorWithFallback()", context), "pipeline");
    assert.deepEqual(calls.map((call) => call.device), ["wasm"]);
    assert.equal(vm.runInContext("activeDevice", context), "wasm");
    assert.equal(vm.runInContext("activeDeviceDetail", context), null);
    assert.ok(vm.runInContext("warning", context));
    assert.equal(wasmEnv.backends.onnx.wasm.wasmPaths.mjs, "/assets/wasm/ort-wasm-simd-threaded.mjs");
    assert.equal(wasmEnv.backends.onnx.wasm.wasmPaths.wasm, "/assets/wasm/ort-wasm-simd-threaded.wasm");
  }
});

test("hardware WebGPU uses the adapter that passed the worker probe", async () => {
  const adapter = { info: { vendor: "AMD", architecture: "RDNA" } };
  const { context, calls } = makeRuntime({ adapter });
  assert.equal(await vm.runInContext("createGeneratorWithFallback()", context), "pipeline");
  assert.deepEqual(calls.map((call) => call.device), ["webgpu"]);
  assert.equal(calls[0].adapter, adapter);
  assert.equal(vm.runInContext("activeDevice", context), "webgpu");
  assert.equal(vm.runInContext("activeDeviceDetail", context), "AMD / RDNA");
});

test("WebGPU model initialization failure clears its adapter before WASM retry", async () => {
  const adapter = { info: { vendor: "AMD", architecture: "RDNA" } };
  const { context, env, wasmEnv, calls } = makeRuntime({ adapter, rejectWebGpu: true });
  assert.equal(await vm.runInContext("createGeneratorWithFallback()", context), "pipeline");
  assert.deepEqual(calls.map((call) => call.device), ["webgpu", "wasm"]);
  assert.equal(calls[0].adapter, adapter);
  assert.equal(calls[1].adapter, undefined);
  assert.equal(vm.runInContext("activeDevice", context), "wasm");
  assert.equal(vm.runInContext("activeDeviceDetail", context), null);
  assert.match(vm.runInContext("warning", context), /unsupported WebGPU operator/);
  assert.equal(vm.runInContext("env", context), wasmEnv);
  assert.notEqual(wasmEnv, env);
  assert.equal(env.backends.onnx.wasm.wasmPaths.wasm, "/assets/wasm/ort-wasm-simd-threaded.asyncify.wasm");
  assert.equal(wasmEnv.backends.onnx.wasm.wasmPaths.wasm, "/assets/wasm/ort-wasm-simd-threaded.wasm");
});

test("remote model cache hooks are reinstalled on the isolated CPU runtime once", async () => {
  const { context, env, wasmEnv } = makeRuntime();
  vm.runInContext(`
    let cpuImports = 0;
    loadWasmTransformers = async () => { cpuImports += 1; return wasmRuntime; };
    config.modelMode = "remote";
    setupTransformersEnv(config);
  `, context);
  const previousCache = env.customCache;
  assert.ok(previousCache);
  await vm.runInContext("createPipeline('wasm')", context);
  assert.equal(wasmEnv.useCustomCache, true);
  assert.ok(wasmEnv.customCache);
  assert.notEqual(wasmEnv.customCache, previousCache);
  assert.equal(env.customCache, previousCache);
  assert.equal(wasmEnv.useBrowserCache, false);
  await vm.runInContext("createPipeline('wasm')", context);
  assert.equal(vm.runInContext("cpuImports", context), 1);
});
