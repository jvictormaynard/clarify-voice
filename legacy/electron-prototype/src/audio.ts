import { exec } from 'child_process';
import { isWindows, isMac } from './config';

// Cached Linux audio backend
let detectedLinuxAudio: string | null = null;

/**
 * Detect the available Linux audio backend (PipeWire, PulseAudio, or ALSA)
 */
export function detectLinuxAudioBackend(): Promise<string> {
  return new Promise((resolve) => {
    if (detectedLinuxAudio) {
      resolve(detectedLinuxAudio);
      return;
    }

    // Try PipeWire first (modern Ubuntu 22.04+)
    exec('pactl info 2>/dev/null | grep -i pipewire', (err, stdout) => {
      if (!err && stdout.includes('PipeWire')) {
        console.log('Detected PipeWire audio backend');
        detectedLinuxAudio = 'pulseaudio'; // PipeWire is compatible with pulseaudio interface
        resolve('pulseaudio');
        return;
      }

      // Try PulseAudio
      exec('pactl info 2>/dev/null', (err2) => {
        if (!err2) {
          console.log('Detected PulseAudio backend');
          detectedLinuxAudio = 'pulseaudio';
          resolve('pulseaudio');
          return;
        }

        // Fallback to ALSA
        console.log('Falling back to ALSA backend');
        detectedLinuxAudio = 'alsa';
        resolve('alsa');
      });
    });
  });
}

/**
 * Get platform-specific audio input arguments for SoX
 */
export async function getSoxAudioInputArgs(): Promise<string[]> {
  if (isWindows) {
    return ['-t', 'waveaudio', '-d']; // Windows audio driver
  } else if (isMac) {
    return ['-t', 'coreaudio', 'default']; // macOS CoreAudio
  } else {
    // Linux - detect available audio backend
    const backend = await detectLinuxAudioBackend();
    if (backend === 'pulseaudio') {
      return ['-t', 'pulseaudio', 'default'];
    } else {
      return ['-t', 'alsa', 'default'];
    }
  }
}
