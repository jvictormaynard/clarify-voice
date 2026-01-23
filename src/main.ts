import { app, globalShortcut, session, desktopCapturer, ipcMain } from 'electron';
import { mainWindow, currentMode, includeVideo, setIncludeVideo } from './state';
import { exec } from 'child_process';

// Enable legacy screen-capture support (must be before app ready)
app.commandLine.appendSwitch('enable-usermedia-screen-capturing');

// Import configuration (initializes env, paths, etc.)
import { isWindows, isLinux, isMac, soxExe } from './config';

// Import modules
import { createMainWindow, createTray, toggleMainWindow } from './windows';
import { setToggleRecordingCallback, setCancelRecordingCallback } from './windows';
import { registerIpcHandlers, setCancelRecordingCallback as setIpcCancelCallback } from './ipc';
import { toggleRecording, cancelRecording } from './recording';

// Log desktopCapturer availability
console.log('Has desktopCapturer in main:', !!desktopCapturer);

/**
 * Initialize the application
 */
app.whenReady().then(() => {
  // Set up callbacks for cross-module communication
  setToggleRecordingCallback(toggleRecording);
  setCancelRecordingCallback(cancelRecording);
  setIpcCancelCallback(cancelRecording);

  // Set up screen capture permissions
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    const allowedPermissions = ['media', 'mediaKeySystem', 'display-capture'];
    callback(allowedPermissions.includes(permission));
  });

  session.defaultSession.setPermissionCheckHandler((_webContents, permission) => {
    const allowedPermissions = ['media', 'mediaKeySystem', 'display-capture'];
    return allowedPermissions.includes(permission);
  });

  // Route getDisplayMedia through desktopCapturer
  session.defaultSession.setDisplayMediaRequestHandler(async (_request, callback) => {
    try {
      const sources = await desktopCapturer.getSources({
        types: ['screen', 'window'],
        thumbnailSize: { width: 0, height: 0 }
      });

      if (!sources.length) {
        console.error('No display sources available for screen capture.');
        callback({} as any);
        return;
      }

      const primarySource = sources[0];
      console.log('Selected source for screen capture:', primarySource.name, primarySource.id);

      callback({
        video: primarySource,
        audio: 'loopback'
      });
    } catch (error) {
      console.error('DisplayMedia handler error:', error);
      callback({} as any);
    }
  });

  // Create window and tray
  createMainWindow();
  createTray();

  // Register IPC handlers
  registerIpcHandlers();

  // Register global hotkeys
  globalShortcut.register('Alt+L', toggleRecording);
  globalShortcut.register('Alt+R', toggleMainWindow);

  // Mode selection shortcuts
  globalShortcut.register('Alt+1', () => {
    if (mainWindow) {
      mainWindow.webContents.send('set-mode-shortcut', 'prompt');
    }
  });

  globalShortcut.register('Alt+2', () => {
    if (mainWindow) {
      mainWindow.webContents.send('set-mode-shortcut', 'transcription');
    }
  });

  // Video toggle shortcut (only works in prompt mode)
  globalShortcut.register('Alt+V', () => {
    if (mainWindow && currentMode === 'prompt') {
      const newValue = !includeVideo;
      setIncludeVideo(newValue);
      mainWindow.webContents.send('toggle-video-shortcut', newValue);
    }
  });

  // Check SoX availability
  const soxCheckCmd = isWindows ? `"${soxExe}" --version` : 'sox --version';
  exec(soxCheckCmd, (err) => {
    if (err) {
      console.error('SoX not found! Please install SoX:');
      if (isLinux) {
        console.error('  Ubuntu/Debian: sudo apt install sox libsox-fmt-all');
      } else if (isMac) {
        console.error('  macOS: brew install sox');
      } else {
        console.error('  SoX should be bundled with this application');
      }
    } else {
      console.log('SoX is available');
    }
  });

  // Check xdotool on Linux
  if (isLinux) {
    exec('which xdotool', (err) => {
      if (err) {
        console.error('xdotool not found! Paste functionality will not work.');
        console.error('  Install with: sudo apt install xdotool');
      } else {
        console.log('xdotool is available');
      }
    });
  }
});

// Clean up on quit
app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
