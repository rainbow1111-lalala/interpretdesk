export type Pair = { src: string; dst: string };

export type Entry = {
  turnId: number;
  pairs: Pair[];
  srcLang: string;
  echo: boolean;
  ts: number;
};

export type LiveEntry = {
  turnId: number;
  src: string;
  dst: string;
  srcLang: string;
  echo: boolean;
};

export type LinkState = "idle" | "starting" | "live" | "paused" | "reconnecting" | "error";

export type Term = { en: string; zh: string; variants?: string[] };

export type Party = { en: string; zh: string; role: string };

export type StoredDoc = { name: string; chars: number };

export type ContextInfo = {
  matter: string;
  parties: Party[];
  issues: string[];
  terms: Term[];
  myPosition: string;
  sources: string[];
  glossarySize: number;
  docs: StoredDoc[];
  indexChunks: number;
};

export type Draft = {
  id: number;
  instruction: string;
  en: string;
  zh: string;
  done: boolean;
  error?: string;
  refined?: boolean;
  // 段落收口后系统自动写的建议回复，同一张卡原地更新，区别于我主动要的
  auto?: boolean;
};
