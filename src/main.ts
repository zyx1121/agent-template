// agent-template: a Telegram bot whose every turn is a headless Claude Code run.
// Persona in SOUL.md, one rolling session per chat, persistent schedules, files both ways.

import { chmodSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { parseOwnerOnly, turnServers } from "./access";
import { guestEnv, isStaleSession, runTurn, stopUser, TurnError } from "./claude";
import { cronMatches } from "./cron";
import { Draft } from "./draft";
import * as otel from "./otel";
import { createHandlers, sessionFile, type Turn } from "./handlers";
import { clearOutbox } from "./outbox";
import { isNoReply, systemNote, toolLine } from "./prompts";
import { guestKey, guestSchedules, listSchedules, readRegular, removeSchedule, type Schedule } from "./schedules";
import { quote, Telegram, type Update } from "./telegram";

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
const UPDATE_HOURS = Number(env("CLAUDE_UPDATE_HOURS", "24"));
const GROUPS = new Set(env("ALLOWED_GROUP_IDS").split(",").map((s) => s.trim()).filter(Boolean).map(Number));
const OWNER_ONLY_MCP = parseOwnerOnly(env("AGENT_OWNER_ONLY_MCP"));
// Everything the bot writes is private to its uid; the image's entrypoint closes run/ to the guest uid.
process.umask(0o077);
const RUN = join(HOME, "run");
const OUTBOX = join(RUN, "outbox");
const INBOX = join(RUN, "telegram");
const SCHEDULES = join(RUN, "schedules.json");
// Turns that do not act for the owner run as the guest uid (AGENT_GUEST_UID, set by the image's
// entrypoint). The only part of run/ they can touch is run/guest/; what they write there is data.
const GUEST_UID = Number(env("AGENT_GUEST_UID"));
const GUEST = GUEST_UID ? { uid: GUEST_UID, gid: Number(env("AGENT_GUEST_GID", String(GUEST_UID))) } : undefined;
const GUEST_DIR = join(RUN, "guest");
const GUEST_OUTBOX = join(GUEST_DIR, "outbox");
const GUEST_INBOX = join(GUEST_DIR, "telegram");
const GUEST_CONFIG = join(GUEST_DIR, "config");
const GUEST_WORK = join(GUEST_DIR, "work");
const GUEST_MCP = join(GUEST_DIR, "mcp");
const GUEST_SCHEDULES = join(GUEST_DIR, "sched", "schedules.json");
// Guest once-schedules that already fired. Owner state: the bot never writes into a directory a guest can write.
const GUEST_FIRED = join(RUN, "guest-fired.json");
/** A setup problem: say it once and wait, so a restart policy does not loop on the same line. */
async function stuck(reason: string): Promise<never> {
  console.error(`${reason} Fix the configuration and restart.`);
  return new Promise<never>(() => {});
}

if (!TOKEN || !OWNER) await stuck("TELEGRAM_BOT_TOKEN and OWNER_USER_ID must be set (see .env.example).");
for (const d of [RUN, OUTBOX, INBOX, GUEST_OUTBOX, GUEST_INBOX, GUEST_CONFIG, GUEST_WORK, GUEST_MCP, dirname(GUEST_SCHEDULES)]) mkdirSync(d, { recursive: true });
if (!GUEST) console.warn("AGENT_GUEST_UID is not set: guest turns would share the bot's uid, so they are refused (group members) or skipped (group schedules). Owner turns still run.");
redactSecrets([TOKEN, env("CLAUDE_CODE_OAUTH_TOKEN"), env("SENSORIUM_TOKEN")]);

const SOUL = existsSync(join(HOME, "SOUL.md")) ? readFileSync(join(HOME, "SOUL.md"), "utf8") : "";
const systemFor = (outbox: string) => [SOUL.trim(), systemNote(outbox)].filter(Boolean).join("\n\n");
const SYSTEM = systemFor(OUTBOX);
const GUEST_SYSTEM = systemFor(GUEST_OUTBOX);
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

function mcpConfig(chat: number, owner: boolean) {
  const servers: Record<string, unknown> = {};
  const extra = join(HOME, "mcp-config.json");
  if (existsSync(extra)) {
    const own = JSON.parse(readFileSync(extra, "utf8")).mcpServers ?? {};
    if (own.schedule) console.warn("mcp-config.json: a 'schedule' server is ignored, the builtin one wins");
    Object.assign(servers, turnServers(own, OWNER_ONLY_MCP, owner));
  }
  servers.schedule = {
    type: "stdio",
    command: process.execPath,
    args: [join(import.meta.dir, "mcp-schedule.ts")],
    // Only the owner may see or change schedules of other chats.
    env: { AGENT_SCHEDULES: owner ? SCHEDULES : GUEST_SCHEDULES, AGENT_CHAT_ID: String(chat), AGENT_SCHEDULE_SCOPE: owner ? "all" : "chat" },
  };
  // The guest's claude reads its config itself, so it lives where the guest can.
  const path = owner ? join(RUN, `mcp-runtime-${chat}.json`) : join(GUEST_MCP, `mcp-runtime-${chat}.json`);
  // Drop whatever sits at the path and refuse to follow a link planted there ("wx" fails on an existing entry).
  rmSync(path, { force: true });
  writeFileSync(path, JSON.stringify({ mcpServers: servers }), { flag: "wx" });
  chmodSync(path, owner ? 0o600 : 0o644);
  return path;
}

// ---------- one turn, with its live view ----------

const ACKS = ["👍", "🔥", "🎉", "👀", "🤔", "🙏", "💯", "⚡", "🤩", "👌", "🫡", "✍", "🤝", "👏", "🤓"];
let queue: Promise<unknown> = Promise.resolve();
const stops = new Map<number, AbortController>(); // draft id -> its turn

/** Turns run one at a time: a resumed session cannot run twice at once. */
function enqueue(t: Turn) {
  const task = queue.then(() => serve(t));
  queue = task.catch(() => {});
  return task;
}

const { onMessage, fire } = createHandlers({
  tg,
  me,
  OWNER,
  GROUPS,
  NAME,
  GUEST: !!GUEST,
  RUN,
  INBOX,
  GUEST_INBOX,
  removeSchedule: (id) => removeSchedule(SCHEDULES, id),
  markGuestFired,
  enqueue,
  log: otel.log,
});

// ---------- Claude Code self-update ----------

// `claude -p` never updates itself, so the bot runs `claude update` between turns: at start and
// every CLAUDE_UPDATE_HOURS. The next turn spawns the new binary. A recreated container starts
// from the image's version again and catches up on its first check.
function updateClaude() {
  queue = queue.then(async () => {
    // umask 022 so the new binary stays executable for the guest uid.
    const p = Bun.spawn(["sh", "-c", 'umask 022; exec "$0" update', CLAUDE], { stdout: "pipe", stderr: "pipe" });
    const timer = setTimeout(() => p.kill(), 5 * 60_000);
    const [code, out, err] = await Promise.all([p.exited, new Response(p.stdout).text(), new Response(p.stderr).text()]);
    clearTimeout(timer);
    const line = (out + err).trim().split("\n").filter(Boolean).at(-1) ?? "";
    if (code === 0) console.log(`claude update: ${line}`);
    else console.warn(`claude update exited ${code}: ${line}`);
    otel.log(`claude update: ${line}`, { code }, code === 0 ? "INFO" : "WARN");
  }).catch(console.error);
}

async function serve(t: Turn) {
  const { chat, message, scheduled, isPrivate } = t;
  if (message) tg.react(chat, message, ACKS[Math.floor(Math.random() * ACKS.length)]);
  const outbox = t.owner ? OUTBOX : GUEST_OUTBOX;
  clearOutbox(outbox, (m) => {
    console.warn(m);
    otel.log(m, {}, "WARN");
  });

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
    await deliverOutbox(chat, outbox, !t.owner);
    if (message) tg.react(chat, message, outcome);
  }
}

