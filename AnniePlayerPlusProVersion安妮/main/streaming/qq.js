'use strict';
// QQ 音乐接入：搜索 + 播放地址 + 歌词。
// 实现移植自 Mineradio（GPL-3.0）server.js 的 QQ 音乐部分：
// zzc 签名搜索（u.y.qq.com/cgi-bin/musics.fcg）+ vkey 播放地址（musicu.fcg）+ 播放链接魔数探测。
// 匿名模式（uin=0）即可搜索与播放免费曲目；VIP 曲目返回明确错误，由前端提示。

const crypto = require('crypto');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';
const QQ_MUSICU_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg';
const QQ_SMARTBOX_URL = 'https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg';
const QQ_HEADERS = { Referer: 'https://y.qq.com/', 'User-Agent': UA };

// 播放请求头：ffmpeg 拉流时透传
const PLAY_HEADERS = 'Referer: https://y.qq.com/\r\n';

const QUALITY_TEMPLATES = [
  { prefix: 'RS01', ext: '.flac', level: 'hires', label: 'Hi-Res FLAC' },
  { prefix: 'F000', ext: '.flac', level: 'lossless', label: '无损 FLAC' },
  { prefix: 'M800', ext: '.mp3', level: 'exhigh', label: '320k MP3' },
  { prefix: 'M500', ext: '.mp3', level: 'standard', label: '128k MP3' },
  { prefix: 'C400', ext: '.m4a', level: 'aac', label: 'AAC/M4A' },
];

const VKEY_TIMEOUT_MS = 6000;
const PROBE_TOTAL_MS = 6200;
const PROBE_ATTEMPT_MS = 2000;
const PROBE_BYTES = 8192;

/* ---------------- 登录态（手动粘贴 Cookie），由 index.js 在启动时注入 ---------------- */
let userCookie = '';

function parseCookieString(str) {
  const obj = {};
  for (const part of String(str || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) obj[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return obj;
}
function normalizeUin(raw) {
  const digits = String(raw || '').replace(/\D/g, '');
  return digits.replace(/^0+/, '') || digits;
}
function cookieUin(obj) {
  const raw = Number(obj.login_type) === 2 ? (obj.wxuin || obj.uin || obj.p_uin) : (obj.uin || obj.qqmusic_uin || obj.wxuin || obj.p_uin);
  return normalizeUin(raw);
}
function cookieMusicKey(obj) {
  return obj.qm_keyst || obj.qqmusic_key || obj.music_key || obj.p_skey || obj.skey ||
    obj.psrf_qqaccess_token || obj.wxrefresh_token || obj.wxskey || '';
}

/** 设置 Cookie，返回解析结果 { ok, uin, hasKey }。 */
function setCookie(cookie) {
  userCookie = String(cookie || '').trim();
  const obj = parseCookieString(userCookie);
  const uin = cookieUin(obj);
  return { ok: !!uin, uin: uin || '', hasKey: !!cookieMusicKey(obj) };
}
function getCookie() { return userCookie; }
function logout() { userCookie = ''; }
function loginStatus() {
  const obj = parseCookieString(userCookie);
  const uin = cookieUin(obj);
  return uin ? { loggedIn: true, uin, hasKey: !!cookieMusicKey(obj) } : { loggedIn: false };
}

/* ---------------- 基础请求 ---------------- */
async function fetchJson(url, opts = {}, body) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs || 10000);
  try {
    const resp = await fetch(url, {
      method: opts.method || 'GET',
      headers: opts.headers || {},
      body,
      signal: ctrl.signal,
    });
    const text = await resp.text();
    try { return JSON.parse(text); } catch { return null; }
  } finally {
    clearTimeout(timer);
  }
}

