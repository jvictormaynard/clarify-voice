import { app, BrowserWindow, globalShortcut, ipcMain, screen, clipboard, desktopCapturer, session } from 'electron';
import path from 'path';
import { GoogleGenAI } from "@google/genai";
import fs from "fs";
import dotenv from "dotenv";
import { exec, spawn, ChildProcess } from "child_process";
import os from "os";

// Enable legacy screen-capture support for getUserMedia/desktop capture
// (must be set before the app is ready)
app.commandLine.appendSwitch('enable-usermedia-screen-capturing');

// Get the app root directory
const appRoot = app.isPackaged 
  ? path.dirname(app.getPath('exe'))
  : path.join(__dirname, '..');

// Load environment variables - in packaged app, .env is in resources folder
const envPath = app.isPackaged
  ? path.join(process.resourcesPath, '.env')
  : path.join(appRoot, '.env');
console.log("Looking for .env at:", envPath);
console.log(".env exists:", fs.existsSync(envPath));
dotenv.config({ path: envPath });

// --- Configuration ---
const API_KEY = process.env.API_KEY;
const AUDIO_FILE_PATH = path.join(app.getPath('userData'), "temp_recording.wav");

// Set up SoX path - platform specific
const isWindows = process.platform === 'win32';
const isLinux = process.platform === 'linux';
const isMac = process.platform === 'darwin';

// On Linux/macOS, SoX should be installed via package manager (apt, brew)
// On Windows, we bundle SoX in extra/sox-14.4.2
const soxDir = isWindows
  ? (app.isPackaged
      ? path.join(process.resourcesPath, 'extra', 'sox-14.4.2')
      : path.join(appRoot, 'extra', 'sox-14.4.2'))
  : ''; // Linux/macOS use system sox

// Add sox directory to PATH only on Windows
if (isWindows && soxDir) {
  process.env.PATH = soxDir + path.delimiter + (process.env.PATH || '');
}

console.log("Platform:", process.platform);
console.log("SoX Directory:", soxDir || "(system)");
console.log("API_KEY loaded:", API_KEY ? "Yes" : "No");
console.log("Is Packaged:", app.isPackaged);


// --- State ---
let mainWindow: BrowserWindow | null = null;
let isRecording = false;
let soxProcess: ChildProcess | null = null;
let ai: GoogleGenAI | null = null;
let currentMode = 'prompt'; // 'prompt' or 'transcription'
let includeVideo = false;
let recordedVideoFrames: string[] = []; // Base64 encoded frames
const VIDEO_FILE_PATH = path.join(app.getPath('userData'), "temp_recording.webm");
let videoRecordingConfirmed = false;

// Full path to sox executable - platform specific
const soxExe = isWindows ? path.join(soxDir, 'sox.exe') : 'sox';
console.log("SoX executable:", soxExe);
console.log("Has desktopCapturer in main:", !!desktopCapturer);

// Detect available Linux audio backend
let detectedLinuxAudio: string | null = null;

