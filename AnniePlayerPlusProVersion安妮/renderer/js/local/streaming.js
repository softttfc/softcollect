'use strict';
/* 安妮播放器 V2 —— 流媒体面板（IIFE 包裹，避免与视觉舞台模块的全局标识符冲突） */
(function () {
/* 平台切换（网易云 / QQ 音乐）→ 搜索 → 点击播放。
 * 播放方式：获取平台直链后交给 AnnieEngine 独占解码输出（ffmpeg 拉流），音质与本地播放一致。
 */

const streamState = {
  provider: 'netease',
  results: [],        // 当前搜索结果（同时作为播放队列）
  index: -1,          // 正在播放的结果索引
  searching: false,
};

const $s = (s) => document.querySelector(s);

/* ---------------- 侧栏标签页切换 ---------------- */
document.querySelectorAll('.side-tab').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.side-tab').forEach(b => b.classList.toggle('active', b === btn));
    const tab = btn.dataset.tab;
    $s('#panel-local').classList.toggle('active', tab === 'local');
    $s('#panel-stream').classList.toggle('active', tab === 'stream');
    if (tab === 'stream') $s('#stream-search').focus();
  };
});
// 流媒体面板的收起按钮与本地面板共用同一个侧栏收放逻辑
$s('#btn-collapse2').onclick = () => $s('#btn-collapse').click();

/* ---------------- 搜索（Pro beat0.0.1：本地 × 流媒体统一视图） ---------------- */
function setStreamStatus(text, warn) {
  const el = $s('#stream-status');
  el.textContent = text || '';
  el.classList.toggle('warn', !!warn);
}

/* 本地命中：标题/艺术家/专辑模糊匹配，附格式与码率 */
function localMatches(kw) {
  const q = kw.toLowerCase();
  const out = [];
  for (const t of state.library.tracks) {
    const mc = state.library.metaCache[t.path] || {};
    const name = (mc.title || t.name.replace(/\.[^.]+$/, '')).toLowerCase();
    const artist = (mc.artist || '').toLowerCase();
    const album = (mc.album || '').toLowerCase();
    if (name.includes(q) || artist.includes(q) || album.includes(q)) {
      out.push({
        track: t,
        title: mc.title || t.name.replace(/\.[^.]+$/, ''),
        artist: mc.artist || '未知艺术家',
        album: mc.album || '',
        fmt: (mc.codec || t.name.split('.').pop()).toUpperCase() + (mc.bitrate ? ' · ' + Math.round(mc.bitrate / 1000) + 'kbps' : ''),
        fav: state.favorites.has(t.path)
      });
    }
    if (out.length >= 30) break;
  }
  return out;
}

/* 流媒体条目是否已有本地：标题+艺术家归一化匹配 */
function localDupOf(song) {
  const norm = (s) => String(s || '').toLowerCase().replace(/[\s（）()\[\]【】·\-_.]/g, '');
  const n = norm(song.name), a = norm(song.artist);
  if (!n) return null;
  for (const t of state.library.tracks) {
    const mc = state.library.metaCache[t.path] || {};
    const tn = norm(mc.title || t.name.replace(/\.[^.]+$/, ''));
    const ta = norm(mc.artist);
    if (tn === n && (!a || !ta || ta === a || ta.includes(a) || a.includes(ta))) {
      const lossless = /\.(flac|wav|ape|aiff?|alac|tta|wv|dsf|dff)$/i.test(t.path);
      return { fav: state.favorites.has(t.path), lossless, path: t.path };
    }
  }
  return null;
}

