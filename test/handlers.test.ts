import { expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHandlers, sessionFile, type Turn } from "../src/handlers";
import { addSchedule, guestKey, listSchedules, removeSchedule, type Schedule } from "../src/schedules";
import type { Message } from "../src/telegram";

const OWNER = 1;
const MEMBER = 2;
const GROUP = -100;
const BOT = { id: 99, username: "testbot" };

function setup(guest: boolean) {
  const dir = mkdtempSync(join(tmpdir(), "handlers-"));
  const RUN = join(dir, "run");
  const INBOX = join(dir, "inbox");
  const GUEST_INBOX = join(dir, "guest-inbox");
  const schedulesPath = join(dir, "schedules.json");
  const firedPath = join(dir, "guest-fired.json");
  const sent: { chat: number; text: string }[] = [];
  const downloads: string[] = [];
  const turns: Turn[] = [];
  const tg = {
    send: async (chat: number, _a: unknown, text: string) => void sent.push({ chat, text }),
    download: async (id: string) => (downloads.push(id), "file.txt"),
  };
  const h = createHandlers({
    tg: tg as any,
    me: BOT,
    OWNER,
    GROUPS: new Set([GROUP]),
    NAME: "Test",
    GUEST: guest,
    RUN,
    INBOX,
    GUEST_INBOX,
    removeSchedule: (id) => removeSchedule(schedulesPath, id),
    markGuestFired: (s) => {
      const keys: string[] = existsSync(firedPath) ? JSON.parse(readFileSync(firedPath, "utf8")) : [];
      writeFileSync(firedPath, JSON.stringify([...keys, guestKey(s)]));
    },
    enqueue: async (t) => void turns.push(t),
  });
  return { ...h, dir, RUN, schedulesPath, firedPath, sent, downloads, turns };
}

const groupMsg = (from: number, text: string, extra: Partial<Message> = {}): Message => ({
  message_id: 10,
  from: { id: from } as Message["from"],
  chat: { id: GROUP, type: "supergroup", title: "g" },
  text,
  entities: [{ type: "mention", offset: 0, length: "@testbot".length }] as Message["entities"],
  ...extra,
});

const replyToBot = { message_id: 1, from: { id: BOT.id }, chat: { id: GROUP, type: "supergroup" } } as Message;

const sched = (over: Partial<Schedule> = {}): Schedule => ({
  id: "abc123",
  cron: "* * * * *",
  prompt: "ping",
  chat_id: GROUP,
  note: "",
  enabled: true,
  once: true,
  created_at: "2026-01-01T00:00:00.000Z",
  ...over,
});

const fires = (h: ReturnType<typeof setup>, s: Schedule, fromGuest: boolean) => h.fire(s, new Date(2026, 0, 1), fromGuest);

for (const [label, text, extra] of [
  ["plain text", "@testbot hello", {}],
  ["/new", "/new", { entities: [], reply_to_message: replyToBot }],
  ["an attachment", "@testbot see this", { document: { file_id: "f1", file_name: "a.txt" } }],
] as const) {
  test(`guest refusal without GUEST: ${label} gets one refusal and no side effect`, async () => {
    const h = setup(false);
    const session = sessionFile(h.RUN, GROUP, false);
    writeFileSync(join(h.dir, "marker"), "x");
    await h.onMessage(groupMsg(MEMBER, text, extra as Partial<Message>));
    expect(h.sent.length).toBe(1);
    expect(h.sent[0].text).toContain("can't take messages from group members");
    expect(h.downloads).toEqual([]);
    expect(h.turns).toEqual([]);
    expect(existsSync(h.RUN)).toBe(false); // nothing created or removed under RUN
    expect(existsSync(session)).toBe(false);
  });
}

test("guest refusal without GUEST leaves an existing guest session file in place", async () => {
  const h = setup(false);
  const { mkdirSync } = await import("node:fs");
  mkdirSync(h.RUN, { recursive: true });
  const session = sessionFile(h.RUN, GROUP, false);
  writeFileSync(session, "sid");
  await h.onMessage(groupMsg(MEMBER, "/new", { entities: [], reply_to_message: replyToBot }));
  expect(existsSync(session)).toBe(true);
  expect(h.sent.length).toBe(1);
});

test("a guest once-entry skipped by fire stays in both stores", () => {
  const h = setup(false);
  const owned = addSchedule(h.schedulesPath, { cron: "* * * * *", prompt: "p", chat_id: GROUP, note: "", once: true });
  fires(h, owned, false);
  expect(listSchedules(h.schedulesPath).map((s) => s.id)).toEqual([owned.id]);
  const g = sched();
  fires(h, g, true);
  expect(existsSync(h.firedPath)).toBe(false);
  expect(h.turns).toEqual([]);
});

test("with GUEST set, a guest schedule naming the owner chat runs with owner=false", () => {
  const h = setup(true);
  fires(h, sched({ chat_id: OWNER, once: false }), true);
  expect(h.turns.length).toBe(1);
  expect(h.turns[0].owner).toBe(false);
  expect(h.turns[0].chat).toBe(OWNER);
});

test("control: the owner message enqueues with owner=true", async () => {
  const h = setup(false);
  await h.onMessage({ message_id: 5, from: { id: OWNER } as Message["from"], chat: { id: OWNER, type: "private" }, text: "hello" });
  expect(h.turns.length).toBe(1);
  expect(h.turns[0].owner).toBe(true);
  expect(h.sent).toEqual([]);
});

test("control: a once-entry that runs is consumed from both stores", () => {
  const h = setup(true);
  const owned = addSchedule(h.schedulesPath, { cron: "* * * * *", prompt: "p", chat_id: OWNER, note: "", once: true });
  fires(h, owned, false);
  expect(listSchedules(h.schedulesPath)).toEqual([]);
  expect(h.turns[0].owner).toBe(true);
  const g = sched();
  fires(h, g, true);
  expect(JSON.parse(readFileSync(h.firedPath, "utf8"))).toEqual([guestKey(g)]);
  expect(h.turns.length).toBe(2);
});
