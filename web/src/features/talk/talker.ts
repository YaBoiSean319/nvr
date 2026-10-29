/**
 * Talking through a camera's speaker (#147): the microphone, as 8 kHz mono 16-bit PCM, over the authenticated talk WebSocket. The microphone is opened when the
 * talk control is pressed (so the browser asks for permission on the first talk, not before) and closed the moment it is released.
 */

export type TalkPhase = "idle" | "connecting" | "talking";
export type TalkOutcome = { kind: "ended"; reason: string } | { kind: "busy"; talker: string } | { kind: "refused"; detail: string } | { kind: "no_microphone"; detail: string };

const SAMPLE_RATE = 8000;
const BLOCK = 512; // 64 ms at 8 kHz
const MAX_QUEUED_BLOCKS = 48; // About three seconds of speech spoken while the camera is still being reached

function pcm16(samples: Float32Array): ArrayBuffer {
  const out = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index]!));
    out[index] = value < 0 ? value * 0x8000 : value * 0x7fff;
  }
  return out.buffer;
}

export class Talker {
  private socket: WebSocket | null = null;
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private processor: ScriptProcessorNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private generation = 0;
  private ready = false;
  private queued: ArrayBuffer[] = [];

  constructor(private readonly onPhase: (phase: TalkPhase) => void, private readonly onOutcome: (outcome: TalkOutcome) => void) {}

  /** Start talking: the microphone first (the permission prompt), then the camera. Resolves once talking or refused; `release` before then cancels. */
  async press(cameraId: string): Promise<void> {
    const mine = ++this.generation;
    this.ready = false;
    this.queued = [];
    this.onPhase("connecting");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch (error) {
      if (mine === this.generation) this.fail({ kind: "no_microphone", detail: error instanceof DOMException && error.name === "NotAllowedError" ? "The browser did not allow the microphone." : "No microphone could be used." });
      return;
    }
    if (mine !== this.generation) { this.closeMicrophone(); return; }
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${scheme}//${location.host}/api/cameras/${encodeURIComponent(cameraId)}/talk/stream`);
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    this.capture(); // Speech starts at once: what is said while the camera is being reached is sent as soon as it is ready
    socket.onmessage = event => {
      if (mine !== this.generation || typeof event.data !== "string") return;
      const message = JSON.parse(event.data) as { type: string; talker?: string; detail?: string; reason?: string };
      if (message.type === "ready") this.begin();
      else if (message.type === "busy") this.fail({ kind: "busy", talker: message.talker ?? "Someone" });
      else if (message.type === "ended") this.fail({ kind: "ended", reason: message.reason ?? "released" });
      else if (message.detail) this.fail({ kind: "refused", detail: message.detail });
    };
    socket.onclose = () => { if (mine === this.generation && this.socket === socket) this.fail({ kind: "ended", reason: "closed" }); };
    socket.onerror = () => undefined;
  }

  private capture(): void {
    if (!this.stream) return;
    const context = new AudioContext({ sampleRate: SAMPLE_RATE }); // The browser resamples the microphone to this
    this.context = context;
    this.source = context.createMediaStreamSource(this.stream);
    this.processor = context.createScriptProcessor(BLOCK, 1, 1);
    this.processor.onaudioprocess = event => {
      const block = pcm16(event.inputBuffer.getChannelData(0));
      if (this.ready && this.socket?.readyState === WebSocket.OPEN) this.socket.send(block);
      else if (!this.ready && this.queued.length < MAX_QUEUED_BLOCKS) this.queued.push(block);
    };
    this.source.connect(this.processor);
    this.processor.connect(context.destination); // The processor only runs while connected on; it outputs silence
    void context.resume();
  }

  private begin(): void {
    if (!this.socket) return;
    this.ready = true;
    for (const block of this.queued) this.socket.send(block);
    this.queued = [];
    this.onPhase("talking");
  }

  /** Stop at once: the microphone is released, then the camera told. */
  release(): void {
    this.generation += 1;
    this.ready = false;
    this.queued = [];
    const socket = this.socket;
    this.closeMicrophone();
    this.socket = null;
    if (socket) {
      try {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "stop" }));
        socket.close();
      } catch { /* already closed */ }
    }
    this.onPhase("idle");
  }

  private fail(outcome: TalkOutcome): void {
    this.release();
    this.onOutcome(outcome);
  }

  private closeMicrophone(): void {
    if (this.processor) this.processor.onaudioprocess = null;
    this.processor?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
    void this.context?.close().catch(() => undefined);
    this.processor = null; this.source = null; this.stream = null; this.context = null;
  }
}
