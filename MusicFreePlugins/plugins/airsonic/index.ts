import axios from "axios";
import CryptoJs = require("crypto-js");

const pageSize = 25;

// 缓存服务器信息
interface ServerInfo {
  version: string;
  type: string;
  serverVersion: string;
  openSubsonic: boolean;
  authMethod: 'token' | 'password'; // 记录认证方式
}

let cachedServerInfo: ServerInfo | null = null;
let lastConfigHash: string | null = null;

// 生成配置哈希用于检测配置变化
function getConfigHash(url: string, username: string, password: string): string {
  return CryptoJs.MD5(`${url}:${username}:${password}`).toString();
}

async function getServerVersion(url, params): Promise<string | null> {
    const response = await axios.default.get(`${url}/rest/ping`, {
        params,
        timeout: 10000
    });
    const data = response.data;
    if (data && data['subsonic-response'] && data['subsonic-response'].version) {
        return data['subsonic-response'].version || "1.16.1";
    }
    return null;
}

// Ping 服务器获取版本信息
async function pingServer(): Promise<ServerInfo | null> {
  const userVariables = env?.getUserVariables() ?? {};
  // @ts-ignore
  let { url, username, password } = userVariables;

  if (!(url && username && password)) {
    return null;
  }

  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    url = `http://${url}`;
  }

  const configHash = getConfigHash(url, username, password);

  // 如果配置没有变化且已有缓存，直接返回缓存
  if (lastConfigHash === configHash && cachedServerInfo) {
    return cachedServerInfo;
  }

  let serverVersion = null;
  // 尝试 token 认证方式 (u+s+t)
  try {
    const salt = Math.random().toString(16).slice(2);
    const tokenParams = {
      u: username,
      s: salt,
      t: CryptoJs.MD5(`${password}${salt}`).toString(CryptoJs.enc.Hex),
      c: "MusicFree",
      v: serverVersion??'0.0.1',
      f: "json",
    };

    if (!serverVersion) {
        serverVersion = await getServerVersion(url, tokenParams);
        tokenParams.v = serverVersion || '1.16.1';
    }

    const response = await axios.get(`${url}/rest/ping`, {
      params: tokenParams,
      timeout: 10000
    });

    const data = response.data;
    if (data && data['subsonic-response'] && data['subsonic-response'].status === 'ok') {
      const subsonicResponse = data['subsonic-response'];

      cachedServerInfo = {
        version: subsonicResponse.version || "1.16.1",
        type: subsonicResponse.type || "airsonic",
        serverVersion: subsonicResponse.version || "unknown",
        openSubsonic: subsonicResponse.openSubsonic || false,
        authMethod: 'token'
      };

      lastConfigHash = configHash;
      return cachedServerInfo;
    }
  } catch (tokenError) {
    console.log('Token认证失败，尝试密码认证:', tokenError.message);
  }

  // 尝试明文密码认证方式 (u+p)
  try {
    const passwordParams = {
      u: username,
      p: password,
      c: "MusicFree",
      v: serverVersion??'0.0.1',
      f: "json",
    };

    if (!serverVersion) {
        serverVersion = await getServerVersion(url, passwordParams);
        passwordParams.v = serverVersion || '1.16.1';
    }

    const response = await axios.get(`${url}/rest/ping`, {
      params: passwordParams,
      timeout: 10000
    });

    const data = response.data;
    if (data && data['subsonic-response'] && data['subsonic-response'].status === 'ok') {
      const subsonicResponse = data['subsonic-response'];

      cachedServerInfo = {
        version: subsonicResponse.version || "1.16.1",
        type: subsonicResponse.type || "airsonic",
        serverVersion: subsonicResponse.version || "unknown",
        openSubsonic: subsonicResponse.openSubsonic || false,
        authMethod: 'password'
      };

      lastConfigHash = configHash;
      return cachedServerInfo;
    }
  } catch (passwordError) {
    console.log('密码认证失败:', passwordError.message);
  }
  return null;
}

