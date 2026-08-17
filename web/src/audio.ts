import workletUrl from "./pcm-worklet.js?url";

export type SourceMode = "tab" | "mic" | "both";

const SAMPLE_RATE = 16000;
const FRAME_SAMPLES = 1600; // 100ms

/** 采到的音频不落地，直接按帧交给回调。 */
export class AudioCapture {
  private ctx: AudioContext | null = null;
  private streams: MediaStream[] = [];
  private node: AudioWorkletNode | null = null;

  async start(
    mode: SourceMode,
    onFrame: (pcm: ArrayBuffer, peak: number) => void,
    onSourceEnded: () => void,
  ): Promise<void> {
    const sources: MediaStream[] = [];

    if (mode === "tab" || mode === "both") {
      // Chrome 只在共享标签页时给音频轨，所以必须一起请求视频；帧率压到最低省 CPU。
      // 弹共享面板本身是浏览器安全要求，去不掉；能做的是把选错的路堵上：
      // displaySurface 让面板默认停在「Chrome 标签页」页签，monitorTypeSurfaces 藏掉
      // 整屏（macOS 上抓不到声音），selfBrowserSurface 藏掉本工具自己这个标签页，
      // surfaceSwitching 允许会中直接换共享的标签页而不用重弹面板。
      const display = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 1, displaySurface: "browser" },
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
        selfBrowserSurface: "exclude",
        monitorTypeSurfaces: "exclude",
        surfaceSwitching: "include",
      } as DisplayMediaStreamOptions);
      if (display.getAudioTracks().length === 0) {
        display.getTracks().forEach((t) => t.stop());
        throw new Error(
          "这次共享没有带上声音。重新开始，在共享面板里选「Chrome 标签页」并打开「同时分享标签页音频」。",
        );
      }
      display.getTracks().forEach((t) => t.addEventListener("ended", onSourceEnded));
      sources.push(display);
    }

    if (mode === "mic" || mode === "both") {
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      sources.push(mic);
    }

    const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await ctx.audioWorklet.addModule(workletUrl);
    const node = new AudioWorkletNode(ctx, "pcm-worklet", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      channelCountMode: "explicit",
      channelInterpretation: "speakers",
      processorOptions: { frameSamples: FRAME_SAMPLES },
    });
    node.port.onmessage = (e) => onFrame(e.data.pcm as ArrayBuffer, e.data.peak as number);

    for (const stream of sources) {
      if (stream.getAudioTracks().length === 0) continue;
      const src = ctx.createMediaStreamSource(stream);
      const gain = ctx.createGain();
      gain.gain.value = sources.length > 1 ? 0.85 : 1;
      src.connect(gain).connect(node);
    }

    // 静音接到出口，只为让 worklet 被驱动，不外放，避免回声
    const sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);

    this.ctx = ctx;
    this.node = node;
    this.streams = sources;
  }

  stop(): void {
    this.node?.port.close();
    this.node?.disconnect();
    this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
    this.ctx?.close().catch(() => {});
    this.node = null;
    this.streams = [];
    this.ctx = null;
  }
}
