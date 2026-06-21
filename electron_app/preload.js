
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('svBridge', {
  minimize:    () => ipcRenderer.send('sv-minimize'),
  toggleMax:   () => ipcRenderer.send('sv-maximize'),
  close:       () => ipcRenderer.send('sv-close'),
  isMaximized: () => ipcRenderer.invoke('sv-is-maximized'),
});

// pywebview compatibility shim — keeps alpha.py window-control calls working
// without any changes to the frontend HTML/JS.
contextBridge.exposeInMainWorld('pywebview', {
  api: {
    minimize:   () => ipcRenderer.send('sv-minimize'),
    toggle_max: () => ipcRenderer.send('sv-maximize'),
    close_win:  () => ipcRenderer.send('sv-close'),
  },
});
