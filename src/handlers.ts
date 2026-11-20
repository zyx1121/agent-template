// The Telegram message and schedule handlers, behind a factory that takes its I/O so a test can drive them.

import { randomBytes } from "node:crypto";
import { chmodSync, closeSync, lstatSync, mkdirSync, openSync, renameSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { routeMessage, routeSchedule, senderLabel } from "./access";
import { addressed, replyContext, scheduledPrompt, stripMention } from "./prompts";
import type { Schedule } from "./schedules";
import { parseCommand, quote, type Message, type Telegram } from "./telegram";

/** owner: the turn acts for OWNER_USER_ID (a private chat, or the owner's own message in a group). */
export type Turn = { chat: number; prompt: string; message?: number; scheduled?: boolean; isPrivate: boolean; owner: boolean };

// An owner turn in a group gets a new name too, so it never resumes a history that others wrote into.
export const sessionFile = (run: string, chat: number, owner: boolean) =>
  join(run, !owner ? `session-${chat}-guest` : chat > 0 ? `session-${chat}` : `session-${chat}-owner`);

export type HandlerDeps = {
  tg: Pick<Telegram, "send" | "download">;
  me: { id: number; username: string };
  OWNER: number;
  GROUPS: Set<number>;
  NAME: string;
  /** Whether a guest uid is configured; guest turns are refused or skipped without one. */
  GUEST: boolean;
  RUN: string;
  INBOX: string;
  GUEST_INBOX: string;
  /** Removes an owner once-entry. */
  removeSchedule(id: string): void;
  /** Remembers a fired guest once-entry. */
  markGuestFired(s: Schedule): void;
  enqueue(t: Turn): Promise<unknown>;
  /** Telemetry sink, `otel.log` in production. Injected so importing this module starts no exporter. */
  log(event: string, attrs?: Record<string, string | number | boolean | undefined>): void;
};

export function createHandlers(deps: HandlerDeps) {
  const { tg, me, OWNER, GROUPS, NAME, GUEST, RUN, INBOX, GUEST_INBOX, enqueue, log } = deps;

  async function saveAttachment(msg: Message, guest: boolean) {
    const f = msg.document ?? msg.video ?? msg.audio ?? msg.voice ?? msg.video_note ?? msg.animation ?? msg.photo?.at(-1);
    if (!f) return;
    if ((f.file_size ?? 0) > 20 * 1024 * 1024) throw new Error("Telegram only lets bots download files up to 20 MB.");
    const kind = msg.document ? "document" : msg.photo ? "photo" : msg.voice ? "voice" : msg.audio ? "audio" : "video";
    const dir = join(guest ? GUEST_INBOX : INBOX, String(msg.chat.id));
    mkdirSync(dir, { recursive: true });
    // A planted symlink or file at the chat dir must not make the bot chmod or write elsewhere.
    if (!lstatSync(dir).isDirectory()) throw new Error(`${dir} is not a directory.`);
    if (guest) chmodSync(dir, 0o755); // the guest reads it
    const original = msg.document?.file_name ?? msg.audio?.file_name ?? msg.animation?.file_name;
    // Claim a fresh unique name exclusively, so the download never writes through a planted link.
    const tmp = join(dir, `${msg.message_id}.${randomBytes(8).toString("hex")}.part`);
    closeSync(openSync(tmp, "wx", 0o600));
    let remote: string;
    try {
      remote = await tg.download(f.file_id, tmp);
    } catch (e) {
      rmSync(tmp, { force: true });
      throw e;
    }
    const ext = remote.includes(".") ? `.${remote.split(".").pop()}` : "";
    const path = join(dir, original ? `${msg.message_id}-${original.replace(/[^\w.-]+/g, "_")}` : `${msg.message_id}${ext}`);
    try {
      if (lstatSync(path, { throwIfNoEntry: false })?.isSymbolicLink()) throw new Error(`${path} is a symlink.`);
      renameSync(tmp, path);
    } catch (e) {
      rmSync(tmp, { force: true });
      throw e;
    }
    if (guest) chmodSync(path, 0o644);
    return { path, kind, kb: Math.round(statSync(path).size / 1024) };
  }

  async function onMessage(msg: Message) {
    const chat = msg.chat.id;
    const isPrivate = msg.chat.type === "private";
    const fromOwner = msg.from?.id === OWNER;
    if (isPrivate && !fromOwner) return console.log(`ignored private message from ${msg.from?.id}`);
    if (!isPrivate) {
      if (!addressed(msg, me.username, me.id)) return;
      if (!GROUPS.has(chat) && !fromOwner) {
        console.log(`ignored group ${chat} (${msg.chat.title}) from ${msg.from?.id}`);
        return tg.send(chat, undefined, `This group's id is ${chat}. Add it to ALLOWED_GROUP_IDS to let me in.`, quote(msg.message_id));
      }
    }

    const raw = msg.text ?? msg.caption ?? "";
    const cmd = parseCommand(raw);
    return routeMessage(fromOwner, GUEST, cmd?.cmd, {
      refuse: () => {
        console.log(`refused guest message in ${chat} from ${msg.from?.id}: AGENT_GUEST_UID is not set`);
        return tg.send(chat, undefined, "Sorry, I can't take messages from group members in this setup.", quote(msg.message_id));
      },
      start: () => tg.send(chat, undefined, `${NAME} is up. Send a message to start; /new starts a fresh conversation.`),
      fresh: () => {
        rmSync(sessionFile(RUN, chat, fromOwner), { force: true });
        return tg.send(chat, undefined, "Fresh conversation: the next message starts a new session.");
      },
      turn: () => turnFrom(msg, raw, isPrivate, fromOwner),
    });
  }

  async function turnFrom(msg: Message, raw: string, isPrivate: boolean, fromOwner: boolean) {
    const chat = msg.chat.id;
    let attachment;
    try {
      attachment = await saveAttachment(msg, !fromOwner);
    } catch (e) {
      return tg.send(chat, undefined, `⚠️ ${(e as Error).message}`, quote(msg.message_id));
    }
    let text = isPrivate ? raw.trim() : stripMention(raw, me.username);
    if (attachment) {
      text =
        `${text}\n\n--- attachment (${attachment.kind}) ---\n${attachment.path} (${attachment.kb} KB)\n` +
        "(Saved at the path above; open it with your tools.)";
    }
    if (!text.trim()) return;
    if (!isPrivate) text = `${senderLabel(msg, OWNER)}: ${text.trim()}`;
    log("message", { chat, private: isPrivate, attachment: attachment?.kind });
    await enqueue({ chat, prompt: replyContext(msg, me.id) + text.trim(), message: msg.message_id, isPrivate, owner: fromOwner });
  }

  function fire(s: Schedule, at: Date, fromGuest: boolean) {
    routeSchedule(s.chat_id, OWNER, s.once, fromGuest, GUEST, {
      skip: () => console.warn(`schedule ${s.id} skipped: AGENT_GUEST_UID is not set`),
      consume: () => (fromGuest ? deps.markGuestFired(s) : deps.removeSchedule(s.id)),
      run: (owner) => {
        log("schedule fired", { id: s.id, chat: s.chat_id });
        enqueue({ chat: s.chat_id, prompt: scheduledPrompt(s, at), scheduled: true, isPrivate: s.chat_id > 0, owner }).catch(console.error);
      },
    });
  }

  return { onMessage, fire };
}
