import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { addSchedule, listSchedules } from "../src/schedules";

test("schedule MCP server speaks stdio JSON-RPC and writes the store", async () => {
  const file = join(mkdtempSync(join(tmpdir(), "mcp-")), "schedules.json");
  const p = Bun.spawn([process.execPath, join(import.meta.dir, "../src/mcp-schedule.ts")], {
    stdin: "pipe",
    stdout: "pipe",
    env: { ...process.env, AGENT_SCHEDULES: file, AGENT_CHAT_ID: "42" },
  });
  const send = (o: object) => p.stdin.write(`${JSON.stringify(o)}\n`);
  send({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } });
  send({ jsonrpc: "2.0", method: "notifications/initialized" });
  send({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  send({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "schedule_add", arguments: { cron: "0 8 * * *", prompt: "hi" } } });
  send({ jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "schedule_add", arguments: { cron: "bad", prompt: "x" } } });
  p.stdin.end();
  const lines = (await new Response(p.stdout).text()).trim().split("\n").map((l) => JSON.parse(l));
  expect(lines.map((l) => l.id)).toEqual([1, 2, 3, 4]);
  expect(lines[1].result.tools.map((t: any) => t.name)).toContain("schedule_add");
  expect(lines[2].result.content[0].text).toStartWith("Added");
  expect(lines[3].result.isError).toBe(true);
  const stored = JSON.parse(await Bun.file(file).text());
  expect(stored.schedules[0].chat_id).toBe(42);
});

test("a chat-scoped schedule server cannot see or change another chat's schedules", async () => {
  const file = join(mkdtempSync(join(tmpdir(), "mcp-")), "schedules.json");
  const owners = addSchedule(file, { cron: "0 8 * * *", prompt: "owner task", chat_id: 123, note: "", once: false });
  const call = async (scope: string, name: string, args: object) => {
    const p = Bun.spawn([process.execPath, join(import.meta.dir, "../src/mcp-schedule.ts")], {
      stdin: "pipe",
      stdout: "pipe",
      env: { ...process.env, AGENT_SCHEDULES: file, AGENT_CHAT_ID: "-5", AGENT_SCHEDULE_SCOPE: scope },
    });
    p.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } })}\n`);
    p.stdin.end();
    return JSON.parse((await new Response(p.stdout).text()).trim()).result.content[0].text as string;
  };
  expect(await call("chat", "schedule_list", {})).toBe("No schedules.");
  expect(await call("chat", "schedule_edit", { id: owners.id, prompt: "evil" })).toBe(`No schedule ${owners.id}`);
  expect(await call("chat", "schedule_remove", { id: owners.id })).toBe(`No schedule ${owners.id}`);
  expect(listSchedules(file)[0].prompt).toBe("owner task");
  expect(await call("all", "schedule_list", {})).toContain(owners.id);
});
