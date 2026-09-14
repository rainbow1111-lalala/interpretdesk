import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 同一个浏览器同时只录一场会。
 *
 * 两个标签页同时录，两条音频流会抢同一个断句器，落库的段落互相穿插，事后看笔录像两个人
 * 在抢话。真正的强制在服务端（它按会话拒掉第二条连接），这里只负责让另一个标签页立刻把
 * 按钮置灰并说清原因，不必等用户点下去才知道。
 *
 * 用 BroadcastChannel 而不是 localStorage 锁：标签页崩溃时 localStorage 里会留下一把
 * 过期的锁，用户被自己锁在门外还找不到解锁的地方；BroadcastChannel 的状态随标签页一起
 * 消失，不会留残骸。
 */
export function useSoleRecorder() {
  const [blocked, setBlocked] = useState(false);
  const chanRef = useRef<BroadcastChannel | null>(null);
  const miningRef = useRef(false);

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const chan = new BroadcastChannel("mi-recording");
    chanRef.current = chan;
    chan.onmessage = (ev) => {
      const kind = ev.data?.kind;
      if (kind === "claim" && !miningRef.current) setBlocked(true);
      else if (kind === "release") setBlocked(false);
      // 新开的标签页问一声「现在有人在录吗」，正在录的那个回一条 claim
      else if (kind === "who" && miningRef.current) chan.postMessage({ kind: "claim" });
    };
    chan.postMessage({ kind: "who" });
    const bye = () => {
      if (miningRef.current) chan.postMessage({ kind: "release" });
    };
    window.addEventListener("beforeunload", bye);
    return () => {
      bye();
      window.removeEventListener("beforeunload", bye);
      chan.close();
      chanRef.current = null;
    };
  }, []);

  const claim = useCallback(() => {
    miningRef.current = true;
    chanRef.current?.postMessage({ kind: "claim" });
  }, []);

  const release = useCallback(() => {
    if (!miningRef.current) return;
    miningRef.current = false;
    chanRef.current?.postMessage({ kind: "release" });
  }, []);

  return { blocked, claim, release };
}
