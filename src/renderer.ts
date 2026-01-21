type RecordingStatus = 'ready' | 'recording' | 'processing' | 'error';

type Nullable<T> = T | null;

function requireElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element: #${id}`);
  }
  return element as T;
}

function setPressed(button: HTMLButtonElement, pressed: boolean) {
  button.setAttribute('aria-pressed', pressed ? 'true' : 'false');
}

function setHidden(element: HTMLElement, hidden: boolean) {
  element.toggleAttribute('hidden', hidden);
}

function setVisible(element: HTMLElement, visible: boolean) {
  element.classList.toggle('visible', visible);
}

const container = requireElement<HTMLDivElement>('container');
const statusText = requireElement<HTMLDivElement>('status-text');

const modePromptBtn = requireElement<HTMLButtonElement>('mode-prompt');
const modeTranscriptionBtn = requireElement<HTMLButtonElement>('mode-transcription');

const videoToggleBtn = requireElement<HTMLButtonElement>('video-toggle');
const testVideoBtn = requireElement<HTMLButtonElement>('test-video-btn');

const cancelBtn = requireElement<HTMLButtonElement>('cancel-btn');
const closeBtn = requireElement<HTMLButtonElement>('close-btn');

const transcriptionBox = requireElement<HTMLElement>('transcription-box');
const transcriptionText = requireElement<HTMLPreElement>('transcription-text');
const copyBtn = requireElement<HTMLButtonElement>('copy-btn');
const hideBtn = requireElement<HTMLButtonElement>('hide-btn');

const { electronAPI } = window;

let audioContext: Nullable<AudioContext> = null;
let currentTranscription = '';
let currentMode: 'prompt' | 'transcription' = 'prompt';
let includeVideo = false;

let videoStream: Nullable<MediaStream> = null;
let mediaRecorder: Nullable<MediaRecorder> = null;
let recordedChunks: BlobPart[] = [];

let videoCanvas: Nullable<HTMLCanvasElement> = null;
let videoContext: Nullable<CanvasRenderingContext2D> = null;
let videoElement: Nullable<HTMLVideoElement> = null;
let frameExtractionInterval: Nullable<number> = null;

function setStatus(status: RecordingStatus, text?: string) {
  container.dataset.state = status;
  if (text) statusText.textContent = text;
}

function initAudio() {
  if (!audioContext) {
    audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
  }
}

function playBeep(frequency: number, durationMs: number) {
  initAudio();
  if (!audioContext) return;

  const oscillator = audioContext.createOscillator();
  const gainNode = audioContext.createGain();

  oscillator.connect(gainNode);
  gainNode.connect(audioContext.destination);

  oscillator.frequency.value = frequency;
  oscillator.type = 'sine';

  gainNode.gain.setValueAtTime(0.28, audioContext.currentTime);
  gainNode.gain.exponentialRampToValueAtTime(
    0.01,
    audioContext.currentTime + durationMs / 1000
  );

  oscillator.start(audioContext.currentTime);
  oscillator.stop(audioContext.currentTime + durationMs / 1000);
}

function updateModeUI() {
  const isPrompt = currentMode === 'prompt';
  setPressed(modePromptBtn, isPrompt);
  setPressed(modeTranscriptionBtn, !isPrompt);

  // Video is only relevant for prompt mode.
  setHidden(videoToggleBtn, !isPrompt);
  setHidden(testVideoBtn, !isPrompt);

  if (!isPrompt) {
    includeVideo = false;
    setPressed(videoToggleBtn, false);
    electronAPI.setIncludeVideo(false);
  }
}

function notifyVideoError(message: string) {
  console.error('Video recording error:', message);
  setStatus('error', 'Screen capture blocked');
  electronAPI.sendVideoRecordingError(message);
}

function initVideoCanvas() {
  if (videoCanvas && videoContext) return;
  videoCanvas = document.createElement('canvas');
  videoContext = videoCanvas.getContext('2d');
}

async function startVideoRecording() {
  try {
    recordedChunks = [];
    electronAPI.sendVideoRecordingError('');

    const screenStream = await navigator.mediaDevices
      .getDisplayMedia({
        audio: false,
        video: {
          frameRate: { ideal: 10, max: 15 }
        }
      })
      .catch((error) => {
        notifyVideoError('Screen capture denied or failed. Please allow screen recording.');
        throw error;
      });

    videoStream = screenStream;

    electronAPI.sendVideoRecordingStarted({
      screen: 'display-media',
      audioTracks: screenStream.getAudioTracks().map((track) => track.label)
    });

    let recorderOptions: MediaRecorderOptions = {};

    try {
      if (
        window.MediaRecorder &&
        MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus')
      ) {
        recorderOptions = { mimeType: 'video/webm;codecs=vp9,opus' };
      } else if (
        window.MediaRecorder &&
        MediaRecorder.isTypeSupported('video/webm;codecs=vp8,opus')
      ) {
        recorderOptions = { mimeType: 'video/webm;codecs=vp8,opus' };
      } else if (window.MediaRecorder && MediaRecorder.isTypeSupported('video/webm')) {
        recorderOptions = { mimeType: 'video/webm' };
      }
    } catch (error) {
      console.warn('MediaRecorder.isTypeSupported failed, using defaults:', error);
    }

    mediaRecorder = new MediaRecorder(screenStream, recorderOptions);

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        recordedChunks.push(event.data);
      }
    };

    mediaRecorder.onerror = (event) => {
      const error = (event as any)?.error;
      notifyVideoError(`MediaRecorder error: ${error?.message || error || 'unknown'}`);
    };

    mediaRecorder.start();

    initVideoCanvas();

    videoElement = document.createElement('video');
    videoElement.srcObject = screenStream;
    videoElement.autoplay = true;
    videoElement.muted = true;

    await new Promise<void>((resolve) => {
      if (!videoElement) return resolve();
      videoElement.onloadedmetadata = () => resolve();
      setTimeout(resolve, 1000);
    });

    if (!videoElement || !videoCanvas || !videoContext) return;

    videoCanvas.width = videoElement.videoWidth || 1920;
    videoCanvas.height = videoElement.videoHeight || 1080;

    frameExtractionInterval = window.setInterval(() => {
      if (!videoElement || !videoCanvas || !videoContext) return;
      if (videoElement.readyState !== videoElement.HAVE_ENOUGH_DATA) return;

      try {
        videoContext.drawImage(videoElement, 0, 0, videoCanvas.width, videoCanvas.height);
        const frameData = videoCanvas.toDataURL('image/jpeg', 0.6).split(',')[1];
        electronAPI.sendVideoFrame(frameData);
      } catch (error) {
        console.error('Error capturing frame:', error);
      }
    }, 1000);
  } catch (error: any) {
    console.error('Failed to start video recording:', error);
    notifyVideoError(error?.message || 'Failed to start video recording.');
  }
}

