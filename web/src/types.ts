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

export type LinkState =
  | "idle"
  | "starting"
  | "live"
  | "paused"
  | "reconnecting"
  // 已经发了停止，正在等服务端把语音尾句收完并落库。这段时间里连接还开着
  | "settling"
  | "error";

export type Term = { en: string; zh: string; variants?: string[] };

export type Party = { en: string; zh: string; role: string };

export type StoredDoc = { name: string; chars: number };

export type ContextInfo = {
  meetingId: number | null;
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

// 使用者本人填的会前交代。程序不替他猜身份，没填就什么都不假设。
export type Profile = {
  identity: string;
  goal: string;
  facts: string;
  noCommit: string;
};

export type Directive = { text: string; savedAt: number };

export type MeetingSummary = {
  id: number;
  title: string;
  startedAt: number;
  turns: number;
  docCount: number;
  hasProfile: boolean;
  hasMinutes: boolean;
  active: boolean;
};

// 一条依据：模型给的编号与摘录，服务端拿原文核对过
export type EvidenceItem = {
  tag: string;
  quote: string;
  note: string;
  doc?: string;
  // 只有核不过的条目才有，写明为什么核不过
  reason?: string;
};

export type Evidence = {
  verified: EvidenceItem[];
  pending: EvidenceItem[];
  todo: string[];
};

export type Draft = {
  id: number;
  instruction: string;
  en: string;
  zh: string;
  done: boolean;
  error?: string;
  refined?: boolean;
  evidence?: Evidence;
  // 段落收口后系统自动写的建议回复，同一张卡原地更新，区别于我主动要的
  auto?: boolean;
  // 这张自动卡回应的是哪一句。卡片停在旧问题上时一眼能看出来
  answering?: string;
};
