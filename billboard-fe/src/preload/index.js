import { contextBridge, ipcRenderer } from 'electron'
import { electronAPI } from '@electron-toolkit/preload'

const api = {
  startPython: (source) => ipcRenderer.invoke('start-python', source),
  stopPython: () => ipcRenderer.invoke('stop-python'),
  onPythonMessage: (callback) => ipcRenderer.on('python-message', (_event, value) => callback(value)),
  removePythonListener: () => ipcRenderer.removeAllListeners('python-message')
}

if (process.contextIsolated) {
  try {
    contextBridge.exposeInMainWorld('electron', electronAPI)
    contextBridge.exposeInMainWorld('api', api)
  } catch (error) {
    console.error(error)
  }
} else {
  window.electron = electronAPI
  window.api = api
}
