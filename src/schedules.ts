// run/schedules.json, shared by the bot (the minute tick) and the per-turn schedule MCP server.
// Same schema as the Python template, so an existing bot keeps its schedules.
// Writes go through a lock directory and an atomic rename, so a reader never sees half a file.

import { existsSync, mkdirSync, readFileSync, renameSync, rmdirSync, writeFileSync } from "node:fs";
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
    return { ...data, schedules: data.schedules ?? [] };
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
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, JSON.stringify(data, null, 2));
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
