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

/** Guest turns need a separate uid; without one they would run as the bot with its files and environment. */
export const turnAllowed = (owner: boolean, hasGuestUid: boolean) => owner || hasGuestUid;

/** What `onMessage` does once a message is addressed to the bot. Side effects are injected so a test can see their order. */
export type MessageIo<T> = {
  refuse(): T;
  start(): T;
  fresh(): T;
  /** Saves the attachment and queues the turn. */
  turn(): T;
};

/** The guest refusal comes before /new, attachments and the queue, so a refused message leaves no trace. */
export function routeMessage<T>(fromOwner: boolean, hasGuestUid: boolean, cmd: string | undefined, io: MessageIo<T>): T {
  if (!turnAllowed(fromOwner, hasGuestUid)) return io.refuse();
  if (cmd === "start") return io.start();
  if (cmd === "new") return io.fresh();
  return io.turn();
}

export type FireIo = {
  skip(): void;
  /** Removes an owner once-entry, or marks a guest one as fired. */
  consume(): void;
  run(owner: boolean): void;
};

/** A schedule that may not run is skipped before a once-entry is removed or marked fired, so it fires after the fix. */
export function routeSchedule(chatId: number, ownerId: number, once: boolean, fromGuest: boolean, hasGuestUid: boolean, io: FireIo) {
  // Anyone in a group can add a schedule there, so a group schedule never acts for the owner.
  // A schedule a guest wrote never acts for the owner, whatever chat_id it carries.
  const owner = !fromGuest && chatId === ownerId;
  if (!turnAllowed(owner, hasGuestUid)) return io.skip();
  if (once) io.consume();
  io.run(owner);
}
