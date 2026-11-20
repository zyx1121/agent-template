// run/schedules.json, shared by the bot (the minute tick) and the per-turn schedule MCP server.
// Same schema as the Python template, so an existing bot keeps its schedules.
// Writes go through a lock directory and an atomic rename, so a reader never sees half a file.

import { closeSync, constants, existsSync, fstatSync, mkdirSync, openSync, readFileSync, renameSync, rmdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export type Schedule = {
  id: string;
  cron: string;
  prompt: string;
  chat_id: number;
  note: string;
  enabled: boolean;
  once: boolean;
  created_at: string;
};

const EDITABLE = ["cron", "prompt", "note", "enabled", "once"] as const;

function read(path: string): { schedules: Schedule[] } {
  if (!existsSync(path)) return { schedules: [] };
  try {
    const data = JSON.parse(readFileSync(path, "utf8"));
    return { schedules: data.schedules ?? [] };
  } catch {
    return { schedules: [] };
  }
}

function locked<T>(path: string, fn: () => T): T {
  mkdirSync(dirname(path), { recursive: true });
  const lock = `${path}.lockdir`;
  for (let i = 0; ; i++) {
    try {
      mkdirSync(lock);
      break;
    } catch {
      if (i > 200) rmdirSync(lock); // a crashed holder: take over after ~10 s
      Bun.sleepSync(50);
    }
  }
  try {
    return fn();
  } finally {
    rmdirSync(lock);
  }
}

function write(path: string, data: object) {
  // A fresh name, created exclusively: a link planted at a predictable name is never followed.
  const tmp = `${path}.${process.pid}.${Buffer.from(crypto.getRandomValues(new Uint8Array(6))).toString("hex")}.tmp`;
  writeFileSync(tmp, JSON.stringify(data, null, 2), { flag: "wx" });
  renameSync(tmp, path);
}

export function listSchedules(path: string): Schedule[] {
  return read(path).schedules;
}

export function addSchedule(path: string, s: Pick<Schedule, "cron" | "prompt" | "chat_id" | "note" | "once">) {
  return locked(path, () => {
    const data = read(path);
    const ids = new Set(data.schedules.map((x) => x.id));
    let id: string;
    do id = Buffer.from(crypto.getRandomValues(new Uint8Array(3))).toString("hex");
    while (ids.has(id));
    const sched: Schedule = { id, enabled: true, created_at: new Date().toISOString(), ...s };
    data.schedules.push(sched);
    write(path, data);
    return sched;
  });
}

export function editSchedule(path: string, id: string, fields: Partial<Schedule>) {
  return locked(path, () => {
    const data = read(path);
    const sched = data.schedules.find((x) => x.id === id);
    if (!sched) return null;
    for (const k of EDITABLE) if (fields[k] !== undefined) (sched as any)[k] = fields[k];
    write(path, data);
    return sched;
  });
}

export function removeSchedule(path: string, id: string) {
  return locked(path, () => {
    const data = read(path);
    const before = data.schedules.length;
    data.schedules = data.schedules.filter((x) => x.id !== id);
    if (data.schedules.length !== before) write(path, data);
    return data.schedules.length !== before;
  });
}

/** Read a file another uid controls: no symlinks, no FIFOs or devices (they would block or leak), bounded size. */
export function readRegular(path: string, max = 50 * 1024 * 1024): Buffer | undefined {
  let fd: number | undefined;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const st = fstatSync(fd);
    if (st.isFile() && st.size <= max) return readFileSync(fd);
  } catch {}
  finally {
    if (fd !== undefined) closeSync(fd);
  }
}

const short = (v: unknown, max: number): v is string => typeof v === "string" && v.length <= max;

/** Identifies a guest schedule across rewrites by the guest, for the bot's record of fired once-entries. */
export const guestKey = (s: Schedule) => `${s.id}@${s.created_at}`;

/**
 * Schedules written by guest turns, which share no uid with the bot: the file is data, not trusted,
 * and the bot never writes it. A fired once-entry stays in the file; the caller passes its key in `fired`.
 * Only well-formed entries for an allowed group survive, and the caller never runs them as the owner.
 */
export function guestSchedules(path: string, groups: Set<number>, fired: Set<string> = new Set()): Schedule[] {
  let schedules: unknown;
  try {
    schedules = JSON.parse(readRegular(path, 1024 * 1024)?.toString("utf8") ?? "{}").schedules;
  } catch {}
  if (!Array.isArray(schedules)) return [];
  return (schedules as Schedule[]).filter(
    (s): s is Schedule =>
      Boolean(s) &&
      short(s.id, 64) &&
      short(s.cron, 128) &&
      short(s.prompt, 16 * 1024) &&
      short(s.created_at, 64) &&
      Number.isInteger(s.chat_id) &&
      s.chat_id < 0 &&
      groups.has(s.chat_id) &&
      s.enabled === true &&
      !fired.has(guestKey(s)),
  ).map((s) => ({ ...s, note: short(s.note, 1024) ? s.note : "", once: s.once === true }));
}
