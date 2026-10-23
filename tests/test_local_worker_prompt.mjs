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
