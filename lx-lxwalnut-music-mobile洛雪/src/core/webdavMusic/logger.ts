import {
  existsFile,
  appendFile,
  writeFile,
  unlink,
} from '@/utils/fs'
import settingState from '@/store/setting/state'

const getWebDAVLogPath = () => {
  const downloadDir = settingState.setting['download.path'] || '/storage/emulated/0/Music/LX-N Music'
  return `${downloadDir}/webdav_debug.log`
}

let webDAVTempLog: Array<{ time: string; type: 'LOG' | 'WARN' | 'ERROR'; text: string }> = []

const writeWebDAVLog = async (msg: string) => {
  if (!settingState.setting['common.isEnableWebDAVLog']) return
  const logPath = getWebDAVLogPath()
  try {
    await appendFile(logPath, '\n' + msg)
  } catch (err) {
    console.error('[WebDAV] Failed to write log:', err)
  }
}

const formatLogMsg = (...msgs: unknown[]) => {
  return msgs
    .map((m: unknown) => {
      if (typeof m === 'string') return m
      if (m instanceof Error) return m.stack ?? m.message
      return JSON.stringify(m)
    })
    .join(' ')
}

export const webDAVLog = {
  info(...msgs: unknown[]) {
    const msg = formatLogMsg(...msgs)
    console.log('[WebDAV]', msg)
    if (!settingState.setting['common.isEnableWebDAVLog']) return
    const time = new Date().toISOString()
    void writeWebDAVLog(`${time} LOG ${msg}`)
  },
  warn(...msgs: unknown[]) {
    const msg = formatLogMsg(...msgs)
    console.warn('[WebDAV]', msg)
    if (!settingState.setting['common.isEnableWebDAVLog']) return
    const time = new Date().toISOString()
    void writeWebDAVLog(`${time} WARN ${msg}`)
  },
  error(...msgs: unknown[]) {
    const msg = formatLogMsg(...msgs)
    console.error('[WebDAV]', msg)
    if (!settingState.setting['common.isEnableWebDAVLog']) return
    const time = new Date().toISOString()
    void writeWebDAVLog(`${time} ERROR ${msg}`)
  },
}

export const initWebDAVLog = async () => {
  if (!settingState.setting['common.isEnableWebDAVLog']) return
  const logPath = getWebDAVLogPath()
  try {
    const isExists = await existsFile(logPath)
    if (!isExists) await writeFile(logPath, `[WebDAV] Log initialized at ${new Date().toISOString()}\n`)
  } catch (err) {
    console.error('[WebDAV] Failed to init log:', err)
  }
}

export const clearWebDAVLogs = async () => {
  if (!settingState.setting['common.isEnableWebDAVLog']) return
  const logPath = getWebDAVLogPath()
  try {
    await unlink(logPath)
    await writeFile(logPath, `[WebDAV] Log cleared at ${new Date().toISOString()}\n`)
  } catch (err) {
    console.error('[WebDAV] Failed to clear log:', err)
  }
}