async function httpGet(
  urlPath: string,
  params?: Record<string, string | number | boolean>
) {
  const userVariables = env?.getUserVariables() ?? {};
  // @ts-ignore
  let { url, username, password } = userVariables;

  if (!(url && username && password)) {
    return null;
  }

  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    url = `http://${url}`;
  }

  // 确保服务器信息已获取
  const serverInfo = await pingServer();
  if (!serverInfo) {
    console.error('无法连接到服务器');
    return null;
  }

  // 根据服务器支持的认证方式构建参数
  let preParams: Record<string, string>;

  if (serverInfo.authMethod === 'token') {
    // 使用 token 认证 (u+s+t)
    const salt = Math.random().toString(16).slice(2);
    preParams = {
      u: username,
      s: salt,
      t: CryptoJs.MD5(`${password}${salt}`).toString(CryptoJs.enc.Hex),
      c: "MusicFree",
      v: serverInfo.version,
      f: "json",
    };
  } else {
    // 使用密码认证 (u+p)
    preParams = {
      u: username,
      p: password,
      c: "MusicFree",
      v: serverInfo.version,
      f: "json",
    };
  }

  try {
    return (
      await axios.get(`${url}/rest/${urlPath}`, {
        params: {
          ...preParams,
          ...params
        },
        timeout: 15000
      })
    ).data;
  } catch (error) {
    console.error(`请求 ${urlPath} 失败:`, error);
    return null;
  }
}

function formatMusicItem(it) {
  return {
    id: it.id,
    title: it.title,
    artist: it.artist,
    album: it.album,
    duration: it.duration,
    artwork: it.coverArt,
    albumId: it.albumId,
    artistId: it.artistId,
    track: it.track,
    year: it.year,
    genre: it.genre,
    bitRate: it.bitRate,
    size: it.size,
    suffix: it.suffix,
    contentType: it.contentType,
    path: it.path
  }
}

function formatAlbumItem(it) {
  return {
    id: it.id,
    title: it.name,
    artist: it.artist,
    artwork: it.coverArt,
    artistId: it.artistId,
    songCount: it.songCount,
    duration: it.duration,
    created: it.created,
    year: it.year,
    genre: it.genre
  }
}

function formatArtistItem(it) {
  return {
    id: it.id,
    name: it.name,
    avatar: it.artistImageUrl,
    albumCount: it.albumCount,
    starred: it.starred
  }
}

async function searchMusic(query, page) {
  // 根据服务器版本选择搜索API
  const serverInfo = cachedServerInfo;
  const useSearch3 = serverInfo && parseFloat(serverInfo.version) >= 1.4;

  const searchEndpoint = useSearch3 ? 'search3' : 'search2';
  const data = await httpGet(searchEndpoint, {
    query,
    songCount: pageSize,
    songOffset: (page - 1) * pageSize
  });

  if (!data || !data['subsonic-response']) {
    return { isEnd: true, data: [] };
  }

  const searchResult = useSearch3 ?
    data['subsonic-response'].searchResult3 :
    data['subsonic-response'].searchResult2;

  if (!searchResult) {
    return { isEnd: true, data: [] };
  }

  const songs = searchResult.song || [];

  return {
    isEnd: songs.length < pageSize,
    data: songs.map(formatMusicItem)
  }
}

async function searchAlbum(query, page) {
  const serverInfo = cachedServerInfo;
  const useSearch3 = serverInfo && parseFloat(serverInfo.version) >= 1.4;

  const searchEndpoint = useSearch3 ? 'search3' : 'search2';
  const data = await httpGet(searchEndpoint, {
    query,
    albumCount: pageSize,
    albumOffset: (page - 1) * pageSize
  });

  if (!data || !data['subsonic-response']) {
    return { isEnd: true, data: [] };
  }

  const searchResult = useSearch3 ?
    data['subsonic-response'].searchResult3 :
    data['subsonic-response'].searchResult2;

  if (!searchResult) {
    return { isEnd: true, data: [] };
  }

  const albums = searchResult.album || [];

  return {
    isEnd: albums.length < pageSize,
    data: albums.map(formatAlbumItem)
  }
}

