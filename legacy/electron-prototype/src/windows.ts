import { app, BrowserWindow, screen, Tray, Menu, nativeImage, globalShortcut } from 'electron';
import path from 'path';
import {
  mainWindow, recordingWindow, tray, isRecording,
  setMainWindow, setRecordingWindow, setTray,
  setRecordingStartTime, setRecordingTimerInterval,
  recordingTimerInterval, wasWindowVisibleBeforeRecording,
  setWasWindowVisibleBeforeRecording
} from './state';
import { appRoot } from './config';

// Callback for toggle recording (set by recording module to avoid circular deps)
let toggleRecordingCallback: (() => void) | null = null;
let cancelRecordingCallback: (() => void) | null = null;

export function setToggleRecordingCallback(cb: () => void) {
  toggleRecordingCallback = cb;
}

export function setCancelRecordingCallback(cb: () => void) {
  cancelRecordingCallback = cb;
}

/**
 * Create the main application window
 */
export function createMainWindow() {
  const { width } = screen.getPrimaryDisplay().workAreaSize;

  const window = new BrowserWindow({
    width: 530,
    height: 50,
    minWidth: 300,
    maxWidth: 700,
    minHeight: 50,
    maxHeight: 500,
    x: width - 550,
    y: 20,
    frame: false,
    transparent: false,
    alwaysOnTop: true,
    resizable: true,
    skipTaskbar: true,
    focusable: true,
    hasShadow: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    }
  });

  window.setAlwaysOnTop(true, 'floating');
  window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  const htmlPath = app.isPackaged
    ? path.join(__dirname, '../src/index.html')
    : path.join(appRoot, 'src', 'index.html');

  console.log('Loading HTML from:', htmlPath);
  window.loadFile(htmlPath);

  if (!app.isPackaged) {
    window.webContents.openDevTools({ mode: 'detach' });
  }

  setMainWindow(window);
  return window;
}

/**
 * Create tray icon image
 */
function createTrayIcon(isRed: boolean = false) {
  const iconSize = 16;
  const canvas = Buffer.alloc(iconSize * iconSize * 4);

  const r = isRed ? 239 : 59;
  const g = isRed ? 68 : 130;
  const b = isRed ? 68 : 246;

  for (let y = 0; y < iconSize; y++) {
    for (let x = 0; x < iconSize; x++) {
      const idx = (y * iconSize + x) * 4;
      const cx = iconSize / 2;
      const cy = iconSize / 2;
      const dist = Math.sqrt((x - cx) ** 2 + (y - cy) ** 2);

      if (dist >= 5 && dist <= 7 || dist <= 3) {
        canvas[idx] = r;
        canvas[idx + 1] = g;
        canvas[idx + 2] = b;
        canvas[idx + 3] = 255;
      } else {
        canvas[idx] = 0;
        canvas[idx + 1] = 0;
        canvas[idx + 2] = 0;
        canvas[idx + 3] = 0;
      }
    }
  }

  return nativeImage.createFromBuffer(canvas, { width: iconSize, height: iconSize });
}

/**
 * Update tray icon color based on recording state
 */
export function updateTrayIcon(recording: boolean) {
  if (tray) {
    tray.setImage(createTrayIcon(recording));
    tray.setToolTip(recording ? 'ClarifyVoice - Recording...' : 'ClarifyVoice');
  }
}

/**
 * Create system tray
 */
export function createTray() {
  const newTray = new Tray(createTrayIcon(false));

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show/Hide (Alt+R)',
      click: () => toggleMainWindow()
    },
    {
      label: 'Start/Stop Recording (Alt+L)',
      click: () => toggleRecordingCallback?.()
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => app.quit()
    }
  ]);

  newTray.setToolTip('ClarifyVoice');
  newTray.setContextMenu(contextMenu);

  newTray.on('click', () => toggleMainWindow());

  setTray(newTray);
  return newTray;
}

/**
 * Toggle main window visibility
 */
export function toggleMainWindow() {
  if (mainWindow) {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  }
}

/**
 * Update main window status
 */
export function updateStatus(status: 'ready' | 'recording' | 'processing') {
  if (mainWindow) {
    mainWindow.webContents.send('update-status', status);
  }
}

/**
 * Play sound through renderer
 */
export function playSound(frequency: number, duration: number) {
  if (mainWindow) {
    mainWindow.webContents.send('play-sound', { frequency, duration });
  }
}

