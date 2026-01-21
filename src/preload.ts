import { contextBridge, ipcRenderer } from 'electron';

// Expose a secure API to the renderer process
contextBridge.exposeInMainWorld('electronAPI', {
  // Send methods
  sendVideoRecordingError: (message: string) => ipcRenderer.send('video-recording-error', message),
  sendVideoRecordingStarted: (info: { screen: string; audioTracks: string[] }) =>
    ipcRenderer.send('video-recording-started', info),
  sendVideoFrame: (frameData: string) => ipcRenderer.send('video-frame', frameData),
  sendVideoFileComplete: (base64data: string) => ipcRenderer.send('video-file-complete', base64data),
  setMode: (mode: string) => ipcRenderer.send('set-mode', mode),
  setIncludeVideo: (enabled: boolean) => ipcRenderer.send('set-include-video', enabled),
  testVideoCapture: () => ipcRenderer.send('test-video-capture'),
  cancelRecording: () => ipcRenderer.send('cancel-recording'),
  closeApp: () => ipcRenderer.send('close-app'),
  minimizeApp: () => ipcRenderer.send('minimize-app'),
  hideTranscription: () => ipcRenderer.send('hide-transcription'),

  // Listener methods
  onStartVideoRecording: (callback: () => void) => {
    ipcRenderer.on('start-video-recording', callback);
    return () => ipcRenderer.removeListener('start-video-recording', callback);
  },
  onStopVideoRecording: (callback: () => void) => {
    ipcRenderer.on('stop-video-recording', callback);
    return () => ipcRenderer.removeListener('stop-video-recording', callback);
  },
  onUpdateStatus: (callback: (status: string) => void) => {
    ipcRenderer.on('update-status', (_event, status) => callback(status));
    return () => ipcRenderer.removeAllListeners('update-status');
  },
  onPlaySound: (callback: (data: { frequency: number; duration: number }) => void) => {
    ipcRenderer.on('play-sound', (_event, data) => callback(data));
    return () => ipcRenderer.removeAllListeners('play-sound');
  },
  onShowTranscription: (callback: (text: string) => void) => {
    ipcRenderer.on('show-transcription', (_event, text) => callback(text));
    return () => ipcRenderer.removeAllListeners('show-transcription');
  },
  onVideoTestComplete: (callback: (result: { success: boolean } | null) => void) => {
    ipcRenderer.on('video-test-complete', (_event, result) => callback(result));
    return () => ipcRenderer.removeAllListeners('video-test-complete');
  }
});
