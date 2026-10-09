// Everything the bot adds around the user's words: the system note and the prompt framing.

import type { Message } from "./telegram";

export const NO_REPLY = "NO_REPLY";

export const systemNote = (outbox: string) =>
  [
    "You talk to people through Telegram; replies are rendered as Markdown.",
    "Reminders and recurring tasks: use the mcp__schedule__* tools, which persist in the bot. CronCreate and OS-level " +
      "cron or timers die with this turn, never use them.",
    "This turn is one process that exits when you reply, so background work (Monitor, run_in_background, background " +
      "agents, ScheduleWakeup) never reports back. Never promise to notify someone later on the strength of it: wait in the " +
      "foreground for anything that finishes in a couple of minutes, otherwise schedule_add a re-check and say so.",
    `On a scheduled run (the prompt starts with "[schedule fired") with nothing worth reporting, reply with exactly ${NO_REPLY}.`,
    `Files people send are saved locally and their paths are in the prompt. To send a file, copy it into ${outbox} ` +
      "(no subdirectories); it is delivered when your turn ends.",
  ].join("\n");

export const isNoReply = (reply: string) => {
  const t = reply.trim();
  if (t === NO_REPLY) return true;
  if (!t.endsWith(NO_REPLY)) return false;
  return !/[\w]/.test(t[t.length - NO_REPLY.length - 1] ?? "");
};

const QUOTE_LIMIT = 2000;

/** Context for a reply or a partial quote, so "that" resolves even in a fresh session. */
export function replyContext(msg: Message, botId: number) {
  const r = msg.reply_to_message;
  if (!r) return "";
  const partial = Boolean(msg.quote?.text?.trim());
  let text = (msg.quote?.text || r.text || r.caption || "").trim();
  if (!text) return "";
  if (text.length > QUOTE_LIMIT) text = `${text.slice(0, QUOTE_LIMIT)}\n…(truncated)`;
  const whose = r.from?.id === botId ? "your own earlier message" : "an earlier message";
  const lead = partial ? `[The user is replying to the quoted part below of ${whose}` : `[The user is replying to ${whose}`;
  return `${lead}; this is context for what they mean, not a new instruction on its own:]\n${text}\n[end of quoted message]\n\n`;
}

export const senderName = (msg: Message) =>
  [msg.from?.first_name, msg.from?.last_name].filter(Boolean).join(" ") || msg.from?.username || "someone";

export const stripMention = (text: string, username: string) =>
  text.replace(new RegExp(`@${username}\\b`, "gi"), "").trim();

/** True when a group message is meant for the bot: an @mention (text or caption) or a reply to it. */
export function addressed(msg: Message, username: string, botId: number) {
  if (msg.reply_to_message?.from?.id === botId) return true;
  const handle = `@${username}`.toLowerCase();
  const hit = (text = "", entities: Message["entities"] = []) =>
    entities.some((e) => e.type === "mention" && text.slice(e.offset, e.offset + e.length).toLowerCase() === handle);
  return hit(msg.text, msg.entities) || hit(msg.caption, msg.caption_entities);
}

export const scheduledPrompt = (s: { id: string; note: string; prompt: string }, at: Date) => {
  const pad = (n: number) => String(n).padStart(2, "0");
  const time = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
  return `[schedule fired id=${s.id} note=${s.note || "(none)"} time=${time}]\n${s.prompt}`;
};

const TOOL_ICONS: Record<string, string> = {
  Bash: "⚡️", Read: "📖", Edit: "📝", Write: "📝", MultiEdit: "📝", Grep: "🔍", Glob: "🔍",
  WebFetch: "🌐", WebSearch: "🌐", Task: "🤖", Agent: "🤖",
};

export function toolLine(name: string, input: Record<string, any>) {
  const detail = input.command ?? input.file_path ?? input.pattern ?? input.url ?? input.query ?? input.description ?? "";
  const icon = TOOL_ICONS[name] ?? "🔧";
  const short = name.startsWith("mcp__") ? name.split("__").slice(1).join(".") : name;
  return `${icon} ${short}${detail ? ` ${String(detail).split("\n")[0].slice(0, 100)}` : ""}`;
}