function guestTurnEnv() {
  const own = { AGENT_OUTBOX: GUEST_OUTBOX, CLAUDE_CONFIG_DIR: GUEST_CONFIG };
  return GUEST ? guestEnv(process.env, { ...own, HOME: env("AGENT_GUEST_HOME", "/home/guest") }) : own;
}

async function turnWithResume(t: Turn, events: Parameters<typeof runTurn>[1], signal: AbortSignal) {
  const file = sessionFile(RUN, t.chat, t.owner);
  const resume = existsSync(file) ? readFileSync(file, "utf8").trim() || undefined : undefined;
  const run = (sid?: string) =>
    runTurn(
      {
        claude: CLAUDE,
        cwd: t.owner ? HOME : GUEST_WORK,
        prompt: t.prompt,
        systemPrompt: t.owner ? SYSTEM : GUEST_SYSTEM,
        mcpConfig: mcpConfig(t.chat, t.owner),
        model: MODEL || undefined,
        effort: EFFORT || undefined,
        resume: sid,
        timeoutMs: TIMEOUT_MS,
        env: t.owner ? { AGENT_OUTBOX: OUTBOX } : guestTurnEnv(),
        isolate: t.owner ? undefined : GUEST,
        signal,
      },
      events,
    );
  let result;
  // No guest process may outlive its turn or see the next one: stop them before and after every turn,
  // and refuse the turn while any are left.
  const left = GUEST ? await stopUser(GUEST) : [];
  if (left.length) {
    console.error(`guest processes could not be stopped: ${left.join(" ")}`);
    throw new TurnError("guest processes from an earlier turn could not be stopped; refusing the turn.", "error");
  }
  try {
    result = await run(resume);
  } catch (e) {
    if (!resume || !isStaleSession(e)) throw e;
    console.warn(`session ${resume} could not be resumed, starting fresh`);
    result = await run(undefined);
  } finally {
    const left = GUEST ? await stopUser(GUEST) : [];
    if (left.length) console.error(`guest processes could not be stopped: ${left.join(" ")}`);
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

async function deliverOutbox(chat: number, dir: string, guest: boolean) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    try {
      if (!lstatSync(path).isFile()) continue;
    } catch (e) {
      console.warn(`outbox: skipping ${name}: ${(e as Error).message ?? e}`);
      continue;
    }
    const bytes = guest ? readRegular(path) : undefined;
    if (guest && !bytes) continue;
    await tg.sendFile(chat, undefined, path, bytes).then(
      () => unlinkSync(path),
      (e) => tg.send(chat, undefined, `Could not send ${name}: ${e.message ?? e} (kept on the host)`),
    );
  }
}

