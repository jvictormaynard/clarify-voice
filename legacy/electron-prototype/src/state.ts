import { BrowserWindow, Tray } from 'electron';
import { ChildProcess } from 'child_process';
import { GoogleGenAI } from '@google/genai';
import { API_KEY } from './config';

// Window references
export let mainWindow: BrowserWindow | null = null;
export let recordingWindow: BrowserWindow | null = null;
export let tray: Tray | null = null;

// Recording state
export let isRecording = false;
export let recordingStartTime: number = 0;
export let recordingTimerInterval: NodeJS.Timeout | null = null;
export let wasWindowVisibleBeforeRecording: boolean = true;
export let soxProcess: ChildProcess | null = null;

// Mode and settings
export let currentMode: 'prompt' | 'transcription' = 'prompt';
export let includeVideo = false;

// Video recording state
export let recordedVideoFrames: string[] = [];
export let videoRecordingConfirmed = false;

// Gemini AI instance
export let ai: GoogleGenAI | null = API_KEY ? new GoogleGenAI({ apiKey: API_KEY }) : null;

// Setters for mutable state
export function setMainWindow(window: BrowserWindow | null) {
  mainWindow = window;
}

export function setRecordingWindow(window: BrowserWindow | null) {
  recordingWindow = window;
}

export function setTray(t: Tray | null) {
  tray = t;
}

export function setIsRecording(value: boolean) {
  isRecording = value;
}

export function setRecordingStartTime(value: number) {
  recordingStartTime = value;
}

export function setRecordingTimerInterval(interval: NodeJS.Timeout | null) {
  recordingTimerInterval = interval;
}

export function setWasWindowVisibleBeforeRecording(value: boolean) {
  wasWindowVisibleBeforeRecording = value;
}

export function setSoxProcess(process: ChildProcess | null) {
  soxProcess = process;
}

export function setCurrentMode(mode: 'prompt' | 'transcription') {
  currentMode = mode;
}

export function setIncludeVideo(value: boolean) {
  includeVideo = value;
}

export function setRecordedVideoFrames(frames: string[]) {
  recordedVideoFrames = frames;
}

export function addVideoFrame(frame: string) {
  recordedVideoFrames.push(frame);
}

export function clearVideoFrames() {
  recordedVideoFrames = [];
}

export function setVideoRecordingConfirmed(value: boolean) {
  videoRecordingConfirmed = value;
}