async function stopVideoRecording() {
  if (frameExtractionInterval) {
    clearInterval(frameExtractionInterval);
    frameExtractionInterval = null;
  }

  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();

    // Let final chunks flush.
    await new Promise((resolve) => setTimeout(resolve, 500));

    if (recordedChunks.length > 0) {
      const blob = new Blob(recordedChunks, { type: 'video/webm' });

      const reader = new FileReader();
      reader.onloadend = () => {
        const result = String(reader.result || '');
        const base64data = result.split(',')[1] || '';
        if (base64data) {
          electronAPI.sendVideoFileComplete(base64data);
        }
      };
      reader.readAsDataURL(blob);
    }

    recordedChunks = [];
    mediaRecorder = null;
  }

  if (videoStream) {
    for (const track of videoStream.getTracks()) {
      track.stop();
    }
    videoStream = null;
  }

  if (videoElement) {
    videoElement.srcObject = null;
    videoElement = null;
  }
}

function showTranscription(text: string) {
  currentTranscription = text;
  transcriptionText.textContent = text;
  setVisible(transcriptionBox, true);
}

function hideTranscription() {
  setVisible(transcriptionBox, false);
  electronAPI.hideTranscription();
}

async function copyTranscription() {
  try {
    await navigator.clipboard.writeText(currentTranscription);
    copyBtn.textContent = 'Copied';
    window.setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
  } catch (error) {
    console.error('Failed to copy:', error);
    copyBtn.textContent = 'Copy failed';
    window.setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
  }
}

function setMode(mode: 'prompt' | 'transcription') {
  currentMode = mode;
  electronAPI.setMode(mode);
  updateModeUI();
}

modePromptBtn.addEventListener('click', () => setMode('prompt'));
modeTranscriptionBtn.addEventListener('click', () => setMode('transcription'));

videoToggleBtn.addEventListener('click', () => {
  includeVideo = !includeVideo;
  setPressed(videoToggleBtn, includeVideo);
  electronAPI.setIncludeVideo(includeVideo);
});

testVideoBtn.addEventListener('click', () => {
  if (testVideoBtn.disabled) return;
  testVideoBtn.disabled = true;
  const original = testVideoBtn.textContent;
  testVideoBtn.textContent = '…';

  electronAPI.testVideoCapture();

  window.setTimeout(() => {
    // Safety: if main never replies.
    if (!testVideoBtn.disabled) return;
    testVideoBtn.disabled = false;
    testVideoBtn.textContent = original;
  }, 12000);
});

cancelBtn.addEventListener('click', () => {
  electronAPI.cancelRecording();
});

closeBtn.addEventListener('click', () => {
  electronAPI.closeApp();
});

copyBtn.addEventListener('click', () => {
  void copyTranscription();
});

hideBtn.addEventListener('click', () => {
  hideTranscription();
});

updateModeUI();

// Main process -> renderer events

electronAPI.onStartVideoRecording(() => {
  void startVideoRecording();
});

electronAPI.onStopVideoRecording(() => {
  void stopVideoRecording();
});

electronAPI.onUpdateStatus((status) => {
  if (status === 'recording') {
    setStatus('recording', 'Recording…');
    setHidden(cancelBtn, false);
    setVisible(transcriptionBox, false);
    return;
  }

  if (status === 'processing') {
    setStatus('processing', 'Processing…');
    setHidden(cancelBtn, true);
    return;
  }

  setStatus('ready', 'Ready');
  setHidden(cancelBtn, true);
});

electronAPI.onPlaySound(({ frequency, duration }) => {
  playBeep(frequency, duration);
});

electronAPI.onShowTranscription((text) => {
  showTranscription(text);
});

electronAPI.onVideoTestComplete((result) => {
  testVideoBtn.disabled = false;
  testVideoBtn.textContent = 'T';

  if (!result?.success) {
    setStatus('error', 'Video test failed');
    window.setTimeout(() => setStatus('ready', 'Ready'), 1500);
    return;
  }

  setStatus('ready', 'Video OK');
  window.setTimeout(() => setStatus('ready', 'Ready'), 1200);
});
