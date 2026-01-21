import { ipcMain, desktopCapturer } from 'electron';
import fs from 'fs';
import {
  mainWindow, isRecording,
  setCurrentMode, setIncludeVideo,
  setVideoRecordingConfirmed, addVideoFrame
} from './state';
import { VIDEO_FILE_PATH } from './config';
import { playSound, updateStatus } from './windows';

// Callback for cancel recording (set by recording module)
let cancelRecordingCallback: (() => void) | null = null;

export function setCancelRecordingCallback(cb: () => void) {
  cancelRecordingCallback = cb;
}

/**
 * Register all IPC handlers
 */
export function registerIpcHandlers() {
  // Cancel recording from renderer
  ipcMain.on('cancel-recording', () => {
    if (isRecording) {
      cancelRecordingCallback?.();
    }
  });

  // Set mode (prompt/transcription)
  ipcMain.on('set-mode', (_event, mode) => {
    console.log(`Switching mode to: ${mode}`);
    setCurrentMode(mode);
  });

  // Include video setting
  ipcMain.on('set-include-video', (_event, enabled) => {
    console.log(`Include video: ${enabled}`);
    setIncludeVideo(enabled);
  });

  // Video recording started confirmation
  ipcMain.on('video-recording-started', (_event, info) => {
    setVideoRecordingConfirmed(true);
    console.log('Renderer reports video recording started:', info);
  });

  // Video recording error
  ipcMain.on('video-recording-error', (_event, message) => {
    if (message) {
      console.error('Video recording error (renderer):', message);
      playSound(400, 200);
      updateStatus('ready');
    }
  });

  // Receive video frames
  ipcMain.on('video-frame', (_event, frameData) => {
    addVideoFrame(frameData);
  });

  // Receive complete video file
  ipcMain.on('video-file-complete', (_event, videoData) => {
    console.log('Received video file, size:', videoData.length);
    try {
      const buffer = Buffer.from(videoData, 'base64');
      fs.writeFileSync(VIDEO_FILE_PATH, buffer);
      setVideoRecordingConfirmed(true);
      console.log('Video file saved:', VIDEO_FILE_PATH);
    } catch (error) {
      console.error('Failed to save video file:', error);
    }
  });

  // Close app
  ipcMain.on('close-app', () => {
    const { app } = require('electron');
    app.quit();
  });

  // Minimize app
  ipcMain.on('minimize-app', () => {
    if (mainWindow) {
      mainWindow.hide();
    }
  });

  // Hide transcription (restore window size)
  ipcMain.on('hide-transcription', () => {
    if (mainWindow) {
      const currentBounds = mainWindow.getBounds();
      mainWindow.setBounds({
        ...currentBounds,
        height: 50,
        width: 380
      });
    }
  });

  // Test video capture
  ipcMain.on('test-video-capture', async () => {
    console.log('Testing video capture...');
    try {
      const sources = await desktopCapturer.getSources({
        types: ['screen', 'window'],
        thumbnailSize: { width: 150, height: 150 }
      });

      const success = sources.length > 0;
      console.log('Video test result:', success ? 'success' : 'no sources found');

      if (mainWindow) {
        mainWindow.webContents.send('video-test-complete', { success });
      }
    } catch (error) {
      console.error('Video test failed:', error);
      if (mainWindow) {
        mainWindow.webContents.send('video-test-complete', { success: false });
      }
    }
  });
}
