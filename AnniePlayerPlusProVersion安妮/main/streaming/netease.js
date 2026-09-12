'use strict';
// 网易云音乐接入：搜索 + 播放地址 + 歌词。
// 基于 NeteaseCloudMusicApi（weapi/eapi 加密由库内部完成），匿名模式可搜索并播放免费曲目。
// 播放策略移植自 Mineradio：按音质候选表逐级回退，并对返回 URL 做前 8KB 魔数探测防假链。

const { cloudsearch, song_url_v1, lyric_new, login_qr_key, login_qr_create, login_qr_check, login_status, song_detail } = require('NeteaseCloudMusicApi');

// 登录态（MUSIC_U cookie），由 index.js 在启动时注入 / 扫码成功后更新
let userCookie = '';
function setCookie(cookie) { userCookie = String(cookie || '').trim(); }
function getCookie() { return userCookie; }

// 播放请求头：ffmpeg 拉流时透传
const PLAY_HEADERS = 'Referer: https://music.163.com/\r\n';

// 音质候选（匿名从 hires 开始；登录后含 jymaster 超清母带，SVIP 可用，失败自动回退）
const QUALITY_CANDIDATES_ANON = [
  { level: 'hires', label: '高清臻音' },
  { level: 'lossless', label: '无损' },
  { level: 'exhigh', label: '极高 320k' },
  { level: 'standard', label: '标准 128k' },
];
const QUALITY_CANDIDATES_LOGIN = [
  { level: 'jymaster', label: '超清母带' },
  ...QUALITY_CANDIDATES_ANON,
];

const PROBE_BYTES = 8192;

function audioProbeMagic(buf) {
  if (!buf || !buf.length) return '';
  if (buf.length >= 3 && buf.subarray(0, 3).toString('ascii') === 'ID3') return 'mp3-id3';
  if (buf.length >= 4 && buf.subarray(0, 4).toString('ascii') === 'fLaC') return 'flac';
  if (buf.length >= 4 && buf.subarray(0, 4).toString('ascii') === 'OggS') return 'ogg';
  if (buf.length >= 12 && buf.subarray(4, 8).toString('ascii') === 'ftyp') return 'mp4';
  const scan = Math.min(buf.length - 1, 2048);
  for (let i = 0; i < scan; i++) {
    if (buf[i] === 0xff && (buf[i + 1] & 0xe0) === 0xe0) return 'mpeg-frame';
  }
  return '';
}

async function probeAudioUrl(url, timeoutMs = 2500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      headers: { Referer: 'https://music.163.com/', Range: 'bytes=0-' + (PROBE_BYTES - 1) },
      signal: ctrl.signal,
    });
    const status = resp.status;
    const contentType = String(resp.headers.get('content-type') || '').toLowerCase();
    const chunks = [];
    let bytes = 0;
    if (resp.body && (status === 200 || status === 206)) {
      const reader = resp.body.getReader();
      try {
        while (bytes < PROBE_BYTES) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(Buffer.from(value));
          bytes += value.length;
        }
      } finally { try { await reader.cancel(); } catch { } }
    } else {
      try { await resp.body?.cancel(); } catch { }
    }
    const sample = chunks.length ? Buffer.concat(chunks, bytes).subarray(0, PROBE_BYTES) : Buffer.alloc(0);
    const magic = audioProbeMagic(sample);
    const looksText = /text\/html|application\/(json|xml)|text\/plain/.test(contentType);
    return { ok: (status === 200 || status === 206) && sample.length >= 512 && !looksText && !!magic, magic };
  } catch {
    return { ok: false };
  } finally {
    clearTimeout(timer);
  }
}

/* ---------------- 数据映射 ---------------- */
function mapSong(s) {
  s = s || {};
  const artists = (s.ar || s.artists || []).map(a => ({ id: a.id, name: a.name || '' })).filter(a => a.name);
  const album = s.al || s.album || {};
  return {
    provider: 'netease',
    id: s.id,
    name: s.name || '',
    artist: artists.map(a => a.name).join(' / '),
    album: album.name || '',
    cover: album.picUrl || '',
    duration: s.dt || s.duration || 0,
    fee: s.fee, // 1: VIP, 4: 付费专辑, 8: 低音质免费
  };
}

