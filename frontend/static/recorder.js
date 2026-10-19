(() => {
  const root = document.getElementById('recorder');
  const MIN = Number(root.dataset.min);
  const MAX = Number(root.dataset.max);
  const TARGET_SR = 16000;

  const recordBtn = document.getElementById('record');
  const timerEl = document.getElementById('timer');
  const statusEl = document.getElementById('status');
  const review = document.getElementById('review');
  const preview = document.getElementById('preview');
  const againBtn = document.getElementById('again');
  const submitBtn = document.getElementById('submit');
  const fileInput = document.getElementById('file');

  let recorder, stream, chunks = [], startedAt = 0, tick = null;
  let wavBlob = null;

  const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  const say = (msg, isError = false) => {
    statusEl.textContent = msg;
    statusEl.classList.toggle('error-text', isError);
  };

  function showReview(blob) {
    wavBlob = blob;
    if (preview.src) URL.revokeObjectURL(preview.src);
    preview.src = URL.createObjectURL(blob);
    review.hidden = false;
    recordBtn.hidden = true;
  }

  async function start() {
    try {
      // Browser "enhancements" would alter the speech signal before the clinician hears it.
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
    } catch (err) {
      say('Could not use the microphone. Allow microphone access, or upload a WAV file below.', true);
      return;
    }
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = e => e.data.size && chunks.push(e.data);
    recorder.onstop = onStopped;
    recorder.start();
    startedAt = performance.now();
    recordBtn.textContent = '■ Stop';
    recordBtn.classList.add('recording');
    say('Recording… speak now.');
    tick = setInterval(() => {
      const s = (performance.now() - startedAt) / 1000;
      timerEl.textContent = fmt(s);
      if (s >= MAX) stop();
    }, 200);
  }

  function stop() {
    clearInterval(tick);
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    stream?.getTracks().forEach(t => t.stop());
  }

  async function onStopped() {
    recordBtn.textContent = '● Record';
    recordBtn.classList.remove('recording');
    const seconds = (performance.now() - startedAt) / 1000;
    if (seconds < MIN) {
      say(`That was too short. Please record at least ${MIN} seconds.`, true);
      return;
    }
    say('Preparing your recording…');
    try {
      const blob = new Blob(chunks, { type: recorder.mimeType });
      showReview(await toWav(blob));
      say('Listen back, then submit or record again.');
    } catch (err) {
      say('Your browser could not process the recording. Try the WAV upload option below.', true);
    }
  }

  // Decode whatever the browser recorded (webm/mp4) and re-encode as 16 kHz mono 16-bit WAV.
  async function toWav(blob) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    ctx.close();
    const frames = Math.ceil(decoded.duration * TARGET_SR);
    const offline = new OfflineAudioContext(1, frames, TARGET_SR);
    const src = offline.createBufferSource();
    src.buffer = decoded;
    src.connect(offline.destination);  // multi-channel input is down-mixed to mono
    src.start();
    const rendered = await offline.startRendering();
    return encodeWav(rendered.getChannelData(0), TARGET_SR);
  }

  function encodeWav(samples, sr) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
    str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE');
    str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, 'data'); v.setUint32(40, samples.length * 2, true);
    samples.forEach((s, i) => {
      const x = Math.max(-1, Math.min(1, s));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    });
    return new Blob([buf], { type: 'audio/wav' });
  }

  async function submit() {
    submitBtn.disabled = againBtn.disabled = true;
    say('Uploading and checking your recording…');
    const form = new FormData();
    form.append('assignment_id', root.dataset.assignment);
    form.append('file', wavBlob, 'recording.wav');
    try {
      const res = await fetch('/api/recordings', {
        method: 'POST',
        body: form,
        headers: { 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Upload failed. Please try again.');
      say('Saved! Your clinician can now listen to it.');
      setTimeout(() => { location.href = root.dataset.doneUrl; }, 1200);
    } catch (err) {
      say(err.message, true);
      submitBtn.disabled = againBtn.disabled = false;
    }
  }

  recordBtn.addEventListener('click', () => (recorder && recorder.state === 'recording' ? stop() : start()));
  againBtn.addEventListener('click', () => {
    review.hidden = true; recordBtn.hidden = false; wavBlob = null; timerEl.textContent = '0:00';
    say('Tap Record when you are ready.');
  });
  submitBtn.addEventListener('click', submit);
  fileInput.addEventListener('change', () => {
    const f = fileInput.files[0];
    if (f) { showReview(f); say('Listen back, then submit.'); }
  });
})();
