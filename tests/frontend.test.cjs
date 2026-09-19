const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

function utilities(fetch) {
    const timers = new Map();
    let nextId = 0;
    const context = vm.createContext({
        document: { addEventListener() {} }, URLSearchParams, URL, AbortController,
        fetch, setTimeout(callback) { timers.set(++nextId, callback); return nextId; },
        clearTimeout(id) { timers.delete(id); },
    });
    vm.runInContext(readFileSync('static/utils.js', 'utf8'), context);
    return { context, timers };
}

test('rendering escapes markup and tolerates nullable values', () => {
    const { context } = utilities();
    assert.equal(vm.runInContext('escapeHTML(null)', context), '');
    assert.equal(vm.runInContext('escapeHTML(42)', context), '42');
    assert.equal(vm.runInContext('escapeHTML("<script>&\\\"")', context), '&lt;script&gt;&amp;&quot;');
});

test('source links reject executable URLs and escape external titles', () => {
    const { context } = utilities();
    assert.equal(vm.runInContext('safeSourceURL("javascript:alert(1)")', context), null);
    assert.equal(vm.runInContext('safeSourceURL("https://user:password@example.com")', context), null);
    assert.equal(vm.runInContext('renderPostSources([{url:"javascript:alert(1)",title:"Bad"}])', context), '');
    const html = vm.runInContext('renderPostSources([{url:"https://example.com/news",title:"<script>bad</script>"}])', context);
    assert.ok(html.includes('&lt;script&gt;bad&lt;/script&gt;'));
    assert.ok(html.includes('rel="noopener noreferrer"'));
    assert.ok(!html.includes('<script>'));
});

test('successful JSON fetch clears its timeout', async () => {
    const { context, timers } = utilities(async () => ({ ok: true, json: async () => ({ success: true }) }));
    const result = await vm.runInContext('fetchJSON("/api/session")', context);
    assert.equal(result.success, true);
    assert.equal(timers.size, 0);
});

test('API errors retain actionable text and status', async () => {
    const { context, timers } = utilities(async () => ({ ok: false, status: 401, json: async () => ({ error: 'Sign in required' }) }));
    await assert.rejects(vm.runInContext('fetchJSON("/api/feed")', context), error => error.status === 401 && error.message === 'Sign in required');
    assert.equal(timers.size, 0);
});

test('a stalled fetch aborts instead of keeping the interface loading forever', async () => {
    let aborted = false;
    const { context, timers } = utilities((url, options) => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => {
            aborted = true;
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
        });
    }));
    const promise = vm.runInContext('fetchJSON("/api/models")', context);
    for (const callback of timers.values()) callback();
    await assert.rejects(promise, /Request timed out/);
    assert.equal(aborted, true);
    assert.equal(timers.size, 0);
});
