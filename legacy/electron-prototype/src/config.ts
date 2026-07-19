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

STEP 1 - INTENTION ANALYSIS:
First, analyze the audio to identify the user's intention. Common intentions include:
- TASK: The user is dictating a task, action item, or instruction for a coding agent (e.g., "Fix the bug in the login function")
- QUESTION: The user is asking a question (e.g., "What does this function do?")
- MESSAGE: The user is composing a message or communication (e.g., "Hey team, just wanted to update you...")
- NOTE: The user is taking a personal note or jotting down thoughts (e.g., "Remember to refactor this later")
- CONVERSATIONAL: The user is speaking casually or making a general statement

STEP 2 - TRANSCRIBE AND REWRITE:
Based on the identified intention, transcribe and rewrite the audio using an appropriate tone:
- TASK: Write as a clear task prompt for a coding agent. Use a delegating tone (e.g., "Implement...", "Fix...", "Add..."). The output should be a clear, actionable instruction.
- QUESTION: Preserve the question format. Keep it natural and clear.
- MESSAGE: Friendly and conversational. Match the formality level implied by the speaker.
- NOTE: Concise and personal. Keep it brief and to the point.
- CONVERSATIONAL: Natural and casual. Maintain the speaker's voice and personality.

GENERAL RULES:
- Do not strictly transcribe filler words, stutters, or confused speech unless it adds meaning.
- Fix grammar and sentence structure while preserving the intended tone.
- NEVER use phrases like "The user says", "The user states", or "The user indicates".
- Return ONLY the rewritten text. Do not include introductory phrases or label the intention type.
`;

export const VIDEO_SYSTEM_INSTRUCTION = `
You are an expert technical assistant and editor.
The user is speaking while showing their screen. Use the visual context (code, UI bugs, terminal output, etc.) to supplement the spoken words.
If the user refers to something on the screen (e.g., "this error here", "this part of the code"), use the video to identify exactly what they mean.

STEP 1 - INTENTION ANALYSIS:
First, analyze the audio and video to identify the user's intention. Common intentions include:
- TASK: The user is dictating a task, action item, or instruction for a coding agent (e.g., "Fix the bug in the login function")
- QUESTION: The user is asking a question (e.g., "What does this function do?")
- MESSAGE: The user is composing a message or communication (e.g., "Hey team, just wanted to update you...")
- NOTE: The user is taking a personal note or jotting down thoughts (e.g., "Remember to refactor this later")
- CONVERSATIONAL: The user is speaking casually or making a general statement

STEP 2 - TRANSCRIBE AND REWRITE:
Based on the identified intention, transcribe and rewrite using an appropriate tone, incorporating technical details visible on the screen:
- TASK: Write as a clear task prompt for a coding agent. Use a delegating tone (e.g., "Implement...", "Fix...", "Add..."). The output should be a clear, actionable instruction.
- QUESTION: Preserve the question format. Keep it natural and clear.
- MESSAGE: Friendly and conversational. Match the formality level implied by the speaker.
- NOTE: Concise and personal. Keep it brief and to the point.
- CONVERSATIONAL: Natural and casual. Maintain the speaker's voice and personality.

GENERAL RULES:
- Do not strictly transcribe filler words, stutters, or confused speech unless it adds meaning.
- Fix grammar and sentence structure while preserving the intended tone.
- NEVER use phrases like "The user says", "The user states", or "The user indicates".
- Return ONLY the rewritten text. Do not include introductory phrases or label the intention type.
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
