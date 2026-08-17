// 把图上下来的音频攒成 100 毫秒一帧的 16 位 PCM，顺手算个电平给录音条用。
class PCMWorklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.frame = (options.processorOptions && options.processorOptions.frameSamples) || 1600;
    this.buf = new Float32Array(this.frame);
    this.n = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];
    for (let i = 0; i < channel.length; i++) {
      this.buf[this.n++] = channel[i];
      if (this.n === this.frame) {
        const pcm = new Int16Array(this.frame);
        let peak = 0;
        for (let j = 0; j < this.frame; j++) {
          let s = this.buf[j];
          s = s > 1 ? 1 : s < -1 ? -1 : s;
          const a = s < 0 ? -s : s;
          if (a > peak) peak = a;
          pcm[j] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage({ pcm: pcm.buffer, peak }, [pcm.buffer]);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