// ---------- incoming messages ----------

// ---------- schedules: a tick every minute, small delays caught up (5 minutes at most) ----------

let lastMinute = Math.floor(Date.now() / 60_000);

function tick() {
  // A tick runs from setInterval: anything it throws would end the bot, owner chats included.
  try {
    tickMinutes();
  } catch (e) {
    console.error(e);
    otel.log(`schedule tick failed: ${(e as Error).message ?? e}`, {}, "ERROR");
  }
}

function tickMinutes() {
  const nowMinute = Math.floor(Date.now() / 60_000);
  const from = Math.max(lastMinute + 1, nowMinute - 4);
  lastMinute = nowMinute;
  for (let m = from; m <= nowMinute; m++) {
    const at = new Date(m * 60_000);
    const all: [Schedule, boolean][] = [
      ...listSchedules(SCHEDULES).map((s): [Schedule, boolean] => [s, false]),
      ...guestSchedules(GUEST_SCHEDULES, GROUPS, guestFired()).map((s): [Schedule, boolean] => [s, true]),
    ];
    for (const [s, fromGuest] of all) {
      if (!s.enabled) continue;
      let due = false;
      try {
        due = cronMatches(s.cron, at);
      } catch (e) {
        console.warn(`schedule ${s.id}: ${(e as Error).message}`);
      }
      if (due) fire(s, at, fromGuest);
    }
  }
}

function guestFired(): Set<string> {
  try {
    return new Set(JSON.parse(readFileSync(GUEST_FIRED, "utf8")));
  } catch {
    return new Set();
  }
}

/** Remember a fired guest once-entry; keys whose entry the guest has since removed are dropped. */
function markGuestFired(s: Schedule) {
  const present = new Set(guestSchedules(GUEST_SCHEDULES, GROUPS).map(guestKey));
  const keep = [...guestFired()].filter((k) => present.has(k));
  writeFileSync(GUEST_FIRED, JSON.stringify([...keep, guestKey(s)]));
}

// Align to the start of each minute (plus a second of slack), then tick every minute.
setTimeout(() => {
  tick();
  setInterval(tick, 60_000);
}, 60_000 - (Date.now() % 60_000) + 1000);

// ---------- long polling ----------

// Telegram answers 502 or 504 now and then. A failing poll is a warning only
// once it has lasted this long; shorter gaps log at INFO.
const POLL_WARN_MS = 5 * 60_000;

async function poll() {
  let offset = 0;
  let failingSince: number | null = null;
  let warned = false;
  for (;;) {
    try {
      const updates = await tg.call<Update[]>("getUpdates", {
        offset,
        timeout: 30,
        allowed_updates: ["message", "stopped_message_generation"],
      });
      if (warned) otel.log("poll recovered", {}, "INFO");
      failingSince = null;
      warned = false;
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
      failingSince ??= Date.now();
      const lasting = Date.now() - failingSince >= POLL_WARN_MS;
      otel.log(`poll failed: ${e}`, {}, lasting && !warned ? "WARN" : "INFO");
      if (lasting) warned = true;
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
if (UPDATE_HOURS > 0) {
  updateClaude();
  setInterval(updateClaude, UPDATE_HOURS * 3_600_000);
}
poll();
