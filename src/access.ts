// Who a turn acts for. The owner's turns get every MCP server and the owner's Claude Code config dir.
// Anyone else (a group member, or a schedule that fires in a group) gets a separate config dir and
// no server named in AGENT_OWNER_ONLY_MCP, so an owner login cannot be used or spoofed from a group.

import type { Message } from "./telegram";
import { senderName } from "./prompts";

export const parseOwnerOnly = (value: string) =>
  new Set(value.split(",").map((s) => s.trim()).filter(Boolean));

/** The fork's own MCP servers that this turn may use. */
export function turnServers(own: Record<string, unknown>, ownerOnly: Set<string>, owner: boolean) {
  if (owner) return { ...own };
  return Object.fromEntries(Object.entries(own).filter(([name]) => !ownerOnly.has(name)));
}

/** The prompt prefix for a group message: the verified Telegram user id first, the display name quoted. */
export function senderLabel(msg: Message, owner: number) {
  const id = msg.from?.id;
  const who = id === owner ? `the owner, user id ${id}` : `user id ${id ?? "unknown"}, not the owner`;
  return `[from ${who}; display name ${JSON.stringify(senderName(msg))}]`;
}