async function doSearch() {
  const kw = $s('#stream-search').value.trim();
  if (!kw || streamState.searching) return;
  streamState.searching = true;
  setStreamStatus('搜索中…（本地 + 网易云 + QQ 音乐）');
  try {
    // 本地 + 双平台并行
    const local = localMatches(kw);
    const [ne, qq] = await Promise.allSettled([
      window.mine.streamSearch({ provider: 'netease', keywords: kw, limit: 10, offset: 0 }),
      window.mine.streamSearch({ provider: 'qq', keywords: kw, limit: 10, offset: 0 }),
    ]);
    streamState.unified = {
      kw,
      local,
      netease: ne.status === 'fulfilled' ? (ne.value.songs || []) : [],
      qq: qq.status === 'fulfilled' ? (qq.value.songs || []) : [],
      neErr: ne.status === 'rejected' ? String(ne.reason && ne.reason.message || ne.reason) : '',
      qqErr: qq.status === 'rejected' ? String(qq.reason && qq.reason.message || qq.reason) : '',
    };
    // 播放队列 = 流媒体两组结果合并（本地条目走本地播放链路）
    streamState.results = [...streamState.unified.netease, ...streamState.unified.qq];
    streamState.index = -1;
    renderUnified();
    const total = local.length + streamState.results.length;
    setStreamStatus(total
      ? `本地 ${local.length} 首 · 网易云 ${streamState.unified.netease.length} 首 · QQ ${streamState.unified.qq.length} 首`
      : '未找到相关曲目');
  } catch (e) {
    setStreamStatus('搜索失败：' + (e.message || e), true);
  } finally {
    streamState.searching = false;
  }
}