function detectLinuxAudioBackend(): Promise<string> {
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

// Get platform-specific audio input type for SoX
async function getSoxAudioInputArgs(): Promise<string[]> {
  if (isWindows) {
    return ['-t', 'waveaudio', '-d'];  // Windows audio driver
  } else if (isMac) {
    return ['-t', 'coreaudio', 'default'];  // macOS CoreAudio
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

if (API_KEY) {
  ai = new GoogleGenAI({ apiKey: API_KEY });
}

const SYSTEM_INSTRUCTION = `
You are an expert editor and transcriber. 
Your task is to take the provided audio input, transcribe it, and then rewrite it to be more organized, clear, and comprehensible.
Do not strictly transcribe filler words, stutters, or confused speech unless it adds meaning.
Fix grammar and sentence structure.
The tone should be professional yet natural.

CRITICAL: Write the output in the first person ("I") as if you are the one speaking. 
NEVER use phrases like "The user says" or "The speaker indicates".
Return ONLY the rewritten text. Do not include introductory phrases like "Here is the rewritten text".
`;

const VIDEO_SYSTEM_INSTRUCTION = `
You are an expert technical assistant and editor.
Your task is to analyze the provided screen recording and the accompanying audio to understand a technical issue or request.
The user is speaking while showing their screen. Use the visual context (code, UI bugs, terminal output, etc.) to supplement the spoken words.
If the user refers to something on the screen (e.g., "this error here", "this part of the code"), use the video to identify exactly what they mean.
Rewrite the user's speech into a clear, organized, and professional prompt that can be used with other AI coding assistants.
The final output should be a well-structured request that describes the problem and the desired solution, incorporating technical details visible on the screen.

CRITICAL: Write the prompt in the first person ("I") as if you are the one reporting the issue or making the request. 
NEVER use phrases like "The user says", "The user states", or "The user indicates". 
The output must be a ready-to-use prompt that I can paste directly into another AI.
Return ONLY the rewritten prompt. Do not include introductory phrases.
`;

const TRANSCRIPTION_INSTRUCTION = `
You are an expert transcriber.
Your task is to transcribe the provided audio input directly.
Clean up filler words (um, uh, like) and correct basic grammar, but keep the original meaning and structure intact.
Transcribe in the exact language spoken in the audio.
Return ONLY the transcribed text. Do not include introductory phrases.
`;

function createWindow() {
  const { width } = screen.getPrimaryDisplay().workAreaSize;

  mainWindow = new BrowserWindow({
    width: 280,
    height: 140,
    minWidth: 280,
    maxWidth: 600,
    minHeight: 60,
    maxHeight: 500,
    x: width - 300, // Position top-right
    y: 20,
    frame: false, // No window frame
    transparent: true, // Transparent background
    alwaysOnTop: true, // Float on top
    resizable: true,
    skipTaskbar: true, // Don't show in taskbar
    focusable: false, // Don't steal focus
    hasShadow: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    }
  });

  // Keep window always on top at the highest level
  mainWindow.setAlwaysOnTop(true, 'screen-saver');
  mainWindow.setVisibleOnAllWorkspaces(true);

  const htmlPath = app.isPackaged
    ? path.join(__dirname, '../src/index.html')
    : path.join(appRoot, 'src', 'index.html');
  console.log("Loading HTML from:", htmlPath);
  mainWindow.loadFile(htmlPath);
  
  // Only open DevTools in development mode
  if (!app.isPackaged) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
}

app.whenReady().then(() => {
  // Handle screen capture permissions globally on the session
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    const allowedPermissions = ['media', 'mediaKeySystem', 'display-capture'];
    if (allowedPermissions.includes(permission)) {
      callback(true);
    } else {
      callback(false);
    }
  });
  
  session.defaultSession.setPermissionCheckHandler((webContents, permission, requestingOrigin, details) => {
    const allowedPermissions = ['media', 'mediaKeySystem', 'display-capture'];
    if (allowedPermissions.includes(permission)) {
      return true;
    }
    return false;
  });

  // Route getDisplayMedia requests through desktopCapturer so screen capture
  // works even though desktopCapturer is not exposed in the renderer.
  const ses = session.defaultSession;
  ses.setDisplayMediaRequestHandler(async (_request, callback) => {
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

      // Find the primary screen or just take the first one
      const primarySource = sources[0];
      console.log('Selected source for screen capture:', primarySource.name, primarySource.id);
      
      callback({
        video: primarySource,
        audio: 'loopback' // Optional: capture system audio too
      });
    } catch (error) {
      console.error('DisplayMedia handler error:', error);
      callback({} as any);
    }
  });

  createWindow();

  // Register Global Hotkey
  const ret = globalShortcut.register('Alt+L', () => {
    toggleRecording();
  });

  if (!ret) {
    console.log('Registration failed');
  }

  // Check if SoX is available
  const soxCheckCmd = isWindows ? `"${soxExe}" --version` : 'sox --version';
  exec(soxCheckCmd, (err) => {
      if (err) {
          console.error("SoX not found! Please install SoX:");
          if (isLinux) {
              console.error("  Ubuntu/Debian: sudo apt install sox libsox-fmt-all");
          } else if (isMac) {
              console.error("  macOS: brew install sox");
          } else {
              console.error("  SoX should be bundled with this application");
          }
      } else {
          console.log("SoX is available");
      }
  });

  // Check for xdotool on Linux (needed for paste functionality)
  if (isLinux) {
    exec('which xdotool', (err) => {
      if (err) {
        console.error("xdotool not found! Paste functionality will not work.");
        console.error("  Install with: sudo apt install xdotool");
      } else {
        console.log("xdotool is available");
      }
    });
  }
});

