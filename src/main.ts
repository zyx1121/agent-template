// agent-template: a Telegram bot whose every turn is a headless Claude Code run.
// Persona in SOUL.md, one rolling session per chat, persistent schedules, files both ways.

import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { isStaleSession, runTurn, TurnError } from "./claude";
import { cronMatches } from "./cron";
import { Draft } from "./draft";
import * as otel from "./otel";
import { addressed, isNoReply, replyContext, scheduledPrompt, senderName, stripMention, systemNote, toolLine } from "./prompts";
import { listSchedules, removeSchedule, type Schedule } from "./schedules";
import { parseCommand, quote, Telegram, type Message, type Update } from "./telegram";

// ---------- configuration ----------

const env = (k: string, d = "") => process.env[k] || d;
const HOME = resolve(env("AGENT_HOME", process.cwd()));
loadDotEnv(join(HOME, ".env"));

const TOKEN = env("TELEGRAM_BOT_TOKEN");
const OWNER = Number(env("OWNER_USER_ID"));
const NAME = env("AGENT_NAME", "Agent");
const CLAUDE = env("CLAUDE_BIN", Bun.which("claude") ?? join(homedir(), ".local/bin/claude"));
const MODEL = env("AGENT_MODEL");
const EFFORT = env("AGENT_EFFORT");
if (EFFORT && !["low", "medium", "high", "xhigh", "max"].includes(EFFORT)) await stuck(`AGENT_EFFORT=${EFFORT} is not one of low, medium, high, xhigh, max.`);
const TIMEOUT_MS = Number(env("AGENT_TURN_TIMEOUT", "1800")) * 1000;
const GROUPS = new Set(env("ALLOWED_GROUP_IDS").split(",").map((s) => s.trim()).filter(Boolean).map(Number));
const RUN = join(HOME, "run");
const OUTBOX = join(RUN, "outbox");
const INBOX = join(RUN, "telegram");
const SCHEDULES = join(RUN, "schedules.json");
const sessionFile = (chat: number) => join(RUN, `session-${chat}`);

/** A setup problem: say it once and wait, so a restart policy does not loop on the same line. */
async function stuck(reason: string): Promise<never> {
  console.error(`${reason} Fix the configuration and restart.`);
  return new Promise<never>(() => {});
}

if (!TOKEN || !OWNER) await stuck("TELEGRAM_BOT_TOKEN and OWNER_USER_ID must be set (see .env.example).");
for (const d of [RUN, OUTBOX, INBOX]) mkdirSync(d, { recursive: true });
redactSecrets([TOKEN, env("CLAUDE_CODE_OAUTH_TOKEN"), env("SENSORIUM_TOKEN")]);

const SOUL = existsSync(join(HOME, "SOUL.md")) ? readFileSync(join(HOME, "SOUL.md"), "utf8") : "";
const SYSTEM = [SOUL.trim(), systemNote(OUTBOX)].filter(Boolean).join("\n\n");
const tg = new Telegram(TOKEN);
const me = await tg.call<{ id: number; username: string }>("getMe").catch((e) => stuck(`Telegram rejected TELEGRAM_BOT_TOKEN: ${e.message}.`));

