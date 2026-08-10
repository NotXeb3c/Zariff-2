import type { Message } from "../stores/session";

export const CHAT_SESSIONS_KEY = "heliox_chat_sessions_v1";
export const ACTIVE_CHAT_SESSION_KEY = "heliox_active_chat_session";
export const LEGACY_CHAT_HISTORY_KEY = "heliox_session_history";

export interface ChatSessionRecord {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: Message[];
  totalTokens: number;
  estimatedCost: number;
  durableTask?: DurableTaskReference;
}

export interface DurableTaskReference {
  taskId: string;
  resumeToken: string;
}

export interface ChatSessionSummary {
  id: string;
  title: string;
  preview: string;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
}

export interface ChatSessionCollection {
  sessions: ChatSessionRecord[];
  activeSessionId: string;
}

function makeId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `chat-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createChatSession(now = Date.now()): ChatSessionRecord {
  return {
    id: makeId(),
    title: "New chat",
    createdAt: now,
    updatedAt: now,
    messages: [],
    totalTokens: 0,
    estimatedCost: 0,
    durableTask: undefined,
  };
}

export function deriveChatTitle(messages: Message[]): string {
  const firstUserMessage = messages.find((message) => message.type === "user")?.text.trim();
  if (!firstUserMessage) return "New chat";
  const singleLine = firstUserMessage.replace(/\s+/g, " ");
  return singleLine.length > 56 ? `${singleLine.slice(0, 53)}...` : singleLine;
}

export function redactSensitiveChatText(text: string): string {
  return text
    .replace(/([?&](?:key|api[_-]?key|token)=)[^&\s"'<>]+/gi, "$1[redacted]")
    .replace(/\bAIza[0-9A-Za-z_-]{20,}\b/g, "[redacted]")
    .replace(/(Bearer\s+)[0-9A-Za-z._~-]+/gi, "$1[redacted]");
}

function parseMessages(value: unknown): Message[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((message): message is Message =>
      Boolean(
        message &&
        typeof message === "object" &&
        typeof (message as Message).type === "string" &&
        typeof (message as Message).text === "string" &&
        typeof (message as Message).timestamp === "number",
      ),
    )
    .map((message) => ({
      ...message,
      text: redactSensitiveChatText(message.text),
      actionResults: message.actionResults?.map((result) => ({
        ...result,
        output: redactSensitiveChatText(result.output),
        error: result.error ? redactSensitiveChatText(result.error) : null,
      })),
    }));
}

function parseRecord(value: unknown): ChatSessionRecord | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<ChatSessionRecord>;
  if (typeof raw.id !== "string" || !raw.id) return null;
  const messages = parseMessages(raw.messages);
  const createdAt = Number(raw.createdAt) || Date.now();
  return {
    id: raw.id,
    title: typeof raw.title === "string" && raw.title.trim() ? raw.title : deriveChatTitle(messages),
    createdAt,
    updatedAt: Number(raw.updatedAt) || createdAt,
    messages,
    totalTokens: Math.max(0, Number(raw.totalTokens) || 0),
    estimatedCost: Math.max(0, Number(raw.estimatedCost) || 0),
    durableTask:
      raw.durableTask &&
      typeof raw.durableTask.taskId === "string" &&
      raw.durableTask.taskId &&
      typeof raw.durableTask.resumeToken === "string" &&
      raw.durableTask.resumeToken
        ? {
            taskId: raw.durableTask.taskId,
            resumeToken: raw.durableTask.resumeToken,
          }
        : undefined,
  };
}

export function loadChatSessions(storage: Storage | null): ChatSessionCollection {
  if (!storage) {
    const session = createChatSession();
    return { sessions: [session], activeSessionId: session.id };
  }

  try {
    const parsed = JSON.parse(storage.getItem(CHAT_SESSIONS_KEY) ?? "[]");
    const sessions = Array.isArray(parsed)
      ? parsed.map(parseRecord).filter((session): session is ChatSessionRecord => session !== null)
      : [];
    if (sessions.length > 0) {
      storage.setItem(CHAT_SESSIONS_KEY, JSON.stringify(sessions));
      const requestedActiveId = storage.getItem(ACTIVE_CHAT_SESSION_KEY);
      const activeSessionId = sessions.some((session) => session.id === requestedActiveId)
        ? String(requestedActiveId)
        : sessions[0].id;
      return { sessions, activeSessionId };
    }
  } catch {
    // Fall through to the legacy migration/new chat path.
  }

  let legacyMessages: Message[] = [];
  try {
    legacyMessages = parseMessages(JSON.parse(storage.getItem(LEGACY_CHAT_HISTORY_KEY) ?? "[]"));
  } catch {
    legacyMessages = [];
  }

  const migrated = createChatSession();
  migrated.messages = legacyMessages;
  migrated.title = deriveChatTitle(legacyMessages);
  const lastMessage = legacyMessages.at(-1);
  if (lastMessage) {
    migrated.createdAt = legacyMessages[0]?.timestamp ?? migrated.createdAt;
    migrated.updatedAt = lastMessage.timestamp;
  }
  return { sessions: [migrated], activeSessionId: migrated.id };
}

export function saveChatSessions(
  storage: Storage | null,
  sessions: ChatSessionRecord[],
  activeSessionId: string,
): void {
  if (!storage) return;
  storage.setItem(CHAT_SESSIONS_KEY, JSON.stringify(sessions));
  storage.setItem(ACTIVE_CHAT_SESSION_KEY, activeSessionId);
}

export function summarizeChatSessions(sessions: ChatSessionRecord[]): ChatSessionSummary[] {
  return [...sessions]
    .sort((left, right) => right.updatedAt - left.updatedAt)
    .map((session) => {
      const previewMessage = [...session.messages]
        .reverse()
        .find((message) => message.type === "user" || message.type === "result" || message.type === "assistant");
      return {
        id: session.id,
        title: session.title,
        preview: previewMessage?.text.replace(/\s+/g, " ").slice(0, 90) ?? "No messages yet",
        createdAt: session.createdAt,
        updatedAt: session.updatedAt,
        messageCount: session.messages.length,
      };
    });
}
