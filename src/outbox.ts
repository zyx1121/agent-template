import { readdirSync, rmSync } from "node:fs";
import { join } from "node:path";

/**
 * Empty `dir` before a turn. The guest can leave an entry the bot cannot remove (a non-empty 0700 directory),
 * so a failure is logged per entry and never stops the turn.
 */
export function clearOutbox(dir: string, warn: (msg: string) => void = console.warn): void {
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch (e) {
    warn(`outbox ${dir}: cannot list: ${(e as Error).message ?? e}`);
    return;
  }
  for (const name of names) {
    try {
      rmSync(join(dir, name), { recursive: true, force: true });
    } catch (e) {
      warn(`outbox: cannot remove ${name}: ${(e as Error).message ?? e}`);
    }
  }
}