async function searchArtist(query, page) {
  const serverInfo = cachedServerInfo;
  const useSearch3 = serverInfo && parseFloat(serverInfo.version) >= 1.4;

  const searchEndpoint = useSearch3 ? 'search3' : 'search2';
  const data = await httpGet(searchEndpoint, {
    query,
    artistCount: pageSize,
    artistOffset: (page - 1) * pageSize
  });

  if (!data || !data['subsonic-response']) {
    return { isEnd: true, data: [] };
  }

  const searchResult = useSearch3 ?
    data['subsonic-response'].searchResult3 :
    data['subsonic-response'].searchResult2;

  if (!searchResult) {
    return { isEnd: true, data: [] };
  }

  const artists = searchResult.artist || [];

  return {
    isEnd: artists.length < pageSize,
    data: artists.map(formatArtistItem)
  }
}

async function getAlbumInfo(albumItem, page) {
  const data = await httpGet('getAlbum', {
    id: albumItem.id
  });

  if (!data || !data['subsonic-response'] || !data['subsonic-response'].album) {
    return { isEnd: true, data: [] };
  }

  const songs = data['subsonic-response'].album.song || [];

  return {
    isEnd: true,
    data: songs.map(formatMusicItem)
  };
}

async function getArtistWorks(artistItem, page, type) {
  if (type === 'album') {
    const data = await httpGet('getArtist', {
      id: artistItem.id
    });

    if (!data || !data['subsonic-response'] || !data['subsonic-response'].artist) {
      return { isEnd: true, data: [] };
    }

    const albums = data['subsonic-response'].artist.album || [];

    return {
      isEnd: true,
      data: albums.map(formatAlbumItem)
    };
  }

  if (type === 'music') {
    // 获取艺术家的所有专辑，然后获取所有歌曲
    const artistData = await httpGet('getArtist', {
      id: artistItem.id
    });

    if (!artistData || !artistData['subsonic-response'] || !artistData['subsonic-response'].artist) {
      return { isEnd: true, data: [] };
    }

    const albums = artistData['subsonic-response'].artist.album || [];
    let allSongs = [];

    for (const album of albums) {
      const albumData = await httpGet('getAlbum', {
        id: album.id
      });

      if (albumData && albumData['subsonic-response'] && albumData['subsonic-response'].album) {
        const songs = albumData['subsonic-response'].album.song || [];
        allSongs = [...allSongs, ...songs];
      }
    }

    return {
      isEnd: true,
      data: allSongs.map(formatMusicItem)
    };
  }

  return { isEnd: true, data: [] };
}

async function getTopLists() {
  try {
    const data = await httpGet('getAlbumList2', {
      type: 'newest',
      size: 20
    });

    const newestAlbums = data?.['subsonic-response']?.albumList2?.album || [];

    const recentData = await httpGet('getAlbumList2', {
      type: 'recent',
      size: 20
    });

    const recentAlbums = recentData?.['subsonic-response']?.albumList2?.album || [];

    const randomData = await httpGet('getAlbumList2', {
      type: 'random',
      size: 20
    });

    const randomAlbums = randomData?.['subsonic-response']?.albumList2?.album || [];

    return [
      {
        title: "最新专辑",
        data: newestAlbums.map(album => ({
          id: album.id,
          title: album.name,
          coverImg: album.coverArt,
          description: `${album.artist} - ${album.songCount} 首歌曲`
        }))
      },
      {
        title: "最近播放",
        data: recentAlbums.map(album => ({
          id: album.id,
          title: album.name,
          coverImg: album.coverArt,
          description: `${album.artist} - ${album.songCount} 首歌曲`
        }))
      },
      {
        title: "随机专辑",
        data: randomAlbums.map(album => ({
          id: album.id,
          title: album.name,
          coverImg: album.coverArt,
          description: `${album.artist} - ${album.songCount} 首歌曲`
        }))
      }
    ];
  } catch (error) {
    console.error('获取榜单失败:', error);
    return [];
  }
}

