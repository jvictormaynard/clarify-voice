import fs from 'fs';
import { ai, currentMode } from './state';
import {
  SYSTEM_INSTRUCTION,
  VIDEO_SYSTEM_INSTRUCTION,
  TRANSCRIPTION_INSTRUCTION
} from './config';

/**
 * Process audio with Gemini AI
 */
export async function processAudioWithGemini(
  filePath: string,
  videoFrames: string[] = []
): Promise<string> {
  if (!ai) return '';

  const hasVideo = videoFrames.length > 0;

  const instruction = currentMode === 'transcription'
    ? TRANSCRIPTION_INSTRUCTION
    : (hasVideo ? VIDEO_SYSTEM_INSTRUCTION : SYSTEM_INSTRUCTION);

  const userPrompt = currentMode === 'transcription'
    ? 'Transcribe this audio.'
    : hasVideo
      ? 'Analyze the provided screen recording frames along with the audio. Use the visual context to better understand what I\'m discussing and create a well-structured, clear prompt. The frames show what was on my screen while I was speaking.'
      : 'Transcribe and rewrite this audio for better clarity and organization.';

  try {
    const audioBuffer = fs.readFileSync(filePath);
    const base64Data = audioBuffer.toString('base64');

    const parts: any[] = [];

    // Add video frames (sample every few frames to reduce payload size)
    if (hasVideo) {
      const sampleRate = Math.max(1, Math.floor(videoFrames.length / 10));
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
    parts.push({ text: userPrompt });

    const response = await ai.models.generateContent({
      model: 'gemini-2.5-flash',
      contents: { parts },
      config: {
        systemInstruction: instruction,
        temperature: 0.3,
      }
    });

    return response.text || '';
  } catch (error) {
    console.error('Gemini API Error:', error);
    return '';
  }
}

/**
 * Process video with Gemini AI
 */
export async function processVideoWithGemini(
  videoPath: string,
  audioPath: string = '',
  videoFrames: string[] = []
): Promise<string> {
  if (!ai) return '';

  const instruction = VIDEO_SYSTEM_INSTRUCTION;
  const userPrompt = audioPath
    ? 'Analyze the provided screen recording along with the spoken audio. Use the visual context from the video to better understand what I\'m discussing and create a well-structured, clear prompt. The video shows what was on my screen while I was speaking.'
    : 'Analyze the provided video recording. Create a well-structured, clear prompt based on the visual context shown on the screen.';

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

    // Add audio file
    if (audioPath && fs.existsSync(audioPath)) {
      const audioBuffer = fs.readFileSync(audioPath);
      parts.push({
        inlineData: {
          mimeType: 'audio/wav',
          data: audioBuffer.toString('base64')
        }
      });
    }

    // Add extracted frames for better analysis
    if (videoFrames.length > 0) {
      const sampleRate = Math.max(1, Math.floor(videoFrames.length / 5));
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
    parts.push({ text: userPrompt });

    const response = await ai.models.generateContent({
      model: 'gemini-2.5-flash',
      contents: { parts },
      config: {
        systemInstruction: instruction,
        temperature: 0.3,
      }
    });

    return response.text || '';
  } catch (error) {
    console.error('Gemini Video API Error:', error);
    return '';
  }
}