// IPC handler for cancel recording from renderer
ipcMain.on('cancel-recording', () => {
  if (isRecording) {
    cancelRecording();
  }
});

// IPC handler for setting mode
ipcMain.on('set-mode', (event, mode) => {
  console.log(`Switching mode to: ${mode}`);
  currentMode = mode;
});

// IPC handler for include video setting
ipcMain.on('set-include-video', (event, enabled) => {
  console.log(`Include video: ${enabled}`);
  includeVideo = enabled;
});

// Renderer confirmed video recording started
ipcMain.on('video-recording-started', (event, info) => {
  videoRecordingConfirmed = true;
  console.log('Renderer reports video recording started:', info);
});

// Renderer hit a video-recording error
ipcMain.on('video-recording-error', (event, message) => {
  if (message) {
    console.error('Video recording error (renderer):', message);
    playSound(400, 200);
    updateStatus('ready');
  }
});

// IPC handler for receiving video frames from renderer
ipcMain.on('video-frame', (event, frameData) => {
  recordedVideoFrames.push(frameData);
});

// IPC handler for receiving complete video file
ipcMain.on('video-file-complete', (event, videoData) => {
  console.log('Received video file, size:', videoData.length);
  try {
    const buffer = Buffer.from(videoData, 'base64');
    fs.writeFileSync(VIDEO_FILE_PATH, buffer);
    videoRecordingConfirmed = true;
    console.log('Video file saved:', VIDEO_FILE_PATH);
  } catch (error) {
    console.error('Failed to save video file:', error);
  }
});

// IPC handler for closing the app
ipcMain.on('close-app', () => {
  app.quit();
});

