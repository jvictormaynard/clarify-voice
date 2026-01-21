// Minimal declaration to satisfy TypeScript for packages without bundled types.
declare module "node-record-lpcm16" {
  const recorder: any;
  export default recorder;
}

// Electron API exposed via preload script
interface ElectronAPI {
  sendVideoRecordingError: (message: string) => void;
  sendVideoRecordingStarted: (info: { screen: string; audioTracks: string[] }) => void;
  sendVideoFrame: (frameData: string) => void;
  sendVideoFileComplete: (base64data: string) => void;
  setMode: (mode: string) => void;
  setIncludeVideo: (enabled: boolean) => void;
  testVideoCapture: () => void;
  cancelRecording: () => void;
  closeApp: () => void;
  minimizeApp: () => void;
  hideTranscription: () => void;
  onStartVideoRecording: (callback: () => void) => () => void;
  onStopVideoRecording: (callback: () => void) => () => void;
  onUpdateStatus: (callback: (status: string) => void) => () => void;
  onPlaySound: (callback: (data: { frequency: number; duration: number }) => void) => () => void;
  onShowTranscription: (callback: (text: string) => void) => () => void;
  onVideoTestComplete: (callback: (result: { success: boolean } | null) => void) => () => void;
}

interface Window {
  electronAPI: ElectronAPI;
}