async function getTopListDetail(topListItem) {
  const albumInfo = await getAlbumInfo({ id: topListItem.id }, 1);
  return {
    ...topListItem,
    musicList: albumInfo.data
  };
}

// 获取服务器状态信息
async function getServerStatus() {
  const serverInfo = await pingServer();
  if (!serverInfo) {
    return {
      connected: false,
      error: '无法连接到服务器'
    };
  }

  return {
    connected: true,
    version: serverInfo.version,
    type: serverInfo.type,
    serverVersion: serverInfo.serverVersion,
    openSubsonic: serverInfo.openSubsonic,
    authMethod: serverInfo.authMethod
  };
}

module.exports = {
  platform: "Airsonic Advanced",
  version: "0.1.0",
  author: 'JumuFeng',
  description: "支持 Airsonic Advanced 服务器，自动检测服务器版本和兼容性",
  appVersion: ">0.1.0-alpha.0",
  srcUrl:
    "https://gitee.com/maotoumao/MusicFreePlugins/raw/v0.1/dist/airsonic/index.js",
  cacheControl: "no-cache",
  userVariables: [
    {
      key: "url",
      name: "服务器地址",
    },
    {
      key: "username",
      name: "用户名",
    },
    {
      key: "password",
      name: "密码",
    },
  ],
  supportedSearchType: ["music", "album", "artist"],

  // 初始化插件时测试服务器连接
  async init() {
    const status = await getServerStatus();
    if (!status.connected) {
      console.warn('服务器连接失败:', status.error);
    }
    return status;
  },

  async search(query, page, type) {
    if (type === "music") {
      return await searchMusic(query, page);
    }
    if (type === "album") {
      return await searchAlbum(query, page);
    }
    if (type === "artist") {
      return await searchArtist(query, page);
    }
  },

  async getAlbumInfo(albumItem, page) {
    return await getAlbumInfo(albumItem, page);
  },

  async getArtistWorks(artistItem, page, type) {
    return await getArtistWorks(artistItem, page, type);
  },

  async getTopLists() {
    return await getTopLists();
  },

  async getTopListDetail(topListItem) {
    return await getTopListDetail(topListItem);
  },

  async getMediaSource(musicItem) {
    const userVariables = env?.getUserVariables() ?? {};
    // @ts-ignore
    let { url, username, password } = userVariables;
    if (!(url && username && password)) {
      return null;
    }
    if (!url.startsWith("http://") && !url.startsWith("https://")) {
      url = `http://${url}`;
    }

    // 确保服务器信息已获取
    const serverInfo = await pingServer();
    if (!serverInfo) {
      console.error('无法连接到服务器');
      return null;
    }

    const urlObj = new URL(`${url}/rest/stream`);
    urlObj.searchParams.append('u', username);
    urlObj.searchParams.append('c', 'MusicFree');
    urlObj.searchParams.append('v', serverInfo.version);
    urlObj.searchParams.append('f', 'json');
    urlObj.searchParams.append('id', musicItem.id);

    // 根据服务器支持的认证方式添加认证参数
    if (serverInfo.authMethod === 'token') {
      const salt = Math.random().toString(16).slice(2);
      urlObj.searchParams.append('s', salt);
      urlObj.searchParams.append('t', CryptoJs.MD5(`${password}${salt}`).toString(CryptoJs.enc.Hex));
    } else {
      urlObj.searchParams.append('p', password);
    }

    return {
      url: urlObj.toString()
    }
  },

  async getLyric(musicItem) {
    try {
      const data = await httpGet('getLyrics', {
        artist: musicItem.artist,
        title: musicItem.title
      });

      if (data && data['subsonic-response'] && data['subsonic-response'].lyrics) {
        return {
          rawLrc: data['subsonic-response'].lyrics.value
        };
      }
    } catch (error) {
      console.log('获取歌词失败:', error);
    }

    return null;
  }
};