import { spawn } from 'child_process';
import fs from 'fs';
import {
  mainWindow, isRecording, soxProcess, includeVideo,
  recordedVideoFrames, videoRecordingConfirmed,
  setIsRecording, setSoxProcess, clearVideoFrames,
  setVideoRecordingConfirmed
} from './state';
import { AUDIO_FILE_PATH, VIDEO_FILE_PATH, soxExe, soxDir, isWindows } from './config';
import { getSoxAudioInputArgs } from './audio';
import {
  updateStatus, playSound, showTranscriptionResult,
  showRecordingIndicator, hideRecordingIndicator, updateTrayIcon
} from './windows';
import { processAudioWithGemini, processVideoWithGemini } from './gemini';
import { pasteTextToActiveWindow } from './clipboard';
import { pauseMedia, resumeMedia } from './media';

/**
 * Safely delete a file with retries (handles Windows file locking)
 */
async function safeDeleteFile(filePath: string, maxRetries = 5, delayMs = 300): Promise<void> {
  for (let i = 0; i < maxRetries; i++) {
    try {
      if (fs.existsSync(filePath)) {
        fs.unlinkSync(filePath);
      }
      return;
    } catch (err: any) {
      if (err.code === 'EBUSY' || err.code === 'EPERM') {
        if (i < maxRetries - 1) {
          await new Promise(resolve => setTimeout(resolve, delayMs * (i + 1)));
        }
      } else {
        throw err;
      }
    }
  }
  console.warn(`Could not delete file after ${maxRetries} retries:`, filePath);
}

/**
 * Clean up temporary recording files
 */
async function cleanupTempFiles(): Promise<void> {
  try {
    await safeDeleteFile(AUDIO_FILE_PATH);
    await safeDeleteFile(VIDEO_FILE_PATH);
  } catch (err) {
    console.warn('Error during temp file cleanup:', err);
  }
}

/**
 * Toggle recording state
 */
export async function toggleRecording() {
  if (isRecording) {
    await stopRecording();
  } else {
    await startRecording();
  }
}

/**
 * Start recording audio (and optionally video)
 */
export async function startRecording() {
  console.log('Starting recording...');
  console.log('Include video:', includeVideo);

  // Pause any media playing in the background
  await pauseMedia();

  setIsRecording(true);
  clearVideoFrames();
  setVideoRecordingConfirmed(false);
  updateStatus('recording');
  playSound(800, 100);

  // Start video recording from renderer (before hiding main window)
  if (includeVideo && mainWindow) {
    console.log('Starting video recording from renderer');
    mainWindow.webContents.send('start-video-recording');
  }

  // Show the centered recording indicator
  showRecordingIndicator();

  // Update tray icon to red
  updateTrayIcon(true);

  // Start sox audio recording
  try {
    const audioInputArgs = await getSoxAudioInputArgs();
    const soxArgs = [
      ...audioInputArgs,
      '-r', '16000',
      '-c', '1',
      '-b', '16',
      '-e', 'signed-integer',
      AUDIO_FILE_PATH
    ];

    console.log('Starting sox with args:', soxArgs.join(' '));

    const spawnOptions: any = { windowsHide: true };
    if (isWindows && soxDir) {
      spawnOptions.cwd = soxDir;
    }

    const proc = spawn(soxExe, soxArgs, spawnOptions);
    setSoxProcess(proc);

    proc.stderr?.on('data', (data) => {
      console.log('sox stderr:', data.toString());
    });

    proc.on('error', (err) => {
      console.error('sox error:', err);
      setIsRecording(false);
      setSoxProcess(null);
      hideRecordingIndicator();
      updateStatus('ready');
    });

    proc.on('close', (code) => {
      console.log('sox process exited with code:', code);
      if (code !== 0 && code !== null && isRecording) {
        console.error('sox exited with error code:', code);
        setIsRecording(false);
        setSoxProcess(null);
        hideRecordingIndicator();
        updateStatus('ready');
      }
    });
  } catch (error) {
    console.error('Failed to start recording:', error);
    setIsRecording(false);
    setSoxProcess(null);
    hideRecordingIndicator();
    updateStatus('ready');
  }
}

