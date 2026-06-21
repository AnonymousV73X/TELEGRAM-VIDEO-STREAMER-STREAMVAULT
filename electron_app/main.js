
const { app, BrowserWindow, ipcMain, screen } = require('electron');
const path = require('path');

const PORT = process.env.SV_PORT || 5000;

// ── Single instance lock ─────────────────────────────────────────────────────
if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }

// ── Smooth rendering flags ───────────────────────────────────────────────────
app.commandLine.appendSwitch('enable-gpu-rasterization');
app.commandLine.appendSwitch('enable-zero-copy');
app.commandLine.appendSwitch('disable-gpu-vsync');
app.commandLine.appendSwitch('disable-frame-rate-limit');

// ── Kill elastic/rubberband overscroll ───────────────────────────────────────
app.commandLine.appendSwitch('disable-features', 'OverscrollHistoryNavigation,TouchpadOverscrollHistoryNavigation,ElasticOverscroll');

let win;

function createWindow() {
  const wa = screen.getPrimaryDisplay().workAreaSize;
  const W = Math.floor(wa.width  * 0.9);
  const H = Math.floor(wa.height * 0.9);

  win = new BrowserWindow({
    width:  W,
    height: H,
    x: Math.floor((wa.width  - W) / 2),
    y: Math.floor((wa.height - H) / 2),
    frame: false,
    transparent: false,
    backgroundColor: '#080808',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // ── Drop frame rate during resize, restore after ─────────────────────────
  let _rt = null;
  win.on('will-resize', () => { win.webContents.setFrameRate(30); });
  win.on('resize', () => {
    clearTimeout(_rt);
    _rt = setTimeout(() => { win.webContents.setFrameRate(60); }, 150);
  });

  win.loadURL(`http://127.0.0.1:${PORT}/`);
  win.webContents.on('did-finish-load', () => {
    win.webContents.insertCSS('html, body, * { overscroll-behavior: none !important; }');
  });
  win.once('ready-to-show', () => { win.show(); win.focus(); });
  win.on('closed', () => { win = null; app.quit(); });
}

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
app.on('second-instance', () => {
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
});

// ── Window control IPC ───────────────────────────────────────────────────────
ipcMain.on('sv-minimize',     () => win && win.minimize());
ipcMain.on('sv-maximize',     () => win && (win.isMaximized() ? win.unmaximize() : win.maximize()));
ipcMain.on('sv-close',        () => win && win.close());
ipcMain.handle('sv-is-maximized', () => win ? win.isMaximized() : false);
