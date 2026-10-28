import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createNativePlugin } from '../plugin/native.mjs';

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'zen-native-test-'));
const privatePath = path.join(temp, 'private.json');
fs.writeFileSync(privatePath, JSON.stringify({ port: 19000, token: 'test-local', keys: ['test-key-a', 'test-key-b'] }));
const originalFetch = globalThis.fetch;
try {
  const hooks = {};
  await createNativePlugin(privatePath).setup({ session: { hook: async (name, fn) => { hooks[name] = fn; } } });
  const reports = [];
  let upstream = [];
  let statuses = [429, 200];
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/plan')) return Response.json({ pairs: [[0, 0], [1, 1]], tunnel_port: 19001 });
    if (url.endsWith('/report')) { reports.push(JSON.parse(options.body)); return Response.json({ ok: true }); }
    assert.equal(url, 'https://opencode.ai/inference/openai/v1/chat/completions');
    assert.equal(options.headers.get('x-opencode-org-id'), null);
    assert.equal(options.headers.get('x-org-id'), null);
    assert.ok(options.proxy.startsWith('http://'));
    upstream.push(options);
    return Response.json({ choices: [{ message: { content: 'OK' } }] }, { status: statuses.shift() ?? 200 });
  };
  const event = () => ({ model: { providerID: 'opencode', id: 'example-free' }, request: new Request('https://opencode.ai/inference/openai/v1/chat/completions', { method: 'POST', headers: { authorization: 'Bearer stale', 'x-opencode-org-id': 'stale-org' }, body: JSON.stringify({ model: 'example-free' }) }) });
  const e = event();
  await hooks['http.request'](e);
  assert.equal(upstream.length, 2);
  assert.equal(upstream[0].headers.get('authorization'), 'Bearer test-key-a');
  assert.equal(upstream[1].headers.get('authorization'), 'Bearer test-key-b');
  assert.equal(reports[1].rotation, true);
  const after = { ...e, response: new Response(null, { status: 204 }) };
  await hooks['http.response'](after);
  assert.equal((await after.response.json()).choices[0].message.content, 'OK');

  for (const [provider, model, url] of [ ['other', 'example-free', 'https://other.example/v1'], ['opencode', 'paid', 'https://opencode.ai/inference/openai/v1/chat/completions'], ['opencode', 'example-free', 'https://other.example/v1'], ['opencode', 'example-free', 'https://opencode.ai/unexpected'] ]) {
    const request = new Request(url);
    const bypass = { model: { providerID: provider, id: model }, request };
    await hooks['http.request'](bypass);
    assert.equal(bypass.request, request);
  }
  upstream = []; statuses = [500, 200];
  const nonRetry = event();
  await hooks['http.request'](nonRetry);
  assert.equal(upstream.length, 1);
  const a = { ...nonRetry, response: new Response(null, { status: 204 }) };
  await hooks['http.response'](a);
  assert.equal(a.response.status, 500);

  for (const status of [401, 403, 429]) {
    statuses = [status, status]; upstream = [];
    const failed = event();
    await hooks['http.request'](failed);
    const failedResponse = { ...failed, response: new Response(null, { status: 204 }) };
    await hooks['http.response'](failedResponse);
    assert.equal(failedResponse.response.status, status);
    assert.equal(upstream.length, 2);
  }
  upstream = []; reports.length = 0;
  let quotaAttempts = 0;
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/plan')) return Response.json({ pairs: [[0, 0], [1, 1]], tunnel_port: 19001 });
    if (url.endsWith('/report')) { reports.push(JSON.parse(options.body)); return Response.json({ ok: true }); }
    quotaAttempts++;
    return quotaAttempts === 1
      ? new Response("You've used today's free messages. They reset at 8:17 PM.", { status: 200, headers: { 'content-type': 'text/plain' } })
      : Response.json({ choices: [{ message: { content: 'OK after rotation' } }] });
  };
  const quotaEvent = event();
  await hooks['http.request'](quotaEvent);
  const quotaResponse = { ...quotaEvent, response: new Response(null, { status: 204 }) };
  await hooks['http.response'](quotaResponse);
  assert.equal(quotaAttempts, 2);
  assert.equal(reports[0].daily_free, true);
  assert.equal(reports[0].status, 429);
  assert.equal((await quotaResponse.response.json()).choices[0].message.content, 'OK after rotation');

  upstream = []; reports.length = 0;
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/plan')) return Response.json({ pairs: [[0, 0], [1, 1]], tunnel_port: 19001 });
    if (url.endsWith('/report')) { reports.push(JSON.parse(options.body)); return Response.json({ ok: true }); }
    return new Response("You've used today's free messages as an example.", { status: 200, headers: { 'content-type': 'text/event-stream' } });
  };
  const stream = event();
  await hooks['http.request'](stream);
  assert.equal(reports.length, 1);
  assert.equal(reports[0].daily_free, false);
  const mockFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.startsWith('https://')) throw new TypeError('Test connection failure');
    return mockFetch(url, options);
  };
  const transport = event();
  await hooks['http.request'](transport);
  const transportResponse = { ...transport, response: new Response(null, { status: 204 }) };
  await hooks['http.response'](transportResponse);
  assert.equal(transportResponse.response.status, 502);
  assert.equal((await transportResponse.response.json()).error.type, 'transport_error');
  globalThis.fetch = mockFetch;

  const abort = new AbortController(); abort.abort();
  const aborted = event();
  aborted.request = new Request(aborted.request, { signal: abort.signal });
  await assert.rejects(hooks['http.request'](aborted));
  console.log('PASS: native fetch, key/route rotation, stale organization removal, provider bypass, no replay after response');
} finally {
  globalThis.fetch = originalFetch;
  fs.rmSync(temp, { recursive: true });
}
