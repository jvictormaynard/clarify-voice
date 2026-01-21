import { app } from 'electron';
import path from 'path';
import fs from 'fs';
import dotenv from 'dotenv';

// Get the app root directory
export const appRoot = app.isPackaged
  ? path.dirname(app.getPath('exe'))
  : path.join(__dirname, '..');

// Load environment variables
const envPath = app.isPackaged
  ? path.join(process.resourcesPath, '.env')
  : path.join(appRoot, '.env');

console.log('Looking for .env at:', envPath);
console.log('.env exists:', fs.existsSync(envPath));
dotenv.config({ path: envPath });

// Platform detection
export const isWindows = process.platform === 'win32';
export const isLinux = process.platform === 'linux';
export const isMac = process.platform === 'darwin';

// API Configuration
export const API_KEY = process.env.API_KEY;

// File paths
export const AUDIO_FILE_PATH = path.join(app.getPath('userData'), 'temp_recording.wav');
export const VIDEO_FILE_PATH = path.join(app.getPath('userData'), 'temp_recording.webm');

// SoX configuration
export const soxDir = isWindows
  ? (app.isPackaged
    ? path.join(process.resourcesPath, 'extra', 'sox-14.4.2')
    : path.join(appRoot, 'extra', 'sox-14.4.2'))
  : '';

export const soxExe = isWindows ? path.join(soxDir, 'sox.exe') : 'sox';

// Add sox directory to PATH on Windows
if (isWindows && soxDir) {
  process.env.PATH = soxDir + path.delimiter + (process.env.PATH || '');
}

// AI System Instructions
export const SYSTEM_INSTRUCTION = `
You are an expert editor and transcriber.
Your task is to take the provided audio input, transcribe it, and then rewrite it as a clear task prompt for a coding agent.
Do not strictly transcribe filler words, stutters, or confused speech unless it adds meaning.
Fix grammar and sentence structure.

CRITICAL: You are writing a task prompt to delegate work to an AI coding agent.
Write in a delegating tone, e.g., "You need to...", "Implement...", "Fix...", "Add...", etc.
NEVER use phrases like "The user says", "The user states", or "The user indicates" to reference the interlocutor.
The output should be a clear, actionable instruction that tells the coding agent exactly what to do.
Return ONLY the task prompt. Do not include introductory phrases like "Here is the prompt".
`;

export const VIDEO_SYSTEM_INSTRUCTION = `
You are an expert technical assistant and editor.
Your task is to analyze the provided screen recording and the accompanying audio to understand a technical issue or request.
The user is speaking while showing their screen. Use the visual context (code, UI bugs, terminal output, etc.) to supplement the spoken words.
If the user refers to something on the screen (e.g., "this error here", "this part of the code"), use the video to identify exactly what they mean.
Rewrite the user's speech into a clear task prompt that delegates work to an AI coding agent.
The final output should be a well-structured instruction that tells the coding agent exactly what to do, incorporating technical details visible on the screen.

CRITICAL: You are writing a task prompt to delegate work to an AI coding agent.
Write in a delegating tone, e.g., "You need to...", "Implement...", "Fix...", "Add...", etc.
NEVER use phrases like "The user says", "The user states", or "The user indicates" to reference the interlocutor.
The output should be a clear, actionable instruction that a coding agent can execute.
Return ONLY the task prompt. Do not include introductory phrases.
`;

export const TRANSCRIPTION_INSTRUCTION = `
You are an expert transcriber.
Your task is to transcribe the provided audio input directly.
Clean up filler words (um, uh, like) and correct basic grammar, but keep the original meaning and structure intact.
Transcribe in the exact language spoken in the audio.
Return ONLY the transcribed text. Do not include introductory phrases.
`;

// Log configuration
console.log('Platform:', process.platform);
console.log('SoX Directory:', soxDir || '(system)');
console.log('API_KEY loaded:', API_KEY ? 'Yes' : 'No');
console.log('Is Packaged:', app.isPackaged);
console.log('SoX executable:', soxExe);
