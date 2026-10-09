// Manual check, not part of `bun test`: one real turn with the builtin schedule MCP mounted.
// Run inside the image with CLAUDE_CODE_OAUTH_TOKEN set: bun test/live-turn.ts
import { writeFileSync } from "node:fs";
import { runTurn } from "../src/claude";

const cfg = "/tmp/mcp.json";
writeFileSync(cfg, JSON.stringify({ mcpServers: { schedule: { type: "stdio", command: process.execPath, args: ["/app/src/mcp-schedule.ts"], env: { AGENT_SCHEDULES: "/tmp/s.json", AGENT_CHAT_ID: "1" } } } }));
let texts = 0;
const tools: string[] = [];
const r = await runTurn(
  { claude: "claude", cwd: "/app", prompt: "Call the schedule_list tool, then reply with one short sentence saying how many schedules exist.", systemPrompt: "Be brief.", mcpConfig: cfg, model: "haiku", timeoutMs: 120_000, env: {} },
  { onText: () => texts++, onTool: (n) => tools.push(n) },
);
console.log(JSON.stringify({ reply: r.reply, session: Boolean(r.sessionId), tools, textEvents: texts }));
