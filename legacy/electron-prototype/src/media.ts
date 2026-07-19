import { exec } from 'child_process';
import { app } from 'electron';
import fs from 'fs';
import path from 'path';
import os from 'os';

let mediaWasPaused = false;

/**
 * Pause media playback on the system (only if currently playing)
 */
export async function pauseMedia(): Promise<void> {
  const platform = os.platform();
  console.log('Checking media playback...');

  try {
    if (platform === 'win32') {
      const wasPlaying = await isWindowsMediaPlaying();
      if (wasPlaying) {
        console.log('Media is playing, pausing...');
        await sendWindowsMediaKey();
        mediaWasPaused = true;
      } else {
        console.log('No media playing, skipping pause');
        mediaWasPaused = false;
      }
    } else if (platform === 'darwin') {
      const wasPlaying = await isMacMediaPlaying();
      if (wasPlaying) {
        console.log('Media is playing, pausing...');
        await sendMacMediaKey();
        mediaWasPaused = true;
      } else {
        console.log('No media playing, skipping pause');
        mediaWasPaused = false;
      }
    } else if (platform === 'linux') {
      const wasPlaying = await isLinuxMediaPlaying();
      if (wasPlaying) {
        console.log('Media is playing, pausing...');
        await sendLinuxMediaCommand('pause');
        mediaWasPaused = true;
      } else {
        console.log('No media playing, skipping pause');
        mediaWasPaused = false;
      }
    }
  } catch (err) {
    console.error('Failed to pause media:', err);
    mediaWasPaused = false;
  }
}

/**
 * Resume media playback on the system (only if we paused it)
 */
export async function resumeMedia(): Promise<void> {
  if (!mediaWasPaused) {
    console.log('Media was not paused by us, skipping resume');
    return;
  }

  const platform = os.platform();
  console.log('Resuming media playback...');

  try {
    if (platform === 'win32') {
      await sendWindowsMediaKey();
    } else if (platform === 'darwin') {
      await sendMacMediaKey();
    } else if (platform === 'linux') {
      await sendLinuxMediaCommand('play');
    }
  } catch (err) {
    console.error('Failed to resume media:', err);
  } finally {
    mediaWasPaused = false;
  }
}

/**
 * Check if media is currently playing on Windows
 * Uses the audio meter peak level to detect if audio is being output
 */
async function isWindowsMediaPlaying(): Promise<boolean> {
  const psScript = `
Add-Type @"
using System;
using System.Runtime.InteropServices;

[Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioMeterInformation {
    int GetPeakValue(out float pfPeak);
}

[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
}

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    int NotImpl1();
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice);
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumerator { }

public class AudioChecker {
    public static bool IsAudioPlaying() {
        try {
            var enumerator = new MMDeviceEnumerator() as IMMDeviceEnumerator;
            IMMDevice device;
            // 0 = eRender (speakers), 1 = eMultimedia
            enumerator.GetDefaultAudioEndpoint(0, 1, out device);

            Guid iid = typeof(IAudioMeterInformation).GUID;
            object o;
            device.Activate(ref iid, 0, IntPtr.Zero, out o);
            var meter = o as IAudioMeterInformation;

            float peak;
            meter.GetPeakValue(out peak);

            // If peak > 0.001, audio is playing
            return peak > 0.001f;
        } catch {
            return false;
        }
    }
}
"@

# Check multiple times over 200ms to catch audio
$playing = $false
for ($i = 0; $i -lt 4; $i++) {
    if ([AudioChecker]::IsAudioPlaying()) {
        $playing = $true
        break
    }
    Start-Sleep -Milliseconds 50
}
if ($playing) { Write-Output "Playing" } else { Write-Output "NotPlaying" }
`;

  const psPath = path.join(app.getPath('userData'), 'check_media.ps1');
  fs.writeFileSync(psPath, psScript);

  return new Promise((resolve) => {
    exec(`powershell -ExecutionPolicy Bypass -File "${psPath}"`, { timeout: 5000 }, (err, stdout, stderr) => {
      if (err) {
        console.log('Could not check media status:', stderr || err.message);
        // Fall back to assuming media is playing if we can't check
        resolve(true);
      } else {
        const result = stdout.trim();
        console.log('Media status:', result);
        resolve(result === 'Playing');
      }
    });
  });
}

/**
 * Check if media is currently playing on macOS
 */
async function isMacMediaPlaying(): Promise<boolean> {
  // Try to check common media players
  const script = `
    set isPlaying to false
    try
      tell application "System Events"
        set processList to name of every process
        if processList contains "Spotify" then
          tell application "Spotify"
            if player state is playing then set isPlaying to true
          end tell
        end if
        if processList contains "Music" then
          tell application "Music"
            if player state is playing then set isPlaying to true
          end tell
        end if
      end tell
    end try
    if isPlaying then
      return "Playing"
    else
      return "NotPlaying"
    end if
  `;

  return new Promise((resolve) => {
    exec(`osascript -e '${script.replace(/'/g, "'\\''")}'`, (err, stdout) => {
      if (err) {
        console.log('Could not check media status, assuming not playing');
        resolve(false);
      } else {
        resolve(stdout.trim() === 'Playing');
      }
    });
  });
}

/**
 * Check if media is currently playing on Linux using playerctl
 */
async function isLinuxMediaPlaying(): Promise<boolean> {
  return new Promise((resolve) => {
    exec('playerctl status', (err, stdout) => {
      if (err) {
        // No player running
        resolve(false);
      } else {
        resolve(stdout.trim() === 'Playing');
      }
    });
  });
}

/**
 * Send media play/pause key on Windows using PowerShell script file
 */
async function sendWindowsMediaKey(): Promise<void> {
  const psScript = `
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class MediaKey {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    public static void SendMediaPlayPause() {
        keybd_event(0xB3, 0, 0, UIntPtr.Zero);
        keybd_event(0xB3, 0, 2, UIntPtr.Zero);
    }
}
"@
[MediaKey]::SendMediaPlayPause()
`;

  const psPath = path.join(app.getPath('userData'), 'media_key.ps1');
  fs.writeFileSync(psPath, psScript);

  return new Promise((resolve, reject) => {
    exec(`powershell -ExecutionPolicy Bypass -File "${psPath}"`, (err) => {
      if (err) {
        reject(err);
      } else {
        resolve();
      }
    });
  });
}

/**
 * Send media key on macOS using osascript
 */
async function sendMacMediaKey(): Promise<void> {
  return new Promise((resolve, reject) => {
    exec(`osascript -e 'tell application "System Events" to key code 100'`, (err) => {
      if (err) {
        reject(err);
      } else {
        resolve();
      }
    });
  });
}

/**
 * Send media command on Linux using playerctl
 */
async function sendLinuxMediaCommand(action: 'pause' | 'play'): Promise<void> {
  const command = action === 'pause' ? 'playerctl pause' : 'playerctl play';

  return new Promise((resolve) => {
    exec(command, (err) => {
      if (err) {
        console.log(`playerctl ${action} failed (no player running?):`, err.message);
      }
      resolve();
    });
  });
}
