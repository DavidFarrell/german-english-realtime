// Electron main: spawn the Python bridge, wait for `READY <port>`, then open the window.
// The Python core does ALL the work (audio, Gemini, routing); this process only hosts the window
// and the child. The renderer talks to the bridge directly over ws://127.0.0.1:<port>.
'use strict';
const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// In dev, the project root is one level up from desktop/. In a packaged .app the bundle lives in
// /Applications, so we point at the real project (which holds the venv, gui_bridge, and .env) by
// absolute path - overridable via DD_PROJECT_ROOT. This app is a native launcher over that project.
const PACKAGED_PROJECT_ROOT = '/Users/david/git/ai-sandbox/projects/German-English-realtime';
const PROJECT_ROOT = app.isPackaged
  ? (process.env.DD_PROJECT_ROOT || PACKAGED_PROJECT_ROOT)
  : path.resolve(__dirname, '..');
const VENV_PYTHON = path.join(PROJECT_ROOT, '.venv', 'bin', 'python');
const PYTHON = fs.existsSync(VENV_PYTHON) ? VENV_PYTHON : 'python3';

let pyProc = null;
let win = null;
let lastPort = 0;

function startBridge() {
  return new Promise((resolve, reject) => {
    const proc = spawn(PYTHON, ['-m', 'gui_bridge'], {
      cwd: PROJECT_ROOT,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });
    let buf = '';
    let settled = false;
    const onData = (d) => {
      buf += d.toString();
      const m = buf.match(/READY\s+(\d+)/);
      if (m && !settled) {
        settled = true;
        resolve({ proc, port: parseInt(m[1], 10) });
      }
      // surface bridge logs to the Electron console
      process.stdout.write('[bridge] ' + d.toString());
    };
    proc.stdout.on('data', onData);
    proc.stderr.on('data', (d) => process.stderr.write('[bridge] ' + d.toString()));
    proc.on('exit', (code) => {
      if (!settled) { settled = true; reject(new Error('bridge exited early, code ' + code)); }
    });
    setTimeout(() => { if (!settled) { settled = true; reject(new Error('bridge READY timeout')); } }, 15000);
  });
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1340,
    height: 940,
    minWidth: 1160,
    minHeight: 820,
    backgroundColor: '#2b1f4f',
    titleBarStyle: 'hiddenInset',          // real macOS traffic lights overlay our purple title bar
    trafficLightPosition: { x: 16, y: 16 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: ['--ddport=' + port],
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.on('closed', () => { win = null; });
}

app.whenReady().then(async () => {
  try {
    const { proc, port } = await startBridge();
    pyProc = proc;
    lastPort = port;
    createWindow(port);
  } catch (err) {
    console.error('Failed to start the translation bridge:', err);
    dialog.showErrorBox(
      'DebbieDavidApp could not start',
      'The translation engine (Python bridge) failed to launch.\n\n' +
      'Expected the project at:\n' + PROJECT_ROOT + '\n\n' +
      'Check that its .venv exists and gui_bridge is present.\n\nDetails: ' + (err && err.message));
    app.quit();
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0 && pyProc) createWindow(lastPort);
  });
});

function shutdown() {
  if (pyProc) {
    try { pyProc.kill('SIGTERM'); } catch (_) {}
    pyProc = null;
  }
}
app.on('window-all-closed', () => { shutdown(); if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', shutdown);
process.on('exit', shutdown);