/* ---------------- 搜索 ---------------- */
async function search(keywords, limit = 20, offset = 0) {
  const kw = String(keywords || '').trim();
  if (!kw) return { provider: 'netease', songs: [], hasMore: false };
  const result = await cloudsearch({ keywords: kw, limit, offset, type: 1, cookie: userCookie || undefined });
  const body = result && result.body;
  const songs = ((body && body.result && body.result.songs) || []).map(mapSong);
  // 封面补图：cloudsearch 结果常缺 picUrl，用 song_detail 批量补齐（移植自 Mineradio）
  const missing = songs.filter(s => !s.cover && s.id);
  if (missing.length) {
    try {
      const detail = await song_detail({ ids: missing.map(s => s.id).join(','), cookie: userCookie || undefined });
      const picMap = new Map();
      for (const d of (detail && detail.body && detail.body.songs) || []) {
        const pic = d.al && d.al.picUrl;
        if (d.id && pic) picMap.set(d.id, pic);
      }
      for (const s of missing) { if (picMap.has(s.id)) s.cover = picMap.get(s.id); }
    } catch { }
  }
  // HTTPS 化（部分 CDN http 链接触发混合内容拦截）
  for (const s of songs) { if (s.cover && s.cover.startsWith('http:')) s.cover = 'https:' + s.cover.slice(5); }
  return {
    provider: 'netease',
    songs,
    offset,
    nextOffset: offset + songs.length,
    hasMore: !!(body && body.result && body.result.hasMore),
  };
}

/* ---------------- 播放地址 ---------------- */
async function songUrl(id, quality = 'hires') {
  const songId = String(id || '').trim();
  if (!songId) return { provider: 'netease', playable: false, message: '缺少歌曲 id' };
  const candidates = userCookie ? QUALITY_CANDIDATES_LOGIN : QUALITY_CANDIDATES_ANON;
  const startIdx = Math.max(0, candidates.findIndex(q => q.level === quality));
  let lastError = null;

  for (const candidate of candidates.slice(startIdx)) {
    try {
      const result = await song_url_v1({ id: songId, level: candidate.level, cookie: userCookie || undefined });
      const list = result && result.body && result.body.data;
      const d = Array.isArray(list) ? list[0] : null;
      if (!d || !d.url) continue;
      if (d.freeTrialInfo) { lastError = 'trial'; continue; } // 试听片段，不可用
      const probe = await probeAudioUrl(d.url);
      if (!probe.ok) { lastError = 'probe-failed'; continue; }
      return {
        provider: 'netease',
        playable: true,
        url: d.url,
        headers: PLAY_HEADERS,
        level: d.level || candidate.level,
        quality: candidate.label,
        br: d.br || 0,
        format: d.type || probe.magic || '',
      };
    } catch (err) {
      lastError = err.message;
    }
  }
  return {
    provider: 'netease',
    playable: false,
    message: lastError === 'trial'
      ? '该曲目需要网易云会员，仅提供试听片段，已跳过'
      : '网易云未返回可播放地址（可能受版权或会员限制）',
  };
}

/* ---------------- 歌词 ---------------- */
async function lyric(id) {
  const result = await lyric_new({ id: String(id || ''), cookie: userCookie || undefined });
  const body = result && result.body;
  const lrc = body && body.lrc && body.lrc.lyric;
  return { provider: 'netease', lrc: lrc || '' };
}

/* ---------------- 扫码登录 ---------------- */
/** 生成登录二维码。返回 { key, qrimg(base64 dataURL) }。 */
async function qrCreate() {
  const keyRes = await login_qr_key({ timestamp: Date.now() });
  const key = keyRes && keyRes.body && keyRes.body.data && keyRes.body.data.unikey;
  if (!key) throw new Error('获取登录二维码 key 失败');
  const createRes = await login_qr_create({ key, qrimg: true, timestamp: Date.now() });
  const qrimg = createRes && createRes.body && createRes.body.data && createRes.body.data.qrimg;
  return { key, qrimg };
}

/** 轮询扫码状态。code: 800 过期 / 801 等待扫码 / 802 待确认 / 803 成功（返回 cookie）。 */
async function qrCheck(key) {
  const res = await login_qr_check({ key, timestamp: Date.now() });
  const body = res && res.body || {};
  if (Number(body.code) === 803 && body.cookie) {
    setCookie(body.cookie);
  }
  return { code: Number(body.code) || 0, message: body.message || '', success: Number(body.code) === 803 };
}

/** 查询登录状态（含昵称）。 */
async function loginStatus() {
  if (!userCookie) return { loggedIn: false };
  try {
    const res = await login_status({ cookie: userCookie, timestamp: Date.now() });
    const profile = res && res.body && res.body.data && res.body.data.profile;
    if (profile && profile.userId) {
      return { loggedIn: true, nickname: profile.nickname || '', userId: profile.userId, vipType: profile.vipType || 0 };
    }
  } catch { }
  return { loggedIn: false };
}

function logout() { setCookie(''); }

module.exports = { search, songUrl, lyric, qrCreate, qrCheck, loginStatus, logout, setCookie, getCookie, PLAY_HEADERS };