function audioProbeMagic(buf) {
  if (!buf || !buf.length) return '';
  if (buf.length >= 3 && buf.subarray(0, 3).toString('ascii') === 'ID3') return 'mp3-id3';
  if (buf.length >= 4 && buf.subarray(0, 4).toString('ascii') === 'fLaC') return 'flac';
  if (buf.length >= 4 && buf.subarray(0, 4).toString('ascii') === 'OggS') return 'ogg';
  if (buf.length >= 12 && buf.subarray(0, 4).toString('ascii') === 'RIFF' && buf.subarray(8, 12).toString('ascii') === 'WAVE') return 'wave';
  if (buf.length >= 12 && buf.subarray(4, 8).toString('ascii') === 'ftyp') return 'mp4';
  const scan = Math.min(buf.length - 1, 2048);
  for (let i = 0; i < scan; i++) {
    if (buf[i] === 0xff && (buf[i + 1] & 0xe0) === 0xe0) return 'mpeg-frame';
  }
  return '';
}

/** 下载前 8KB 验证返回的是真实音频而非错误页。 */
async function probeAudioUrl(url, timeoutMs = PROBE_ATTEMPT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), Math.max(800, timeoutMs));
  try {
    const resp = await fetch(url, {
      headers: { ...QQ_HEADERS, Range: 'bytes=0-' + (PROBE_BYTES - 1) },
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
    return { ok: (status === 200 || status === 206) && sample.length >= 512 && !looksText && !!magic, status, magic };
  } catch (err) {
    return { ok: false, status: 0, reason: err && err.name === 'AbortError' ? 'timeout' : 'network' };
  } finally {
    clearTimeout(timer);
  }
}

/* ---------------- 数据映射 ---------------- */
function qqAlbumCover(albumMid, size = 300) {
  if (!albumMid) return '';
  return `https://y.qq.com/music/photo_new/T002R${size}x${size}M000${albumMid}.jpg?max_age=2592000`;
}

function mapQQArtists(raw) {
  return (raw || [])
    .map(a => ({ id: a && a.id, mid: a && a.mid, name: (a && (a.name || a.title)) || '' }))
    .filter(a => a.name);
}

function mapQQTrack(track, fallback = {}) {
  track = track || {};
  const album = track.album || {};
  const artists = mapQQArtists(track.singer || []);
  const mid = track.mid || fallback.mid || fallback.songmid || '';
  const albumMid = album.mid || album.pmid || '';
  return {
    provider: 'qq',
    id: mid,
    mid,
    songmid: mid,
    mediaMid: track.file && track.file.media_mid,
    name: track.name || track.title || fallback.name || '',
    artist: artists.map(a => a.name).join(' / ') || fallback.artist || '',
    album: album.name || album.title || fallback.album || '',
    albumMid,
    cover: qqAlbumCover(albumMid, 300) || fallback.cover || '',
    duration: (Number(track.interval) || 0) * 1000,
    fee: track.pay && Number(track.pay.pay_play) ? 1 : 0,
  };
}

function mapQQSmartSong(item) {
  item = item || {};
  const mid = item.mid || item.songmid || item.id || '';
  return {
    provider: 'qq', id: mid, mid, songmid: mid,
    name: item.name || item.title || '',
    artist: item.singer || '',
    album: '', cover: '', duration: 0, fee: 0,
  };
}

/* ---------------- zzc 签名 ---------------- */
function qqSearchSign(text) {
  const hash = crypto.createHash('sha1').update(text).digest('hex');
  const part1 = [23, 14, 6, 36, 16, 40, 7, 19].map(i => hash[i]).join('');
  const part2 = [16, 1, 32, 12, 19, 27, 8, 5].map(i => hash[i]).join('');
  const scramble = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179];
  const bytes = scramble.map((v, i) => v ^ parseInt(hash.slice(i * 2, i * 2 + 2), 16));
  const middle = Buffer.from(bytes).toString('base64').replace(/[\\/+=]/g, '');
  return `zzc${part1}${middle}${part2}`.toLowerCase();
}