/**
 * Cancel recording without processing
 */
export async function cancelRecording() {
  console.log('Cancelling recording...');
  setIsRecording(false);

  // Resume any media that was paused
  await resumeMedia();

  updateTrayIcon(false);
  hideRecordingIndicator();

  // Stop video recording
  if (includeVideo && mainWindow) {
    mainWindow.webContents.send('stop-video-recording');
  }
  clearVideoFrames();

  // Kill sox and wait for exit
  if (soxProcess) {
    const proc = soxProcess;
    setSoxProcess(null);

    await new Promise<void>((resolve) => {
      proc.on('close', () => resolve());
      proc.kill('SIGINT');
      setTimeout(resolve, 1000);
    });
  }

  // Wait for file handles to release, then delete temp files
  await new Promise(resolve => setTimeout(resolve, 200));
  await cleanupTempFiles();

  updateStatus('ready');
}

/**
 * Stop recording and process with Gemini
 */
export async function stopRecording() {
  console.log('Stopping recording...');
  setIsRecording(false);

  updateTrayIcon(false);
  hideRecordingIndicator();
  updateStatus('processing');

  // Stop video recording
  if (includeVideo && mainWindow) {
    mainWindow.webContents.send('stop-video-recording');
  }

  // Stop sox
  if (soxProcess) {
    const proc = soxProcess;
    setSoxProcess(null);

    await new Promise<void>((resolve) => {
      proc.on('close', () => {
        console.log('Sox process closed');
        resolve();
      });
      proc.kill('SIGINT');
      setTimeout(resolve, 2000);
    });
  }

  // Resume any media that was paused (immediately after recording stops)
  await resumeMedia();

  // Wait for video file if needed
  if (includeVideo) {
    await new Promise(resolve => setTimeout(resolve, 1500));
    if (!videoRecordingConfirmed) {
      console.error('Video recording never started; skipping video processing');
    }
  }

  // Wait for files to be fully written
  await new Promise(resolve => setTimeout(resolve, 500));

  try {
    const hasVideo = includeVideo && fs.existsSync(VIDEO_FILE_PATH) && fs.statSync(VIDEO_FILE_PATH).size > 1000;
    const hasAudio = fs.existsSync(AUDIO_FILE_PATH) && fs.statSync(AUDIO_FILE_PATH).size > 1000;

    if (!hasAudio && !hasVideo) {
      console.error('No valid recording files exist');
      updateStatus('ready');
      playSound(400, 200);
      return;
    }

    console.log('Calling Gemini API...');
    console.log('Has Video:', hasVideo, 'Has Audio:', hasAudio);

    let refinedText = '';
    if (hasVideo && hasAudio) {
      console.log('Processing Video + Audio with Gemini...');
      refinedText = await processVideoWithGemini(VIDEO_FILE_PATH, AUDIO_FILE_PATH, recordedVideoFrames);
    } else if (hasAudio) {
      console.log('Processing Audio-only with Gemini...');
      refinedText = await processAudioWithGemini(AUDIO_FILE_PATH, recordedVideoFrames);
    } else if (hasVideo) {
      console.log('Processing Video-only with Gemini...');
      refinedText = await processVideoWithGemini(VIDEO_FILE_PATH, '', recordedVideoFrames);
    }

    console.log('Gemini response:', refinedText ? refinedText.substring(0, 100) + '...' : '(empty)');

    if (refinedText) {
      playSound(1000, 100);
      showTranscriptionResult(refinedText);
      await pasteTextToActiveWindow(refinedText);
    }
  } catch (error) {
    console.error('Error processing:', error);
    playSound(400, 200);
  } finally {
    clearVideoFrames();
    setVideoRecordingConfirmed(false);

    await cleanupTempFiles();

    updateStatus('ready');
  }
}