/**
 * Show transcription result in main window
 */
export function showTranscriptionResult(text: string) {
  if (mainWindow) {
    const currentBounds = mainWindow.getBounds();
    const lines = text.split('\n').length;
    const newHeight = Math.min(400, Math.max(180, lines * 20 + 120));
    mainWindow.setBounds({
      ...currentBounds,
      height: newHeight,
      width: Math.max(currentBounds.width, 400)
    });
    mainWindow.webContents.send('show-transcription', text);
  }
}

/**
 * Format seconds to MM:SS
 */
function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Create the recording indicator window
 */
export function createRecordingWindow() {
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workAreaSize;

  const window = new BrowserWindow({
    width: 200,
    height: 60,
    x: Math.floor((width - 200) / 2),
    y: Math.floor(height * 0.55),
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    focusable: true,
    hasShadow: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    }
  });

  window.setAlwaysOnTop(true, 'floating');

  const html = `
    <!DOCTYPE html>
    <html>
    <head>
      <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: rgba(15, 23, 42, 0.95);
          border-radius: 12px;
          border: 1px solid rgba(239, 68, 68, 0.5);
          height: 100vh;
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 0 16px;
          gap: 12px;
          -webkit-app-region: drag;
        }
        .dot {
          width: 10px;
          height: 10px;
          background: #ef4444;
          border-radius: 50%;
          box-shadow: 0 0 8px #ef4444;
          animation: pulse 1s infinite;
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
        .text { color: #f1f5f9; font-size: 13px; font-weight: 500; }
        .time {
          color: #ef4444;
          font-size: 13px;
          font-weight: 600;
          font-variant-numeric: tabular-nums;
          min-width: 36px;
        }
        .cancel-btn {
          width: 24px;
          height: 24px;
          border-radius: 6px;
          border: 1px solid rgba(239, 68, 68, 0.5);
          background: rgba(239, 68, 68, 0.2);
          color: #ef4444;
          font-size: 14px;
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          -webkit-app-region: no-drag;
          transition: background 0.15s;
        }
        .cancel-btn:hover { background: rgba(239, 68, 68, 0.4); }
      </style>
    </head>
    <body>
      <div class="dot"></div>
      <span class="text">Recording</span>
      <span class="time" id="timer">0:00</span>
      <button class="cancel-btn" id="cancel" title="Cancel">✕</button>
      <script>
        document.getElementById('cancel').addEventListener('click', () => window.close());
      </script>
    </body>
    </html>
  `;

  window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);

  window.on('closed', () => {
    setRecordingWindow(null);
    if (isRecording) {
      cancelRecordingCallback?.();
    }
  });

  setRecordingWindow(window);
  return window;
}

/**
 * Show the recording indicator
 */
export function showRecordingIndicator() {
  setWasWindowVisibleBeforeRecording(mainWindow ? mainWindow.isVisible() : false);

  if (mainWindow) {
    mainWindow.hide();
  }

  createRecordingWindow();
  setRecordingStartTime(Date.now());

  // Register ESC to cancel recording
  globalShortcut.register('Escape', () => {
    if (isRecording) {
      cancelRecordingCallback?.();
    }
  });

  // Update timer every second
  const interval = setInterval(() => {
    if (recordingWindow && !recordingWindow.isDestroyed()) {
      const elapsed = Math.floor((Date.now() - (globalThis as any).__recordingStartTime) / 1000);
      recordingWindow.webContents.executeJavaScript(
        `document.getElementById('timer').textContent = '${formatTime(elapsed)}';`
      ).catch(() => {});
    }
  }, 1000);

  // Store start time globally for interval access
  (globalThis as any).__recordingStartTime = Date.now();
  setRecordingTimerInterval(interval);
}

/**
 * Hide the recording indicator
 */
export function hideRecordingIndicator() {
  globalShortcut.unregister('Escape');

  if (recordingTimerInterval) {
    clearInterval(recordingTimerInterval);
    setRecordingTimerInterval(null);
  }

  if (recordingWindow && !recordingWindow.isDestroyed()) {
    recordingWindow.close();
    setRecordingWindow(null);
  }

  if (mainWindow && wasWindowVisibleBeforeRecording) {
    mainWindow.show();
    mainWindow.focus();
    mainWindow.setAlwaysOnTop(true, 'floating');
  }
}
