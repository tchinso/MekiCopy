import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../assets/worker.js", import.meta.url), "utf8");
const context = vm.createContext({
  document: { getElementById: () => null },
  window: { addEventListener() {} },
  initialEnv: {},
  initialPipeline: () => {},
  TextEncoder,
});
vm.runInContext(source.replace(/^import .*;\r?\n/, "").replace(/main\(\);\s*$/, ""), context);

test("PLaMo uses its target-language template with byte fallback and one BOS", () => {
  const bos = "<|plamo:bos|>";
  const tokenizer = {
    bos_token: bos,
    unk_token_id: 0,
    encode(text) { return text.includes("녕") ? [1, 0] : [1]; },
    apply_chat_template(messages, options) {
      assert.equal(messages.length, 1);
      assert.equal(messages[0].role, "user");
      assert.equal(messages[0].content, "こんにちは。 안<0xEB><0x85><0x95>하세요");
      assert.equal(options.target_lang, "ko");
      assert.equal(options.add_generation_prompt, true);
      assert.equal(options.tokenize, false);
      return `${bos}<PFML3 prompt>`;
    },
  };
  assert.equal(context.buildPlamoPrompt(tokenizer, "こんにちは。 안녕하세요", "ko"), "<PFML3 prompt>");
});
