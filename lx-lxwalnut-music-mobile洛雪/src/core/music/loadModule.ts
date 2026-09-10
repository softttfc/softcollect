import { updateWebDAVMusicMeta } from '@/core/webdavMusic/drive'

export const loadWebDAVModule = async () => ({
  updateWebDAVMusicMeta,
})

export const loadLyricModule = async () => ({
  getLyric: async () => null,
  fetchLyric: async () => 'NULL',
})