// IPC handler for hiding transcription (restore window size)
ipcMain.on('hide-transcription', () => {
  if (mainWindow) {
    const currentBounds = mainWindow.getBounds();
    mainWindow.setBounds({
      ...currentBounds,
      height: 60,
      width: 280
    });
  }
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// --- Logic ---

async function toggleRecording() {
  if (isRecording) {
    await stopRecording();
  } else {
    await startRecording();
  }
}

function updateStatus(status: 'ready' | 'recording' | 'processing') {
  if (mainWindow) {
    mainWindow.webContents.send('update-status', status);
  }
}

function playSound(frequency: number, duration: number) {
  if (mainWindow) {
    mainWindow.webContents.send('play-sound', { frequency, duration });
  }
}

function showTranscriptionResult(text: string) {
  if (mainWindow) {
    // Resize window to show transcription
    const currentBounds = mainWindow.getBounds();
    const newHeight = Math.min(400, Math.max(150, text.length * 0.5 + 100));
    mainWindow.setBounds({
      ...currentBounds,
      height: newHeight,
      width: Math.max(currentBounds.width, 400)
    });
    mainWindow.webContents.send('show-transcription', text);
    console.log('Showing transcription in component:', text.substring(0, 50) + '...');
  }
}

async function startRecording() {
  console.log("Starting recording...");
  console.log("Include video:", includeVideo);
  isRecording = true;
  recordedVideoFrames = []; // Reset video frames
  videoRecordingConfirmed = false;
  updateStatus('recording');
  playSound(800, 100); // Play start sound

  // If video is enabled, also start video recording from renderer
  if (includeVideo && mainWindow) {
    console.log('Starting video recording from renderer');
    mainWindow.webContents.send('start-video-recording');
  }

  // Always use sox for audio recording to ensure high quality
  try {
    // Use sox with platform-specific audio input
    // Output: 16-bit signed, 16kHz, mono WAV
    const audioInputArgs = await getSoxAudioInputArgs();
    const soxArgs = [
      ...audioInputArgs,          // Platform-specific input device
      '-r', '16000',              // Sample rate 16kHz
      '-c', '1',                  // Mono
      '-b', '16',                 // 16-bit
      '-e', 'signed-integer',     // Signed integer encoding
      AUDIO_FILE_PATH             // Output file
    ];

    console.log("Starting sox with args:", soxArgs.join(' '));

    const spawnOptions: any = {
      windowsHide: true,
    };

    // On Windows, set cwd to sox folder for DLL access
    if (isWindows && soxDir) {
      spawnOptions.cwd = soxDir;
    }

    soxProcess = spawn(soxExe, soxArgs, spawnOptions);

    soxProcess.stderr?.on('data', (data) => {
      console.log('sox stderr:', data.toString());
    });

    soxProcess.on('error', (err) => {
      console.error('sox error:', err);
      isRecording = false;
      soxProcess = null;
      updateStatus('ready');
    });

    soxProcess.on('close', (code) => {
      console.log('sox process exited with code:', code);
      // Only show error if we were actively recording and it failed unexpectedly
      if (code !== 0 && code !== null && isRecording) {
        console.error('sox exited with error code:', code);
        isRecording = false;
        soxProcess = null;
        updateStatus('ready');
      }
    });

  } catch (error) {
    console.error("Failed to start recording:", error);
    isRecording = false;
    soxProcess = null;
    updateStatus('ready');
  }
}

// Cancel recording without transcribing
function cancelRecording() {
  console.log("Cancelling recording...");
  isRecording = false;
  
  // Stop video recording if active
  if (includeVideo && mainWindow) {
    mainWindow.webContents.send('stop-video-recording');
  }
  recordedVideoFrames = [];
  
  if (soxProcess) {
    soxProcess.kill('SIGINT');
    soxProcess = null;
  }
  
  // Delete temp files if they exist
  try {
    if (fs.existsSync(AUDIO_FILE_PATH)) {
      fs.unlinkSync(AUDIO_FILE_PATH);
    }
    if (fs.existsSync(VIDEO_FILE_PATH)) {
      fs.unlinkSync(VIDEO_FILE_PATH);
    }
  } catch (err) {
    console.error('Failed to delete temp recording:', err);
  }
  
  updateStatus('ready');
}

async function stopRecording() {
  console.log("Stopping recording...");
  updateStatus('processing');
  isRecording = false;
  
  // 1. Stop video recording if active
  if (includeVideo && mainWindow) {
    mainWindow.webContents.send('stop-video-recording');
  }

  // 2. Stop sox audio recording
  if (soxProcess) {
    const proc = soxProcess;
    soxProcess = null;
    
    // Wait for sox to finish writing the file
    await new Promise<void>((resolve) => {
      proc.on('close', () => {
        console.log("Sox process closed");
        resolve();
      });
      // Send Ctrl+C to sox to stop recording gracefully
      proc.kill('SIGINT');
      // Fallback timeout in case close event doesn't fire
      setTimeout(resolve, 2000);
    });
  }
  
  // 3. Wait for video file to arrive from renderer if needed
  if (includeVideo) {
    await new Promise(resolve => setTimeout(resolve, 1500));

    // Fail fast if renderer never confirmed start
    if (!videoRecordingConfirmed) {
      console.error('Video recording never started; skipping video processing');
      // We can still continue with audio-only if sox worked
    }
  }
  
  // Additional wait to ensure files are fully written
  await new Promise(resolve => setTimeout(resolve, 500));

  try {
    const hasVideo = includeVideo && fs.existsSync(VIDEO_FILE_PATH) && fs.statSync(VIDEO_FILE_PATH).size > 1000;
    const hasAudio = fs.existsSync(AUDIO_FILE_PATH) && fs.statSync(AUDIO_FILE_PATH).size > 1000;
    
    if (!hasAudio && !hasVideo) {
      console.error("No valid recording files exist");
      updateStatus('ready');
      playSound(400, 200); // Error sound
      return;
    }
    
    console.log("Calling Gemini API...");
    console.log("Has Video:", hasVideo, "Has Audio:", hasAudio);
    
    let refinedText = "";
    if (hasVideo && hasAudio) {
      console.log("Processing Video + Audio with Gemini...");
      refinedText = await processVideoWithGemini(VIDEO_FILE_PATH, AUDIO_FILE_PATH, recordedVideoFrames);
    } else if (hasAudio) {
      console.log("Processing Audio-only with Gemini...");
      refinedText = await processAudioWithGemini(AUDIO_FILE_PATH, recordedVideoFrames);
    } else if (hasVideo) {
      console.log("Processing Video-only with Gemini...");
      refinedText = await processVideoWithGemini(VIDEO_FILE_PATH, "", recordedVideoFrames);
    }
    
    console.log("Gemini response:", refinedText ? refinedText.substring(0, 100) + "..." : "(empty)");
    
    if (refinedText) {
      playSound(1000, 100); // Success sound
      // Always show in component first
      showTranscriptionResult(refinedText);
      // Also attempt to paste
      await pasteTextToActiveWindow(refinedText);
    }
  } catch (error) {
    console.error("Error processing:", error);
    playSound(400, 200); // Error sound
  } finally {
    // Clear video frames after processing
    recordedVideoFrames = [];
    videoRecordingConfirmed = false;
    // Clean up temp files
    try {
      if (fs.existsSync(AUDIO_FILE_PATH)) fs.unlinkSync(AUDIO_FILE_PATH);
      if (fs.existsSync(VIDEO_FILE_PATH)) fs.unlinkSync(VIDEO_FILE_PATH);
    } catch (err) {
      console.error('Failed to cleanup temp files:', err);
    }
    updateStatus('ready');
  }
}

async function processAudioWithGemini(filePath: string, videoFrames: string[] = []): Promise<string> {
  if (!ai) return "";

  const hasVideo = videoFrames.length > 0;
  
  const instruction = currentMode === 'transcription' 
    ? TRANSCRIPTION_INSTRUCTION 
    : (hasVideo ? VIDEO_SYSTEM_INSTRUCTION : SYSTEM_INSTRUCTION);
    
  const userPrompt = currentMode === 'transcription'
    ? "Transcribe this audio."
    : hasVideo 
      ? "Analyze the provided screen recording frames along with the audio. Use the visual context to better understand what I'm discussing and create a well-structured, clear prompt. The frames show what was on my screen while I was speaking."
      : "Transcribe and rewrite this audio for better clarity and organization.";

  try {
    const audioBuffer = fs.readFileSync(filePath);
    const base64Data = audioBuffer.toString('base64');

    // Build parts array - start with video frames if available
    const parts: any[] = [];
    
    // Add video frames (sample every few frames to reduce payload size)
    if (hasVideo) {
      const sampleRate = Math.max(1, Math.floor(videoFrames.length / 10)); // Max 10 frames
      for (let i = 0; i < videoFrames.length; i += sampleRate) {
        parts.push({
          inlineData: {
            mimeType: 'image/jpeg',
            data: videoFrames[i]
          }
        });
      }
      console.log(`Sending ${Math.ceil(videoFrames.length / sampleRate)} video frames to Gemini`);
    }
    
    // Add audio
    parts.push({
      inlineData: {
        mimeType: 'audio/wav',
        data: base64Data
      }
    });
    
    // Add text prompt
    parts.push({
      text: userPrompt
    });

    const response = await ai.models.generateContent({
      model: 'gemini-2.5-flash',
      contents: {
        parts: parts
      },
      config: {
        systemInstruction: instruction,
        temperature: 0.3, 
      }
    });

    return response.text || "";
  } catch (error) {
    console.error("Gemini API Error:", error);
    return "";
  }
}

async function processVideoWithGemini(videoPath: string, audioPath: string = "", videoFrames: string[] = []): Promise<string> {
  if (!ai) return "";

  const instruction = VIDEO_SYSTEM_INSTRUCTION;
  const userPrompt = audioPath 
    ? "Analyze the provided screen recording along with the spoken audio. Use the visual context from the video to better understand what I'm discussing and create a well-structured, clear prompt. The video shows what was on my screen while I was speaking."
    : "Analyze the provided video recording. Create a well-structured, clear prompt based on the visual context shown on the screen.";

  try {
    const parts: any[] = [];
    
    // Add video file
    if (videoPath && fs.existsSync(videoPath)) {
      const videoBuffer = fs.readFileSync(videoPath);
      parts.push({
        inlineData: {
          mimeType: 'video/webm',
          data: videoBuffer.toString('base64')
        }
      });
    }

    // Add audio file (from sox)
    if (audioPath && fs.existsSync(audioPath)) {
      const audioBuffer = fs.readFileSync(audioPath);
      parts.push({
        inlineData: {
          mimeType: 'audio/wav',
          data: audioBuffer.toString('base64')
        }
      });
    }
    
    // Optionally add extracted frames for better analysis
    if (videoFrames.length > 0) {
      const sampleRate = Math.max(1, Math.floor(videoFrames.length / 5)); // Max 5 additional frames
      for (let i = 0; i < Math.min(videoFrames.length, 5); i += sampleRate) {
        parts.push({
          inlineData: {
            mimeType: 'image/jpeg',
            data: videoFrames[i]
          }
        });
      }
      console.log(`Also sending ${Math.min(5, Math.ceil(videoFrames.length / sampleRate))} extracted frames`);
    }
    
    // Add text prompt
    parts.push({
      text: userPrompt
    });

    const response = await ai.models.generateContent({
      model: 'gemini-2.5-flash',
      contents: {
        parts: parts
      },
      config: {
        systemInstruction: instruction,
        temperature: 0.3,
      }
    });

    return response.text || "";
  } catch (error) {
    console.error("Gemini Video API Error:", error);
    return "";
  }
}

async function pasteTextToActiveWindow(text: string): Promise<boolean> {
  console.log("Attempting to paste text:", text.substring(0, 100) + (text.length > 100 ? "..." : ""));
  
  try {
    // Use Electron's native clipboard API instead of clipboardy
    clipboard.writeText(text);
    console.log("Text copied to clipboard successfully");
    
    const platform = os.platform();
    if (platform === 'win32') {
      // Use a more reliable method with proper promise handling
      await new Promise<void>((resolve, reject) => {
        // Use cscript with VBScript for more reliable key sending
        const vbsScript = `
Set WshShell = CreateObject("WScript.Shell")
WScript.Sleep 200
WshShell.SendKeys "^v"
`;
        const vbsPath = path.join(app.getPath('userData'), 'paste.vbs');
        fs.writeFileSync(vbsPath, vbsScript);
        
        exec(`cscript //nologo "${vbsPath}"`, (err, stdout, stderr) => {
          if (err) {
            console.error("Paste exec error:", err);
            console.error("stderr:", stderr);
            reject(err);
          } else {
            console.log("Paste command executed successfully");
            resolve();
          }
        });
      });
    } else if (platform === 'darwin') {
      await new Promise<void>((resolve, reject) => {
        exec('osascript -e \'tell application "System Events" to keystroke "v" using command down\'', (err) => {
          if (err) {
            console.error("Paste failed:", err);
            reject(err);
          } else {
            resolve();
          }
        });
      });
    } else if (platform === 'linux') {
      await new Promise<void>((resolve, reject) => {
        exec('xdotool key ctrl+v', (err) => {
          if (err) {
            console.error("Paste failed:", err);
            reject(err);
          } else {
            resolve();
          }
        });
      });
    }
    return true;
  } catch (err) {
    console.error("Failed to copy/paste:", err);
    return false;
  }
}

