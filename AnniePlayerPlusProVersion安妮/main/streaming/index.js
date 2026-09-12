'use strict';
// 流媒体统一入口：按平台路由到对应模块；登录态 Cookie 持久化到 userData。

const fs = require('fs');
const path = require('path');
const netease = require('./netease');
const qq = require('./qq');

const providers = { netease, qq };

let storeFile = null;

function init(app) {
  storeFile = path.join(app.getPath('userData'), 'stream-cookies.json');
  try {
    const saved = JSON.parse(fs.readFileSync(storeFile, 'utf8'));
    if (saved.netease) netease.setCookie(saved.netease);
    if (saved.qq) qq.setCookie(saved.qq);
  } catch { }
}

function persist() {
  if (!storeFile) return;
  try {
    fs.writeFileSync(storeFile, JSON.stringify({ netease: netease.getCookie(), qq: qq.getCookie() }), 'utf8');
  } catch { }
}

function getProvider(name) {
  const p = providers[String(name || 'netease').toLowerCase()];
  if (!p) throw new Error('未知流媒体平台: ' + name);
  return p;
}

async function search({ provider, keywords, limit, offset }) {
  return getProvider(provider).search(keywords, limit || 20, offset || 0);
}

async function songUrl({ provider, id, mid, mediaMid, quality }) {
  const p = getProvider(provider);
  return p === qq ? p.songUrl(mid || id, mediaMid, quality) : p.songUrl(id, quality);
}

async function lyric({ provider, id, mid }) {
  const p = getProvider(provider);
  return p === qq ? p.lyric(mid || id) : p.lyric(id);
}

/* ---------------- 登录 ---------------- */
async function loginStatus({ provider }) {
  const s = await getProvider(provider).loginStatus();
  return { provider, ...s };
}

async function qrCreateNetease() {
  return netease.qrCreate();
}

async function qrCheckNetease({ key }) {
  const r = await netease.qrCheck(key);
  if (r.success) persist();
  return r;
}

async function setQQCookie({ cookie }) {
  const r = qq.setCookie(cookie);
  if (r.ok) persist();
  return r;
}

async function logout({ provider }) {
  getProvider(provider).logout();
  persist();
  return { ok: true };
}

module.exports = { init, search, songUrl, lyric, loginStatus, qrCreateNetease, qrCheckNetease, setQQCookie, logout };