function loadDotEnv(path: string) {
  if (!existsSync(path)) return;
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^(['"])(.*)\1$/, "$2");
  }
}

/** Last line of defence: no log line may carry a token, whatever threw it. */
function redactSecrets(secrets: string[]) {
  const keep = secrets.filter((s) => s.length >= 8);
  for (const level of ["log", "warn", "error"] as const) {
    const original = console[level].bind(console);
    console[level] = (...args: unknown[]) =>
      original(...args.map((a) => keep.reduce((s, k) => s.split(k).join("<redacted>"), a instanceof Error ? a.stack ?? a.message : String(a))));
  }
}

// ---------- mcp config: the builtin schedule server plus the fork's own ----------

function mcpConfig(chat: number) {
  const servers: Record<string, unknown> = {};
  const extra = join(HOME, "mcp-config.json");
  if (existsSync(extra)) {
    const own = JSON.parse(readFileSync(extra, "utf8")).mcpServers ?? {};
    if (own.schedule) console.warn("mcp-config.json: a 'schedule' server is ignored, the builtin one wins");
    Object.assign(servers, own);
  }
  servers.schedule = {
    type: "stdio",
    command: process.execPath,
    args: [join(import.meta.dir, "mcp-schedule.ts")],
    env: { AGENT_SCHEDULES: SCHEDULES, AGENT_CHAT_ID: String(chat) },
  };
  const path = join(RUN, `mcp-runtime-${chat}.json`);
  writeFileSync(path, JSON.stringify({ mcpServers: servers }), { mode: 0o600 });
  return path;
}

// ---------- one turn, with its live view ----------

const ACKS = ["👍", "🔥", "🎉", "👀", "🤔", "🙏", "💯", "⚡", "🤩", "👌", "🫡", "✍", "🤝", "👏", "🤓"];
let queue: Promise<unknown> = Promise.resolve();
const stops = new Map<number, AbortController>(); // draft id -> its turn

type Turn = { chat: number; prompt: string; message?: number; scheduled?: boolean; isPrivate: boolean };

/** Turns run one at a time: a resumed session cannot run twice at once. */
function enqueue(t: Turn) {
  const task = queue.then(() => serve(t));
  queue = task.catch(() => {});
  return task;
}

async function serve(t: Turn) {
  const { chat, message, scheduled, isPrivate } = t;
  if (message) tg.react(chat, message, ACKS[Math.floor(Math.random() * ACKS.length)]);
  for (const f of readdirSync(OUTBOX)) rmSync(join(OUTBOX, f), { recursive: true, force: true });

  // Private chats stream an animated draft; groups get one progress message edited in place.
  const abort = new AbortController();
  const draft = isPrivate ? new Draft(tg, chat, undefined) : undefined;
  if (draft) stops.set(draft.id, abort);
  let steps: string[] = [];
  let text = "";
  let bubble: number | undefined;
  let bubbleBusy = false;
  const render = () => [text, steps.at(-1)].filter(Boolean).join("\n\n");
  const typing = setInterval(() => tg.call("sendChatAction", { chat_id: chat, action: "typing" }).catch(() => {}), 4000);
  tg.call("sendChatAction", { chat_id: chat, action: "typing" }).catch(() => {});
  draft?.show("");

  const updateBubble = async () => {
    if (bubbleBusy) return;
    bubbleBusy = true;
    const body = steps.slice(-12).join("\n");
    if (bubble) await tg.edit(chat, bubble, body);
    else bubble = (await tg.send(chat, undefined, body, { disable_notification: true }).catch(() => undefined))?.message_id;
    bubbleBusy = false;
  };

  const end = otel.span("agent.turn", { chat, scheduled: Boolean(scheduled), private: isPrivate });
  let outcome = "👎";
  try {
    const result = await turnWithResume(t, {
      onText: (s) => {
        text = s;
        draft?.show(render());
      },
      onTool: (name, input) => {
        steps.push(toolLine(name, input));
        if (draft) draft.show(render());
        else updateBubble();
      },
    }, abort.signal);
    draft?.end();
    if (scheduled && isNoReply(result.reply)) {
      if (bubble) await tg.call("deleteMessage", { chat_id: chat, message_id: bubble }).catch(() => {});
      end({ tools: steps.length, no_reply: true });
      outcome = "👍";
      return;
    }
    await tg.sendMarkdown(chat, undefined, result.reply || "(empty reply)", message);
    end({ tools: steps.length, reply_chars: result.reply.length });
    outcome = "👍";
  } catch (e) {
    draft?.end();
    const err = e instanceof TurnError ? e : new TurnError(String(e), "error");
    end({ tools: steps.length, category: err.category }, err.message);
    otel.log(`turn failed: ${err.message}`, { chat, category: err.category, scheduled: Boolean(scheduled) }, "ERROR");
    const [reply, quiet] = errorReply(err, Boolean(scheduled));
    if (!quiet) await tg.send(chat, undefined, reply, quote(message));
    else if (bubble) await tg.call("deleteMessage", { chat_id: chat, message_id: bubble }).catch(() => {});
  } finally {
    clearInterval(typing);
    if (draft) stops.delete(draft.id);
    await deliverOutbox(chat);
    if (message) tg.react(chat, message, outcome);
  }
}

async function turnWithResume(t: Turn, events: Parameters<typeof runTurn>[1], signal: AbortSignal) {
  const file = sessionFile(t.chat);
  const resume = existsSync(file) ? readFileSync(file, "utf8").trim() || undefined : undefined;
  const run = (sid?: string) =>
    runTurn(
      {
        claude: CLAUDE,
        cwd: HOME,
        prompt: t.prompt,
        systemPrompt: SYSTEM,
        mcpConfig: mcpConfig(t.chat),
        model: MODEL || undefined,
        effort: EFFORT || undefined,
        resume: sid,
        timeoutMs: TIMEOUT_MS,
        env: { AGENT_OUTBOX: OUTBOX },
        signal,
      },
      events,
    );
  let result;
  try {
    result = await run(resume);
  } catch (e) {
    if (!resume || !isStaleSession(e)) throw e;
    console.warn(`session ${resume} could not be resumed, starting fresh`);
    result = await run(undefined);
  }
  if (result.sessionId) writeFileSync(file, result.sessionId);
  return result;
}

function errorReply(err: TurnError, scheduled: boolean): [string, boolean] {
  if (err.category === "usage_limit") return [`🕐 ${err.message}`, scheduled];
  if (err.category === "transient") return [`⏳ Claude is temporarily unavailable, try again shortly.\n(${err.message})`, scheduled];
  if (err.category === "auth") return [`🔑 Claude authentication failed; the token likely needs refreshing.\n(${err.message})`, false];
  return [`⚠️ claude failed: ${err.message}`, false];
}

async function deliverOutbox(chat: number) {
  for (const name of readdirSync(OUTBOX)) {
    const path = join(OUTBOX, name);
    if (!statSync(path).isFile()) continue;
    await tg.sendFile(chat, undefined, path).then(
      () => unlinkSync(path),
      (e) => tg.send(chat, undefined, `Could not send ${name}: ${e.message ?? e} (kept on the host)`),
    );
  }
}

// ---------- incoming messages ----------

async function saveAttachment(msg: Message) {
  const f = msg.document ?? msg.video ?? msg.audio ?? msg.voice ?? msg.video_note ?? msg.animation ?? msg.photo?.at(-1);
  if (!f) return;
  if ((f.file_size ?? 0) > 20 * 1024 * 1024) throw new Error("Telegram only lets bots download files up to 20 MB.");
  const kind = msg.document ? "document" : msg.photo ? "photo" : msg.voice ? "voice" : msg.audio ? "audio" : "video";
  const dir = join(INBOX, String(msg.chat.id));
  mkdirSync(dir, { recursive: true });
  const original = msg.document?.file_name ?? msg.audio?.file_name ?? msg.animation?.file_name;
  const tmp = join(dir, `${msg.message_id}`);
  const remote = await tg.download(f.file_id, tmp);
  const ext = remote.includes(".") ? `.${remote.split(".").pop()}` : "";
  const path = join(dir, original ? `${msg.message_id}-${original.replace(/[^\w.-]+/g, "_")}` : `${msg.message_id}${ext}`);
  Bun.spawnSync(["mv", tmp, path]);
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
  if (cmd?.cmd === "start") return tg.send(chat, undefined, `${NAME} is up. Send a message to start; /new starts a fresh conversation.`);
  if (cmd?.cmd === "new") {
    rmSync(sessionFile(chat), { force: true });
    return tg.send(chat, undefined, "Fresh conversation: the next message starts a new session.");
  }

  let attachment;
  try {
    attachment = await saveAttachment(msg);
  } catch (e) {
    return tg.send(chat, undefined, `⚠️ ${(e as Error).message}`, quote(msg.message_id));
  }
  let text = isPrivate ? raw.trim() : stripMention(raw, me.username);
  if (!isPrivate && text) text = `[${senderName(msg)}]: ${text}`;
  if (attachment) {
    text =
      `${text}\n\n--- attachment (${attachment.kind}) ---\n${attachment.path} (${attachment.kb} KB)\n` +
      "(Saved at the path above; open it with your tools.)";
  }
  if (!text.trim()) return;
  otel.log("message", { chat, private: isPrivate, attachment: attachment?.kind });
  await enqueue({ chat, prompt: replyContext(msg, me.id) + text.trim(), message: msg.message_id, isPrivate });
}

// ---------- schedules: a tick every minute, small delays caught up (5 minutes at most) ----------

let lastMinute = Math.floor(Date.now() / 60_000);

function tick() {
  const nowMinute = Math.floor(Date.now() / 60_000);
  const from = Math.max(lastMinute + 1, nowMinute - 4);
  lastMinute = nowMinute;
  for (let m = from; m <= nowMinute; m++) {
    const at = new Date(m * 60_000);
    for (const s of listSchedules(SCHEDULES)) {
      if (!s.enabled) continue;
      let due = false;
      try {
        due = cronMatches(s.cron, at);
      } catch (e) {
        console.warn(`schedule ${s.id}: ${(e as Error).message}`);
      }
      if (due) fire(s, at);
    }
  }
}

function fire(s: Schedule, at: Date) {
  if (s.once) removeSchedule(SCHEDULES, s.id);
  otel.log("schedule fired", { id: s.id, chat: s.chat_id });
  enqueue({ chat: s.chat_id, prompt: scheduledPrompt(s, at), scheduled: true, isPrivate: s.chat_id > 0 }).catch(console.error);
}

// Align to the start of each minute (plus a second of slack), then tick every minute.
setTimeout(() => {
  tick();
  setInterval(tick, 60_000);
}, 60_000 - (Date.now() % 60_000) + 1000);

// ---------- long polling ----------

async function poll() {
  let offset = 0;
  for (;;) {
    try {
      const updates = await tg.call<Update[]>("getUpdates", {
        offset,
        timeout: 30,
        allowed_updates: ["message", "stopped_message_generation"],
      });
      for (const u of updates) {
        offset = u.update_id + 1;
        const stop = u.stopped_message_generation;
        if (stop) stops.get(stop.draft_id)?.abort();
        if (u.message)
          onMessage(u.message).catch((e) => {
            console.error(e);
            otel.log(`message failed: ${e.message ?? e}`, {}, "ERROR");
          });
      }
    } catch (e) {
      console.error(e);
      otel.log(`poll failed: ${e}`, {}, "WARN");
      await Bun.sleep(3000);
    }
  }
}

await tg.call("setMyCommands", {
  commands: [
    { command: "new", description: "Start a fresh conversation" },
    { command: "start", description: "Check that the bot is up" },
  ],
}).catch(console.error);
console.log(`${NAME} up as @${me.username}: owner ${OWNER}, groups [${[...GROUPS].join(", ")}], home ${HOME}`);
otel.log("agent up", { bot: me.username, groups: GROUPS.size });
poll();
