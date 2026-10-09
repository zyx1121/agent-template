// Minimal Telegram Bot API client: plain fetch, no dependencies.

export type User = { id: number; is_bot?: boolean; first_name?: string; last_name?: string; username?: string };
export type Entity = { type: string; offset: number; length: number };

export type Message = {
  message_id: number;
  from?: User;
  chat: { id: number; type: string; title?: string };
  text?: string;
  caption?: string;
  entities?: Entity[];
  caption_entities?: Entity[];
  reply_to_message?: Message;
  quote?: { text: string };
  sticker?: FileRef;
  video_note?: FileRef;
  animation?: FileRef & { file_name?: string };
  photo?: { file_id: string; file_size?: number }[];
  document?: FileRef & { file_name?: string };
  video?: FileRef;
  audio?: FileRef & { file_name?: string };
  voice?: FileRef & { duration?: number };
  reply_markup?: { inline_keyboard?: { text: string; callback_data?: string }[][] };
};

type FileRef = { file_id: string; file_size?: number; mime_type?: string };


export type Update = {
  update_id: number;
  message?: Message;
  stopped_message_generation?: { chat: { id: number }; message_thread_id?: number; draft_id: number };
};

export const MAX_TEXT = 4000;

export class Telegram {
  constructor(private token: string) {}

  async call<T = any>(method: string, body: Record<string, unknown> = {}): Promise<T> {
    const res = await fetch(`https://api.telegram.org/bot${this.token}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await res.json()) as { ok: boolean; result: T; description?: string };
    if (!data.ok) throw new Error(`${method}: ${data.description}`);
    return data.result;
  }

  send(chat: number, thread: number | undefined, text: string, extra: Record<string, unknown> = {}) {
    return this.call<Message>("sendMessage", {
      chat_id: chat,
      message_thread_id: thread,
      text,
      link_preview_options: { is_disabled: true },
      ...extra,
    });
  }

  async sendLong(chat: number, thread: number | undefined, text: string, replyTo?: number) {
    for (const part of chunk(text || "(empty)")) await this.send(chat, thread, part, quote(replyTo));
  }

  /** Send Markdown as a Rich Message (real headings, lists, code blocks); falls back to plain text. */
  async sendMarkdown(chat: number, thread: number | undefined, markdown: string, replyTo?: number) {
    for (const part of chunkMarkdown(markdown || "(empty)")) {
      await this.call("sendRichMessage", {
        chat_id: chat,
        message_thread_id: thread,
        rich_message: { markdown: part },
        ...quote(replyTo),
      }).catch((e) => {
        console.error(String(e));
        return this.send(chat, thread, part, quote(replyTo));
      });
    }
  }

  edit(chat: number, message: number, text: string, extra: Record<string, unknown> = {}) {
    return this.call("editMessageText", { chat_id: chat, message_id: message, text, ...extra }).catch((e) => {
      // Editing to identical text is a harmless no-op; anything else is worth a log line.
      if (!String(e).includes("not modified")) console.error(e);
    });
  }

  /** Download a file the user sent (Bot API limit: 20 MB) to `dest`. */
  async download(fileId: string, dest: string) {
    const file = await this.call<{ file_path: string }>("getFile", { file_id: fileId });
    const res = await fetch(`https://api.telegram.org/file/bot${this.token}/${file.file_path}`);
    if (!res.ok) throw new Error(`download failed: ${res.status}`);
    await Bun.write(dest, res);
    return file.file_path;
  }

  /** Set (or clear, with no emoji) the bot's reaction on a message; failures are harmless. */
  react(chat: number, message: number, emoji?: string) {
    return this.call("setMessageReaction", {
      chat_id: chat,
      message_id: message,
      reaction: emoji ? [{ type: "emoji", emoji }] : [],
    }).catch(() => {});
  }

  /** Send a file from disk: images as photos (shown inline), everything else as a document. */
  async sendFile(chat: number, thread: number | undefined, path: string) {
    const file = Bun.file(path);
    const name = path.split("/").pop()!;
    const photo = /\.(png|jpe?g|webp)$/i.test(name) && file.size < 10_000_000;
    const form = new FormData();
    form.set("chat_id", String(chat));
    if (thread) form.set("message_thread_id", String(thread));
    form.set(photo ? "photo" : "document", file, name);
    const res = await fetch(`https://api.telegram.org/bot${this.token}/${photo ? "sendPhoto" : "sendDocument"}`, {
      method: "POST",
      body: form,
    });
    const data = (await res.json()) as { ok: boolean; description?: string };
    if (!data.ok) throw new Error(`send ${name}: ${data.description}`);
  }

  async sendDocument(chat: number, thread: number | undefined, name: string, content: string) {
    const form = new FormData();
    form.set("chat_id", String(chat));
    if (thread) form.set("message_thread_id", String(thread));
    form.set("document", new Blob([content], { type: "text/plain" }), name);
    await fetch(`https://api.telegram.org/bot${this.token}/sendDocument`, { method: "POST", body: form });
  }
}

/** Split text into Telegram-sized parts, preferring newline boundaries. */
export function chunk(text: string, size = MAX_TEXT): string[] {
  const parts: string[] = [];
  let rest = text;
  while (rest.length > size) {
    let cut = rest.lastIndexOf("\n", size);
    if (cut < size / 2) cut = size;
    parts.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/, "");
  }
  if (rest) parts.push(rest);
  return parts;
}

/** Reply to the message that asked, so the answer quotes it instead of the topic's opening line. */
export function quote(messageId?: number) {
  return messageId ? { reply_parameters: { message_id: messageId, allow_sending_without_reply: true } } : {};
}

/** Like chunk, but a code fence cut in two is closed and reopened so both parts render. */
export function chunkMarkdown(text: string, size = MAX_TEXT - 16): string[] {
  const parts = chunk(text, size);
  let open = "";
  return parts.map((part) => {
    let out = open ? `${open}\n${part}` : part;
    const fences = part.match(/^```.*$/gm) ?? [];
    let inside = !!open;
    let lang = open;
    for (const f of fences) {
      inside = !inside;
      lang = inside ? f : "";
    }
    open = inside ? lang : "";
    if (inside) out += "\n```";
    return out;
  });
}

export function escapeHtml(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** "/new@my_bot" -> { cmd: "new", args: "" }; null for non-commands. */
export function parseCommand(text: string): { cmd: string; args: string } | null {
  const m = text.match(/^\/([A-Za-z0-9_-]+)(?:@\w+)?(?:\s+([\s\S]*))?$/);
  return m ? { cmd: m[1], args: (m[2] ?? "").trim() } : null;
}

/** Drop the "@bot" suffix Telegram appends to commands picked from the menu in groups. */
export function stripBotSuffix(text: string) {
  return text.replace(/^(\/[A-Za-z0-9_:-]+)@\w+/, "$1");
}
