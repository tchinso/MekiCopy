import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../assets/worker.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?\n/, "")
  .replace(/main\(\);\s*$/, "");

test("local prompt substitution preserves placeholders inside replacement values", () => {
  const context = vm.createContext({
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
    env,
    pipeline: async (_task, _model, options) => {
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
  assert.equal(await vm.runInContext(`
    config = { modelMode: "local", modelId: "local-model", dtype: "q4" };
    setupTransformersEnv(config);
    createPipeline("wasm");
  `, context), "pipeline");
  assert.equal(calls, 1);
});