$s('#btn-stream-search').onclick = doSearch;
$s('#stream-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

/* ---------------- 结果渲染（Pro beat0.0.1：统一分组视图，本地置顶） ---------------- */
function qualityBadge(song) {
  // 音质标签：按平台返回的版权/音质字段启发式标注
  if (song.fee === 1) return '<span class="s-badge">VIP</span>';
  if (song.fee === 4) return '<span class="s-badge">付费</span>';
  if (song.hires || song.hr) return '<span class="s-badge sq">Hi-Res</span>';
  if (song.sq || (song.privilege && song.privilege.pl >= 320000)) return '<span class="s-badge sq">无损</span>';
  return '<span class="s-badge std">标准</span>';
}

function renderUnified() {
  const u = streamState.unified;
  const box = $s('#stream-list');
  box.innerHTML = '';
  if (!u) return;
  const frag = document.createDocumentFragment();

  // —— 本地组（置顶） ——
  if (u.local.length) {
    const head = document.createElement('div');
    head.className = 'stream-group-head local';
    head.textContent = `本地曲库（${u.local.length}）`;
    frag.appendChild(head);
    u.local.forEach((m) => {
      const row = document.createElement('div');
      row.className = 'stream-row local-row';
      row.innerHTML = `
        <div class="s-texts">
          <div class="s-name">${escapeHtml(m.title)}${m.fav ? ' <span class="s-badge fav">♥ 已收藏</span>' : ''}</div>
          <div class="s-sub">${escapeHtml(m.artist)}${m.album ? ' · ' + escapeHtml(m.album) : ''}</div>
        </div>
        <span class="s-badge local">${escapeHtml(m.fmt)}</span>`;
      row.onclick = () => {
        state.queue = [m.track];
        playAt(0);
      };
      frag.appendChild(row);
    });
  }

  // —— 流媒体组 ——
  const groups = [
    ['网易云音乐', u.netease, u.neErr],
    ['QQ 音乐', u.qq, u.qqErr],
  ];
  for (const [label, songs, err] of groups) {
    const head = document.createElement('div');
    head.className = 'stream-group-head';
    head.textContent = err ? `${label}（失败：${err}）` : `${label}（${songs.length}）`;
    frag.appendChild(head);
    songs.forEach((song) => {
      const gi = streamState.results.indexOf(song);
      const dup = localDupOf(song);
      const row = document.createElement('div');
      row.className = 'stream-row' + (gi === streamState.index ? ' active' : '');
      const dur = song.duration ? Math.floor(song.duration / 60000) + ':' + String(Math.floor(song.duration / 1000) % 60).padStart(2, '0') : '';
      row.innerHTML = `
        ${song.cover ? `<img src="${song.cover}" loading="lazy" alt="" onerror="this.style.visibility='hidden'">` : '<img alt="" style="visibility:hidden">'}
        <div class="s-texts">
          <div class="s-name">${escapeHtml(song.name)}${dup ? `<span class="s-badge dup" title="${dup.lossless ? '曲库中已有无损版本' : '曲库中已有此曲'}">✔ ${dup.fav ? '已收藏' : dup.lossless ? '已有本地无损' : '已有本地'}</span>` : ''}</div>
          <div class="s-sub">${escapeHtml(song.artist || '未知艺人')}${song.album ? ' · ' + escapeHtml(song.album) : ''}${dur ? ' · ' + dur : ''}</div>
        </div>
        ${qualityBadge(song)}`;
      row.onclick = () => playStreamAt(gi);
      frag.appendChild(row);
    });
  }
  box.appendChild(frag);
}

function renderResults() { renderUnified(); } // 兼容旧调用

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------- 播放 ---------------- */
async function playStreamAt(i) {
  const song = streamState.results[i];
  if (!song) return;
  streamState.index = i;
  renderResults();
  const pname = song.provider === 'qq' ? 'QQ 音乐' : '网易云音乐';
  setStreamStatus(`正在获取播放地址：${song.name}…`);

  let r;
  try {
    r = await window.mine.streamSongUrl({
      provider: song.provider,
      id: song.id,
      mid: song.mid,
      mediaMid: song.mediaMid,
      quality: 'hires',
    });
  } catch (e) {
    setStreamStatus(`获取播放地址失败：${e.message || e}`, true);
    return;
  }
  // 等待期间用户可能已点击其他曲目
  if (streamState.index !== i) return;

  if (!r || !r.playable || !r.url) {
    setStreamStatus(`${song.name}：${(r && r.message) || '无法播放'}`, true);
    return;
  }

  setStreamStatus(`${pname} · ${r.quality || ''} · 独占输出中`);
  // 交给 player.js 的流媒体播放入口（独占引擎解码）
  if (window.annieStreamPlay) {
    window.annieStreamPlay({
      url: r.url,
      headers: r.headers || null,
      title: song.name,
      artist: song.artist,
      album: song.album || '',
      cover: song.cover || '',
      duration: song.duration ? song.duration / 1000 : 0,
      provider: song.provider,
      quality: r.quality || '',
    });
  }
}

// 供 player.js 在曲目自然结束时调用（自动播放下一首搜索结果）
window.annieStream = {
  playNext() {
    if (streamState.results.length && streamState.index < streamState.results.length - 1) {
      playStreamAt(streamState.index + 1);
    }
  },
  /** 当前流媒体曲目的时长兜底（引擎探测不到 duration 时使用）。 */
  currentFallbackDuration() {
    const s = streamState.results[streamState.index];
    return s && s.duration ? s.duration / 1000 : 0;
  },
};
/* Pro beat0.0.1：外部入口——带关键词跳到流媒体标签页并执行统一搜索 */
window.annieStreamSearch = (kw) => {
  document.querySelector('.side-tab[data-tab="stream"]')?.click();
  $s('#stream-search').value = kw;
  doSearch();
};

/* ================= 登录 ================= */
let qrPollTimer = null;

async function refreshLoginStatus() {
  const provider = $s('#stream-provider').value;
  const el = $s('#stream-login-status');
  const btn = $s('#btn-stream-login');
  try {
    const s = await window.mine.streamLoginStatus({ provider });
    if (s.loggedIn) {
      el.textContent = provider === 'qq' ? `已登录 QQ ${s.uin}` : `已登录 ${s.nickname || '网易云用户'}`;
      el.classList.add('logged');
      btn.textContent = '退出';
    } else {
      el.textContent = '未登录（VIP 曲目受限）';
      el.classList.remove('logged');
      btn.textContent = '登录';
    }
  } catch {
    el.textContent = '登录状态查询失败';
    el.classList.remove('logged');
    btn.textContent = '登录';
  }
}

$s('#stream-provider').addEventListener('change', refreshLoginStatus);

$s('#btn-stream-login').onclick = async () => {
  const provider = $s('#stream-provider').value;
  if ($s('#btn-stream-login').textContent === '退出') {
    await window.mine.streamLogout({ provider });
    refreshLoginStatus();
    return;
  }
  if (provider === 'netease') openNeteaseQrLogin();
  else openQQCookieLogin();
};

function closeLoginModal() {
  $s('#login-modal').classList.add('hidden');
  if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; }
}
$s('#login-modal-cancel').onclick = closeLoginModal;

