import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const template = await fs.readFile(new URL('../plugin/index.ts.in', import.meta.url), 'utf8');
const source = template.replace('__BASE_URL__', JSON.stringify('http://127.0.0.1:19000/v1')).replace('__LOCAL_TOKEN__', JSON.stringify('test-local-token'));
const plugin = (await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))).default;
let hook;
await plugin.setup({session: {hook: async (name, callback) => {
  assert.equal(name, 'model.request'); hook = callback;
}}});
for (const [providerID, id, changes] of [
  ['opencode', 'example-free', true],
  ['opencode', 'paid-model', false],
  ['ddt-gpt-plus', 'example-free', false],
]) {
  const event = {model: {providerID, id}, headers: {'authorization': 'native'}};
  hook(event);
  assert.equal(event.baseURL, changes ? 'http://127.0.0.1:19000/v1' : undefined);
  assert.equal(event.headers['x-zen-rotator-token'], changes ? 'test-local-token' : undefined);
  assert.equal(event.headers.authorization, 'native');
}
console.log('PASS: plugin changes only native Zen free models');
