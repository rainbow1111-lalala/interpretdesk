import workletUrl from "./pcm-worklet.js?url";

export type SourceMode = "tab" | "mic" | "both";

/** 手机上的 Safari 与 Chrome 都没有 getDisplayMedia，抓会议标签页那条路走不通，
 *  界面上就不该给这个选项，否则用户点了只会得到一句报错。 */
export function canCaptureTab(): boolean {
  return typeof navigator !== "undefined"
    && !!navigator.mediaDevices
    && typeof navigator.mediaDevices.getDisplayMedia === "function";
}

const SAMPLE_RATE = 16000;
const FRAME_SAMPLES = 1600; // 100ms

export type MicDevice = { id: string; label: string };

/**
 * allowPrompt=false 时只枚举、不索权限。页面刚打开就弹麦克风授权框会把访客吓走，
 * 所以进页面时用 false，等用户真的点「开始记录」（一次明确的手势）再用 true。
 */
export async function listMics(allowPrompt = true): Promise<MicDevice[]> {
  let devices = await navigator.mediaDevices.enumerateDevices();
  if (allowPrompt && !devices.some((d) => d.kind === "audioinput" && d.label)) {
    // 授权前拿不到设备名，先要一次权限再列（只会弹一次系统授权）
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
    devices = await navigator.mediaDevices.enumerateDevices();
  }
  return devices
    .filter((d) => d.kind === "audioinput")
    .map((d) => ({ id: d.deviceId, label: d.label || "麦克风" }));
}

export function preferredMic(list: MicDevice[]): string {
  // macOS 连续互通可能把 iPhone 设为系统默认输入：点开始会弹 iPhone 连接页，
  // 本机麦克风什么都收不到。默认优先本机内建麦克风，其次避开 iPhone
  const builtin = list.find((d) => /内建|built-?in|macbook/i.test(d.label));
  if (builtin) return builtin.id;
  const notPhone = list.find((d) => !/iphone|ipad|continuity|连续互通/i.test(d.label));
  return (notPhone ?? list[0])?.id ?? "";
}

/** 采到的音频不落地，直接按帧交给回调。 */
export class AudioCapture {
  private ctx: AudioContext | null = null;
  private streams: MediaStream[] = [];
  private node: AudioWorkletNode | null = null;

  async start(
    mode: SourceMode,
    onFrame: (pcm: ArrayBuffer, peak: number) => void,
    onSourceEnded: () => void,
    micDeviceId?: string,
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
        // 读出这次到底共享了什么，把差在哪直接说清楚，不让用户猜
        const surface = display.getVideoTracks()[0]?.getSettings()?.displaySurface;
        display.getTracks().forEach((t) => t.stop());
        if (surface === "window") {
          throw new Error(
            "这次共享的是「窗口」，macOS 上窗口共享带不了声音，音频开关对它无效。" +
              "重新开始，在共享面板顶部切到「Chrome 标签页」页签，选会议标签页。" +
              "会议开在桌面端应用（非浏览器）时，声源改选「麦克风（外放收音）」外放收音。",
          );
        }
        if (surface === "monitor") {
          throw new Error(
            "这次共享的是「整个屏幕」，macOS 上抓不到系统声音。重新开始，" +
              "在共享面板选「Chrome 标签页」，或声源改选「麦克风（外放收音）」外放收音。",
          );
        }
        throw new Error(
          "这次共享的标签页没有带上声音，底部「同时分享标签页音频」开关可能没生效。" +
            "重新开始再试一次；不行就把声源改成「麦克风（外放收音）」，外放会议声音用麦克风收。",
        );
      }
      display.getTracks().forEach((t) => t.addEventListener("ended", onSourceEnded));
      sources.push(display);
    }

    if (mode === "mic" || mode === "both") {
      // 纯麦克风模式的典型用法是收公放：会议声音从扬声器放出来，麦克风拾音。这时
      // 回声消除会把从本机 Chrome 放出的会议声当回声消掉，降噪也削弱远场声音，都要关；
      // 自动增益保留，补偿麦克风到扬声器的距离。混合模式下麦克风只负责收人声，
      // 回声消除要开着，否则标签页的声音会被麦克风重复收一遍。
      const farField = mode === "mic";
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(micDeviceId ? { deviceId: { exact: micDeviceId } } : {}),
          echoCancellation: !farField,
          noiseSuppression: !farField,
          autoGainControl: true,
        },
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