/* ---- 网易云扫码 ---- */
async function openNeteaseQrLogin() {
  $s('#login-modal-title').textContent = '网易云音乐扫码登录';
  $s('#login-qr-box').classList.remove('hidden');
  $s('#login-cookie-box').classList.add('hidden');
  $s('#login-modal-ok').classList.add('hidden');
  $s('#login-qr-tip').textContent = '正在获取二维码…';
  $s('#login-qr-img').removeAttribute('src');
  $s('#login-modal').classList.remove('hidden');
  try {
    const { key, qrimg } = await window.mine.streamNeteaseQrCreate();
    if (qrimg) $s('#login-qr-img').src = qrimg;
    $s('#login-qr-tip').textContent = '请使用网易云音乐 App 扫码';
    qrPollTimer = setInterval(async () => {
      try {
        const r = await window.mine.streamNeteaseQrCheck({ key });
        if (r.code === 800) {
          $s('#login-qr-tip').textContent = '二维码已过期，请重新打开';
          clearInterval(qrPollTimer); qrPollTimer = null;
        } else if (r.code === 802) {
          $s('#login-qr-tip').textContent = '已扫码，请在手机上确认登录';
        } else if (r.success) {
          closeLoginModal();
          refreshLoginStatus();
          setStreamStatus('网易云登录成功');
        }
      } catch { /* 网络抖动时继续轮询 */ }
    }, 2000);
  } catch (e) {
    $s('#login-qr-tip').textContent = '获取二维码失败：' + (e.message || e);
  }
}

/* ---- QQ 音乐 Cookie ---- */
function openQQCookieLogin() {
  $s('#login-modal-title').textContent = 'QQ 音乐登录（粘贴 Cookie）';
  $s('#login-qr-box').classList.add('hidden');
  $s('#login-cookie-box').classList.remove('hidden');
  $s('#login-modal-ok').classList.remove('hidden');
  $s('#login-cookie-tip').textContent = '';
  $s('#login-cookie-tip').className = '';
  $s('#login-modal').classList.remove('hidden');
}

$s('#login-modal-ok').onclick = async () => {
  const cookie = $s('#login-cookie-input').value.trim();
  const tip = $s('#login-cookie-tip');
  if (!cookie) { tip.textContent = '请先粘贴 Cookie'; tip.className = 'err'; return; }
  try {
    const r = await window.mine.streamQqSetCookie({ cookie });
    if (r.ok) {
      tip.textContent = `登录成功（QQ ${r.uin}${r.hasKey ? '' : '，缺少播放密钥，VIP 曲目可能仍受限'}）`;
      tip.className = 'ok';
      setTimeout(() => { closeLoginModal(); refreshLoginStatus(); }, 800);
    } else {
      tip.textContent = 'Cookie 中未找到有效 uin，请确认复制了 y.qq.com 域下的完整 Cookie';
      tip.className = 'err';
    }
  } catch (e) {
    tip.textContent = '登录失败：' + (e.message || e);
    tip.className = 'err';
  }
};

// 启动时恢复登录状态显示
refreshLoginStatus();

})();
