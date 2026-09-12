// Pro 第四章探针：统一搜索视图（用完即删）
const PORT = 9233;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function getWsUrl() {
  for (let i = 0; i < 30; i++) {
    try {
      const r = await fetch('http://127.0.0.1:' + PORT + '/json');
      const list = await r.json();
      const page = list.find(t => t.type === 'page' && t.url.includes('index.html'));
      if (page) return page.webSocketDebuggerUrl;
    } catch { }
    await sleep(500);
  }
  throw new Error('no page target');
}

const ws = new WebSocket(await getWsUrl());
let seq = 0;
const pending = new Map();
const exceptions = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') exceptions.push(JSON.stringify(m.params.exceptionDetails).slice(0, 250));
};
await new Promise(r => { ws.onopen = r; });
const send = (method, params) => new Promise(res => {
  const i = ++seq; pending.set(i, res);
  ws.send(JSON.stringify({ id: i, method, params }));
});
await send('Runtime.enable');
async function ev(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.result && r.result.exceptionDetails) return 'EXC: ' + JSON.stringify(r.result.exceptionDetails).slice(0, 250);
  return r.result && r.result.result ? r.result.result.value : JSON.stringify(r.result).slice(0, 500);
}

// 准备：本地曲库两首（含标题标签）
await ev("state.library.tracks = [" +
  "{ path: 'E:/pro-test/real.wav', name: 'real.wav', dir: 'E:/pro-test', mtime: 1 }," +
  "{ path: 'E:/pro-test/album.wav#cue1', name: '第一首.wav', dir: 'E:/pro-test', mtime: 1 }];" +
  "state.library.metaCache = { 'E:/pro-test/real.wav': { title: '真实测试曲', artist: '测试艺术家', codec: 'wav', bitrate: 1411200 }," +
  " 'E:/pro-test/album.wav#cue1': { title: '第一首', artist: '测试艺术家', album: '测试专辑' } };" +
  "state.library.stats = {}; state.favorites.add('E:/pro-test/real.wav'); 'ok'");

// 统一搜索（网络在本环境可能受限：流媒体组允许失败，本地组必须出现）
await ev("annieStreamSearch('测试'); 'ok'");
await sleep(6000);
console.log('[统一视图]', await ev("JSON.stringify({" +
  "heads: [...document.querySelectorAll('.stream-group-head')].map(x => x.textContent)," +
  "localRows: document.querySelectorAll('.stream-row.local-row').length," +
  "localNames: [...document.querySelectorAll('.stream-row.local-row .s-name')].map(x => x.textContent)," +
  "badges: [...document.querySelectorAll('.stream-row.local-row .s-badge')].map(x => x.textContent)," +
  "status: document.getElementById('stream-status').textContent" +
  "})"));

// 点击本地行 → 本地播放链路
await ev("(() => { const r = document.querySelector('.stream-row.local-row'); if (r) r.click(); })(); 'ok'");
await sleep(1500);
console.log('[本地播放]', await ev("JSON.stringify({ cur: (state.currentPath || '').split('/').pop(), playing: state.playing })"));

console.log('[异常]', exceptions.length ? exceptions.join(' | ') : '(none)');
ws.close();
process.exit(0);
