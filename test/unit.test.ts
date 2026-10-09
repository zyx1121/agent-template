import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { classify } from "../src/claude";
import { cronMatches, validateCron } from "../src/cron";
import { addressed, isNoReply, replyContext, scheduledPrompt, toolLine } from "../src/prompts";
import { addSchedule, editSchedule, listSchedules, removeSchedule } from "../src/schedules";
import { chunkMarkdown, parseCommand } from "../src/telegram";

test("cron: steps, ranges, Sunday as 7, dom OR dow", () => {
  const mon = new Date(2026, 9, 12, 9, 0); // Monday 12 Oct 2026, 09:00
  expect(cronMatches("0 9 * * 1-5", mon)).toBe(true);
  expect(cronMatches("*/15 9 * * *", mon)).toBe(true);
  expect(cronMatches("0 9 * * 0", mon)).toBe(false);
  expect(cronMatches("0 9 * * 7", new Date(2026, 9, 11, 9, 0))).toBe(true);
  expect(cronMatches("0 9 1 * 1", mon)).toBe(true); // a Monday counts though it is not the 1st
  expect(() => validateCron("61 * * * *")).toThrow();
  expect(() => validateCron("* * *")).toThrow();
});

test("NO_REPLY only when it ends the reply as a bare token", () => {
  expect(isNoReply("NO_REPLY")).toBe(true);
  expect(isNoReply("Nothing new. NO_REPLY")).toBe(true);
  expect(isNoReply("I would say NO_REPLY.")).toBe(false);
  expect(isNoReply("FOO_NO_REPLY")).toBe(false);
});

test("errors are classified for delivery", () => {
  expect(classify("You've hit your session limit · resets 1:50pm")).toBe("usage_limit");
  expect(classify("whatever", 401)).toBe("auth");
  expect(classify("API Error: 529 overloaded")).toBe("transient");
  expect(classify("something else")).toBe("error");
});

test("group addressing: mention entity or reply to the bot", () => {
  const base = { message_id: 1, chat: { id: -5, type: "group" } };
  expect(addressed({ ...base, text: "hi @my_bot", entities: [{ type: "mention", offset: 3, length: 7 }] }, "my_bot", 9)).toBe(true);
  expect(addressed({ ...base, text: "😀 @my_bot", entities: [{ type: "mention", offset: 3, length: 7 }] }, "my_bot", 9)).toBe(true);
  expect(addressed({ ...base, text: "hi there" }, "my_bot", 9)).toBe(false);
  expect(addressed({ ...base, text: "ok", reply_to_message: { message_id: 0, chat: base.chat, from: { id: 9 } } }, "my_bot", 9)).toBe(true);
});

test("reply context quotes the replied text", () => {
  const ctx = replyContext(
    { message_id: 2, chat: { id: 1, type: "private" }, reply_to_message: { message_id: 1, chat: { id: 1, type: "private" }, text: "deploy at 5", from: { id: 9 } } },
    9,
  );
  expect(ctx).toContain("your own earlier message");
  expect(ctx).toContain("deploy at 5");
});

test("schedule store keeps the Python schema and round-trips edits", () => {
  const file = join(mkdtempSync(join(tmpdir(), "sched-")), "schedules.json");
  const s = addSchedule(file, { cron: "0 8 * * *", prompt: "standup", chat_id: 42, note: "daily", once: false });
  expect(Object.keys(s).sort()).toEqual(["chat_id", "created_at", "cron", "enabled", "id", "note", "once", "prompt"]);
  expect(editSchedule(file, s.id, { enabled: false })?.enabled).toBe(false);
  expect(listSchedules(file)).toHaveLength(1);
  expect(removeSchedule(file, s.id)).toBe(true);
  expect(listSchedules(file)).toHaveLength(0);
});

test("prompt helpers", () => {
  expect(scheduledPrompt({ id: "abc123", note: "", prompt: "go" }, new Date(2026, 0, 2, 3, 4))).toBe(
    "[schedule fired id=abc123 note=(none) time=2026-01-02 03:04]\ngo",
  );
  expect(toolLine("Bash", { command: "ls -la\nmore" })).toBe("⚡️ Bash ls -la");
  expect(toolLine("mcp__portal__whoami", {})).toBe("🔧 portal.whoami");
  expect(parseCommand("/new@my_bot")).toEqual({ cmd: "new", args: "" });
  for (const p of chunkMarkdown("```\n" + "x\n".repeat(300) + "```", 200)) expect((p.match(/^```/gm) ?? []).length % 2).toBe(0);
});