/* ---------------- 搜索 ---------------- */
async function qqFullSongSearch(keywords, limit, offset) {
  limit = Math.max(1, Math.min(30, Number(limit) || 12));
  offset = Math.max(0, Number(offset) || 0);
  const pageNumber = Math.floor(offset / limit) + 1;
  const payload = {
    comm: {
      ct: '11', cv: '14090508', v: '14090508', tmeAppID: 'qqmusic',
      phonetype: 'EBG-AN10', os_ver: '12', OpenUDID: '0', QIMEI36: '0',
      udid: '0', chid: '0', aid: '0', oaid: '0', taid: '0', tid: '0',
      wid: '0', uid: '0', sid: '0', modeSwitch: '6', teenMode: '0',
      ui_mode: '2', nettype: '1020',
    },
    req: {
      module: 'music.search.SearchCgiService',
      method: 'DoSearchForQQMusicMobile',
      param: {
        search_type: 0,
        searchid: String(Date.now()) + String(Math.random()).slice(2, 8),
        query: keywords,
        page_num: pageNumber,
        num_per_page: limit,
        highlight: 0, nqc_flag: 0, multi_zhida: 0,
        cat: 2, grp: 1, sin: offset, sem: 0,
      },
    },
  };
  const bodyText = JSON.stringify(payload);
  const json = await fetchJson(
    'https://u.y.qq.com/cgi-bin/musics.fcg?sign=' + qqSearchSign(bodyText),
    {
      method: 'POST',
      timeoutMs: 10000,
      headers: { 'User-Agent': 'QQMusic 14090508(android 12)', 'Content-Type': 'application/json' },
    },
    bodyText
  );
  const data = json && json.req && json.req.data;
  const body = data && (data.body || data);
  const items = body && (body.item_song || (body.song && body.song.list) || body.list);
  return (Array.isArray(items) ? items : [])
    .map(item => mapQQTrack((item && (item.track_info || item.songInfo || item.songinfo || item.song)) || item))
    .filter(s => s && s.name && (s.mid || s.id));
}

async function qqSmartboxSearch(keywords, limit) {
  const u = new URL(QQ_SMARTBOX_URL);
  u.searchParams.set('format', 'json');
  u.searchParams.set('key', keywords);
  u.searchParams.set('g_tk', '5381');
  u.searchParams.set('loginUin', '0');
  u.searchParams.set('hostUin', '0');
  u.searchParams.set('inCharset', 'utf8');
  u.searchParams.set('outCharset', 'utf-8');
  u.searchParams.set('notice', '0');
  u.searchParams.set('platform', 'yqq.json');
  u.searchParams.set('needNewCode', '0');
  const json = await fetchJson(u.toString(), { headers: QQ_HEADERS });
  const items = json && json.data && json.data.song && json.data.song.itemlist;
  return (Array.isArray(items) ? items : []).slice(0, Math.max(1, Math.min(limit || 6, 10))).map(mapQQSmartSong);
}

async function search(keywords, limit = 20, offset = 0) {
  const kw = String(keywords || '').trim();
  if (!kw) return { provider: 'qq', songs: [], hasMore: false };
  let songs = [];
  try {
    songs = await qqFullSongSearch(kw, limit, offset);
  } catch (err) {
    console.warn('[stream/qq] 主搜索失败，回退 smartbox:', err.message);
  }
  if (!songs.length) {
    try { songs = await qqSmartboxSearch(kw, limit); } catch (err) {
      console.warn('[stream/qq] smartbox 搜索失败:', err.message);
    }
  }
  return { provider: 'qq', songs, offset, nextOffset: offset + songs.length, hasMore: songs.length >= limit };
}

