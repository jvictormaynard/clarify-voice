/**
 * CLARIFYVOICE DESKTOP AGENT
 * --------------------------
 * This script runs in the background on your computer to provide system-wide
 * dictation using Gemini 2.5 Flash.
 * 
 * CAPABILITIES:
 * - Global Hotkey (Alt + L) to toggle recording anywhere.
 * - Records microphone audio.
 * - Uses Gemini 2.5 Flash to transcribe and clarity-check speech.
 * - Pastes the result directly into your active application.
 * 
 * PREREQUISITES:
 * 1. Node.js installed (v18+)
 * 2. SoX (Sound eXchange) installed and in your PATH.
 *    - Windows: Download from SourceForge, add to PATH.
 *    - Mac: `brew install sox`
 *    - Linux: `sudo apt-get install sox`
 * 3. (Linux Only) xsel or xclip for clipboard, and xdotool for typing.
 *    - `sudo apt-get install xsel xdotool`
 * 
 * SETUP INSTRUCTIONS:
 * 1. Create a new folder on your computer.
 * 2. Save this file as `agent.ts` inside that folder.
 * 3. Open a terminal in that folder and run:
 *    npm init -y
 *    npm install @google/genai node-record-lpcm16 uiohook-napi dotenv clipboardy
 * 
 * 4. Create a .env file in the same folder:
 *    API_KEY=your_gemini_api_key_here
 * 
 * RUNNING:
 * npx ts-node agent.ts
 */

import { GoogleGenAI } from "@google/genai";
import { uIOhook, UiohookKey } from "uiohook-napi";
import recorder from "node-record-lpcm16";
import fs from "fs";
import path from "path";
import dotenv from "dotenv";
import clipboardy from "clipboardy";
import { exec } from "child_process";
import os from "os";

// Load environment variables
dotenv.config();

const API_KEY = process.env.API_KEY;
if (!API_KEY) {
  console.error("ERROR: API_KEY not found in .env file.");
  process.exit(1);
}

// Initialize Gemini
const ai = new GoogleGenAI({ apiKey: API_KEY });

// Configuration
const HOTKEY_KEY = UiohookKey.L;
const AUDIO_FILE_PATH = path.join(__dirname, "temp_recording.wav");

// State
let isRecording = false;
let recordingStream: any = null;
let fileStream: fs.WriteStream | null = null;

const SYSTEM_INSTRUCTION = `
You are an expert editor and transcriber. 
Your task is to take the provided audio input, transcribe it, and then rewrite it to be more organized, clear, and comprehensible.
Do not strictly transcribe filler words, stutters, or confused speech unless it adds meaning.
Fix grammar and sentence structure.
The tone should be professional yet natural.
Return ONLY the rewritten text. Do not include introductory phrases like "Here is the rewritten text".
`;

console.log("----------------------------------------");
console.log("  ClarifyVoice Background Agent Running");
console.log("----------------------------------------");
console.log("  Press [Alt + L] to toggle recording.");
console.log("  Press [Ctrl + C] to exit.");
console.log("----------------------------------------");

// Global Hotkey Listener
uIOhook.on('keydown', (e) => {
  // Check for Alt + L
  if (e.altKey && e.keycode === HOTKEY_KEY) {
    toggleRecording();
  }
});

uIOhook.start();

async function toggleRecording() {
  if (isRecording) {
    await stopRecording();
  } else {
    startRecording();
  }
}

function startRecording() {
  console.log("🎤 Listening... (Press Alt+L to stop)");
  isRecording = true;
  
  try {
    fileStream = fs.createWriteStream(AUDIO_FILE_PATH, { encoding: 'binary' });
    
    recordingStream = recorder.record({
      sampleRate: 16000,
      threshold: 0, 
      verbose: false,
      recordProgram: 'sox', // Make sure SoX is installed!
    });
    
    recordingStream.stream().pipe(fileStream);
  } catch (error) {
    console.error("Failed to start recording:", error);
    console.error("Make sure 'sox' is installed and in your PATH.");
    isRecording = false;
  }
}

async function stopRecording() {
  console.log("⏳ Processing audio...");
  isRecording = false;
  
  if (recordingStream) {
    recordingStream.stop();
  }
  
  // Allow file stream to close
  await new Promise(resolve => setTimeout(resolve, 500));

  try {
    const refinedText = await processAudioWithGemini(AUDIO_FILE_PATH);
    if (refinedText) {
      console.log(`✨ Transcribed: "${refinedText.substring(0, 50)}..."`);
      await pasteTextToActiveWindow(refinedText);
    } else {
        console.log("⚠️ No speech detected or empty response.");
    }
  } catch (error) {
    console.error("❌ Error processing:", error);
  } finally {
      console.log("✅ Ready.");
  }
}

async function processAudioWithGemini(filePath: string): Promise<string> {
  try {
    const audioBuffer = fs.readFileSync(filePath);
    const base64Data = audioBuffer.toString('base64');

    const response = await ai.models.generateContent({
      model: 'gemini-2.5-flash',
      contents: {
        parts: [
          {
            inlineData: {
              mimeType: 'audio/wav',
              data: base64Data
            }
          },
          {
            text: "Transcribe and rewrite this audio for better clarity and organization."
          }
        ]
      },
      config: {
        systemInstruction: SYSTEM_INSTRUCTION,
        temperature: 0.3, 
      }
    });

    return response.text || "";
  } catch (error) {
    console.error("Gemini API Error:", error);
    return "";
  }
}

async function pasteTextToActiveWindow(text: string) {
  try {
    // 1. Copy text to clipboard
    await clipboardy.write(text);
    console.log("📋 Copied to clipboard.");

    // 2. Simulate Paste (Ctrl+V or Cmd+V)
    const platform = os.platform();
    console.log(`⌨️  Simulating paste for ${platform}...`);

    if (platform === 'win32') {
      // Windows: PowerShell SendKeys
      exec('powershell -c "(New-Object -ComObject WScript.Shell).SendKeys(\'^v\')"', (err) => {
          if (err) console.error("Paste failed:", err);
      });
    } else if (platform === 'darwin') {
      // macOS: AppleScript
      exec('osascript -e \'tell application "System Events" to keystroke "v" using command down\'', (err) => {
          if (err) console.error("Paste failed:", err);
      });
    } else if (platform === 'linux') {
      // Linux: xdotool
      exec('xdotool key ctrl+v', (err) => {
           if (err) console.error("Paste failed. Ensure 'xdotool' is installed: sudo apt install xdotool", err);
      });
    }
  } catch (err) {
    console.error("Failed to copy/paste:", err);
  }
}