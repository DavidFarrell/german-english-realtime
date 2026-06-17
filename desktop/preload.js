// Minimal, safe bridge surface for the renderer: just the ws port the Python bridge bound to.
// Everything else the renderer does over the WebSocket itself (browser WebSocket API).
'use strict';
const { contextBridge } = require('electron');

const portArg = process.argv.find((a) => a.startsWith('--ddport=')) || '--ddport=0';
const port = parseInt(portArg.split('=')[1], 10) || 0;

contextBridge.exposeInMainWorld('DD', {
  port,
  wsUrl: `ws://127.0.0.1:${port}`,
});
