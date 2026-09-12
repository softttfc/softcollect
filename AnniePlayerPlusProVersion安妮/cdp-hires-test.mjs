// Hi-Res 长路径切歌卡顿复现探针（用完即删）
const PORT = 9242;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function getWsUrl() {
  for (let i = 0; i < 40; i++) {
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
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
await new Promise(r => { ws.onopen = r; });
const send = (method, params) => new Promise(res => {
  const i = ++seq; pending.set(i, res);
  ws.send(JSON.stringify({ id: i, method, params }));
});
async function ev(ex) {
  const r = await send('Runtime.evaluate', { expression: ex, awaitPromise: true, returnByValue: true });
  return r.result && r.result.result ? r.result.result.value : JSON.stringify(r.result).slice(0, 300);
}

const D = 'E:/pro-test/音源/東京フィルハーモニー交響楽団 - 初音ミクシンフォニー～Miku Symphony 2022 オーケストラライブ 24-96';
await ev(`state.library.tracks=[
  {path:'${D}/01. 初音ミクの激唱 (Live at 東京国際フォーラム) 24bit-96kHz.flac',name:'01.flac',dir:'${D}',mtime:1},
  {path:'${D}/02. 千本桜 (オーケストラ Ver.) 24bit-96kHz.flac',name:'02.flac',dir:'${D}',mtime:1}];
  state.library.metaCache={};state.library.stats={};state.queue=state.library.tracks.slice();
  state.libNav={mode:'tracks',folder:null};renderCurrentView();'ok'`);
await sleep(500);

// 切歌耗时（playAt → playing）
let t0 = Date.now();
await ev("playAt(0); 'ok'");
let waited = 0;
while (waited < 15000) {
  const p = await ev("state.playing && state.position > 0.05");
  if (p === true) break;
  await sleep(150); waited += 150;
}
console.log('[首曲起播]', (Date.now() - t0) + 'ms');
console.log('[格式]', await ev("JSON.stringify({fmt:state.format, bp:window.annieProStatus && annieProStatus.now()})"));
await sleep(1000);

// 切歌后立即暂停：测 RPC 往返
t0 = Date.now();
await ev("playAt(1); 'ok'");
const tPlay = Date.now() - t0;
await sleep(200); // 切歌后 200ms 立刻暂停（模拟用户狂点）
t0 = Date.now();
await ev("mine.engine('pause',{}).then(()=>0); 'ok'");
await ev("(async()=>{ const a=performance.now(); await mine.engine('pause',{}); return Math.round(performance.now()-a); })()").then(v => console.log('[暂停RPC往返]', v + 'ms'));
await ev("mine.engine('resume',{}).then(()=>0); 'ok'");
await sleep(800);
// 拖动进度 seek
const seekMs = await ev("(async()=>{ const a=performance.now(); await mine.engine('seek',{seconds:120}); return Math.round(performance.now()-a); })()");
console.log('[seek RPC]', seekMs + 'ms');
console.log('[切歌调用耗时]', tPlay + 'ms（playAt 同步段）');
console.log('[播放中]', await ev("JSON.stringify({playing:state.playing,pos:Math.round(state.position)})"));
console.log('[缓冲水位]', await ev("(async()=>JSON.stringify(await mine.engine('buffer',{})))()"));
console.log('[异常]', await ev("window.__proErrors ? window.__proErrors.slice(0,2).join('|') : '(无)'"));
ws.close();
process.exit(0);