/* ---------------- 播放地址 ---------------- */
async function songUrl(mid, mediaMid, quality = 'hires') {
  const songmid = String(mid || '').trim();
  if (!songmid) return { provider: 'qq', playable: false, message: '缺少歌曲 mid' };
  const guid = String(10000000 + Math.floor(Math.random() * 90000000));
  // 登录态：uin + musicKey（authst）决定可获取的音质与 VIP 曲目权限
  const cookieObj = parseCookieString(userCookie);
  const uin = cookieUin(cookieObj) || '0';
  const musicKey = cookieMusicKey(cookieObj);
  const loggedIn = uin !== '0' && !!musicKey;
  const startIdx = Math.max(0, QUALITY_TEMPLATES.findIndex(t => t.level === quality));
  const templates = QUALITY_TEMPLATES.slice(startIdx < 0 ? 0 : startIdx);

  const mediaIds = [];
  if (mediaMid) mediaIds.push(String(mediaMid).trim());
  if (!mediaIds.includes(songmid)) mediaIds.push(songmid);
  const fileCandidates = mediaIds.flatMap(mediaId =>
    templates.map(t => ({ ...t, mediaId, filename: t.prefix + mediaId + t.ext }))
  );
  const filenames = fileCandidates.map(f => f.filename);

  const param = {
    guid,
    songmid: filenames.map(() => songmid),
    songtype: filenames.map(() => 0),
    uin, loginflag: 1, platform: '20',
    filename: filenames,
  };
  const comm = { uin, format: 'json', ct: musicKey ? 19 : 24, cv: 0 };
  if (musicKey) comm.authst = musicKey;
  const reqHeaders = { ...QQ_HEADERS, 'Content-Type': 'application/json;charset=UTF-8' };
  if (userCookie) reqHeaders.Cookie = userCookie;
  const json = await fetchJson(QQ_MUSICU_URL, {
    method: 'POST',
    timeoutMs: VKEY_TIMEOUT_MS,
    headers: reqHeaders,
  }, JSON.stringify({
    comm,
    req_0: { module: 'vkey.GetVkeyServer', method: 'CgiGetVkey', param },
  }));

  const data = json && json.req_0 && json.req_0.data;
  const infos = (data && Array.isArray(data.midurlinfo)) ? data.midurlinfo : [];
  const purlInfos = infos.filter(i => i && i.purl);
  const sips = (data && Array.isArray(data.sip) && data.sip.length ? data.sip : ['https://ws.stream.qqmusic.qq.com/']).filter(Boolean);

  const probeDeadline = Date.now() + PROBE_TOTAL_MS;
  let playableInfo = null;
  let playableUrl = '';
  for (const info of purlInfos) {
    if (playableUrl) break;
    for (const sip of sips) {
      if (probeDeadline - Date.now() < 300) break;
      const url = String(sip) + String(info.purl);
      const probe = await probeAudioUrl(url, Math.min(PROBE_ATTEMPT_MS, probeDeadline - Date.now()));
      if (probe.ok) { playableInfo = info; playableUrl = url; break; }
    }
  }

  if (playableUrl && playableInfo) {
    const meta = fileCandidates.find(f => f.filename === playableInfo.filename) || {};
    return {
      provider: 'qq',
      playable: true,
      url: playableUrl,
      headers: PLAY_HEADERS,
      level: meta.level || '',
      quality: meta.label || playableInfo.filename || '',
    };
  }
  const code = infos[0] && (infos[0].result || infos[0].code);
  const vip = infos.some(i => i && i.purl === '' && (Number(i.result) === 104003 || Number(i.code) === 104003));
  return {
    provider: 'qq',
    playable: false,
    message: vip
      ? (loggedIn ? '该曲目需要 QQ 音乐豪华 VIP/SVIP 会员' : '该曲目需要 QQ 音乐会员，请先在流媒体面板登录')
      : 'QQ 音乐未返回可播放地址（可能受版权限制）' + (loggedIn ? '' : '，可尝试登录后重试'),
    code,
  };
}

/* ---------------- 歌词 ---------------- */
async function lyric(mid) {
  const u = new URL('https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg');
  u.searchParams.set('songmid', String(mid || ''));
  u.searchParams.set('g_tk', '5381');
  u.searchParams.set('loginUin', '0');
  u.searchParams.set('hostUin', '0');
  u.searchParams.set('format', 'json');
  u.searchParams.set('inCharset', 'utf8');
  u.searchParams.set('outCharset', 'utf-8');
  u.searchParams.set('notice', '0');
  u.searchParams.set('platform', 'yqq.json');
  u.searchParams.set('needNewCode', '0');
  const json = await fetchJson(u.toString(), { headers: QQ_HEADERS });
  const lrc = json && (json.lyric || json.data && json.data.lyric);
  return { provider: 'qq', lrc: lrc ? Buffer.from(lrc, 'base64').toString('utf8') : '' };
}

module.exports = { search, songUrl, lyric, setCookie, getCookie, logout, loginStatus, PLAY_HEADERS };
