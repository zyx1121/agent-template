// The builtin `schedule` MCP server (stdio, newline-delimited JSON-RPC), spawned by claude for
// each turn. It only edits run/schedules.json; the bot's minute tick does the firing.
// AGENT_SCHEDULES (file) and AGENT_CHAT_ID (the chat new schedules reply to) come from env.

import { validateCron } from "./cron";
import { addSchedule, editSchedule, listSchedules, removeSchedule } from "./schedules";

const FILE = process.env.AGENT_SCHEDULES ?? "";
const CHAT = Number(process.env.AGENT_CHAT_ID ?? "0");

const TOOLS = [
  {
    name: "schedule_add",
    description:
      "Create a persistent schedule. When it fires, a fresh turn runs in this chat with `prompt` and the reply is sent " +
      "here. The only reliable way to set reminders or recurring tasks. The prompt must be self-contained: the turn " +
      "that runs it has none of this conversation.",
    inputSchema: {
      type: "object",
      properties: {
        cron: { type: "string", description: '5-field cron, local time, e.g. "0 8 * * *" or "*/30 * * * *"' },
        prompt: { type: "string", description: "The complete task for the future turn" },
        note: { type: "string", description: "Short label shown by schedule_list" },
        once: { type: "boolean", description: "Fire once, then delete itself" },
      },
      required: ["cron", "prompt"],
    },
  },
  { name: "schedule_list", description: "List every schedule.", inputSchema: { type: "object", properties: {} } },
  {
    name: "schedule_edit",
    description: "Change fields of a schedule by id; omitted fields stay as they are.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string" },
        cron: { type: "string" },
        prompt: { type: "string" },
        note: { type: "string" },
        enabled: { type: "boolean" },
        once: { type: "boolean" },
      },
      required: ["id"],
    },
  },
  {
    name: "schedule_remove",
    description: "Delete a schedule by id.",
    inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"] },
  },
];

function call(name: string, a: any): string {
  if (!FILE || !CHAT) throw new Error("AGENT_SCHEDULES and AGENT_CHAT_ID must be set by the bot");
  if (name === "schedule_add") {
    validateCron(a.cron);
    const s = addSchedule(FILE, { cron: a.cron, prompt: a.prompt, note: a.note ?? "", once: Boolean(a.once), chat_id: CHAT });
    return `Added ${s.id}: "${s.cron}"${s.once ? " (once)" : ""}`;
  }
  if (name === "schedule_list") {
    const all = listSchedules(FILE);
    return all.length ? JSON.stringify(all, null, 2) : "No schedules.";
  }
  if (name === "schedule_edit") {
    if (a.cron !== undefined) validateCron(a.cron);
    const s = editSchedule(FILE, a.id, a);
    return s ? `Updated ${s.id}: ${JSON.stringify(s)}` : `No schedule ${a.id}`;
  }
  if (name === "schedule_remove") return removeSchedule(FILE, a.id) ? `Removed ${a.id}` : `No schedule ${a.id}`;
  throw new Error(`unknown tool ${name}`);
}

function handle(msg: any) {
  const { id, method, params } = msg;
  if (method === "initialize")
    return {
      protocolVersion: params?.protocolVersion ?? "2025-06-18",
      capabilities: { tools: {} },
      serverInfo: { name: "schedule", version: "1.0.0" },
    };
  if (method === "tools/list") return { tools: TOOLS };
  if (method === "tools/call") {
    try {
      return { content: [{ type: "text", text: call(params.name, params.arguments ?? {}) }] };
    } catch (e) {
      return { content: [{ type: "text", text: `Error: ${(e as Error).message}` }], isError: true };
    }
  }
  if (method === "ping") return {};
  if (id === undefined) return undefined; // notifications need no answer
  throw Object.assign(new Error(`method not found: ${method}`), { code: -32601 });
}

let buf = "";
for await (const chunk of Bun.stdin.stream()) {
  buf += new TextDecoder().decode(chunk);
  let nl: number;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    const msg = JSON.parse(line);
    let reply: object | undefined;
    try {
      const result = handle(msg);
      if (result !== undefined && msg.id !== undefined) reply = { jsonrpc: "2.0", id: msg.id, result };
    } catch (e: any) {
      reply = { jsonrpc: "2.0", id: msg.id, error: { code: e.code ?? -32603, message: e.message } };
    }
    if (reply) process.stdout.write(`${JSON.stringify(reply)}\n`);
  }
}
