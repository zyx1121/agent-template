// One headless Claude Code turn: `claude -p --output-format stream-json`, with streamed text and
// tool steps reported as they happen, per-chat --resume, and failures classified for delivery.

import { readdirSync, readFileSync } from "node:fs";

export type TurnEvents = {
  /** The assistant text of this turn so far (all text blocks, grows as tokens arrive). */
  onText?: (text: string) => void;
  onTool?: (name: string, input: Record<string, any>) => void;
};

export type TurnOptions = {
  claude: string;
  cwd: string;
  prompt: string;
  systemPrompt: string;
  mcpConfig: string;
  model?: string;
  effort?: string;
  resume?: string;
  timeoutMs: number;
  env: Record<string, string>;
  /** Run as this uid:gid with exactly `env` (no inherited environment). Needs setuid and setgid in the bot's capabilities. */
  isolate?: { uid: number; gid: number };
  signal?: AbortSignal;
};

export type TurnResult = { reply: string; sessionId?: string };

export type ErrorCategory = "usage_limit" | "auth" | "transient" | "error";

export class TurnError extends Error {
  constructor(
    message: string,
    readonly category: ErrorCategory,
  ) {
    super(message);
  }
}

export function classify(message: string, status?: number): ErrorCategory {
  if (status === 429 || /(usage|session|rate)[ -]?limit/i.test(message)) return "usage_limit";
  if (status === 401 || status === 403 || /not logged in|please run \/login|invalid bearer|failed to authenticate|authentication/i.test(message))
    return "auth";
  if ([408, 500, 502, 503, 504, 529].includes(status ?? 0) || /overloaded|temporarily unavailable|timed? ?out|connection|network|econn/i.test(message))
    return "transient";
  return "error";
}

/** What a guest turn may inherit: the way to reach the API and nothing else (no bot token, no fork credentials). */
const GUEST_ENV = /^(PATH|LANG|LC_[A-Z]+|TZ|TERM|HTTPS?_PROXY|NO_PROXY|ALL_PROXY|CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_[A-Z_]+)$/i;

export function guestEnv(src: Record<string, string | undefined>, extra: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src)) if (v !== undefined && GUEST_ENV.test(k)) out[k] = v;
  return { ...out, ...extra };
}

/** Run argv with umask 022 instead of the bot's 077, so the bot can read what a guest turn writes (outbox, schedules). */
export const withUmask022 = (args: string[]) => ["sh", "-c", 'umask 022; exec "$@"', "sh", ...args];

/** Run argv from `dir`, entered by whoever runs this (so after asUser has dropped privileges). */
export const inDir = (args: string[], dir: string) => ["sh", "-c", 'cd "$0" || exit 1; exec "$@"', dir, ...args];

/** Wrap argv so it runs as another uid with every capability dropped. */
export const asUser = (args: string[], u: { uid: number; gid: number }) => [
  "setpriv", "--reuid", String(u.uid), "--regid", String(u.gid), "--clear-groups",
  "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", ...withUmask022(args),
];

/**
 * Live processes whose real, effective, saved or fs uid is `uid`. A zombie is skipped only when all its
 * threads are zombies: a leader that called pthread_exit shows as a zombie while its other threads run.
 */
export function uidPids(uid: number, proc = "/proc"): number[] {
  const out: number[] = [];
  for (const name of readdirSync(proc)) {
    if (!/^\d+$/.test(name)) continue;
    try {
      const status = readFileSync(`${proc}/${name}/status`, "utf8");
      if (zombie(status) && !liveThread(`${proc}/${name}/task`)) continue;
      const uids = /^Uid:\s+(.*)$/m.exec(status)?.[1].trim().split(/\s+/).map(Number) ?? [];
      if (uids.includes(uid)) out.push(Number(name));
    } catch {} // the process exited while we looked
  }
  return out;
}

const zombie = (status: string) => /^State:\s+Z/m.test(status);

function liveThread(task: string) {
  for (const tid of readdirSync(task))
    try {
      if (!zombie(readFileSync(`${task}/${tid}/status`, "utf8"))) return true;
    } catch {} // the thread exited while we looked
  return false;
}

/**
 * Stop every process of a uid, so nothing a guest turn left running (`setsid ... &`) can watch later
 * turns through /proc. Two sweeps per round:
 * - The bot SIGSTOPs, then SIGKILLs, every match of a /proc scan from its own uid (it holds CAP_KILL),
 *   so a guest process that kills everything of its uid cannot kill this cleanup.
 * - A helper running as the uid calls kill(-1, SIGKILL) once. The kernel signals every process of the
 *   uid under one lock that fork also takes, so a chain that forks and exits cannot hop past it the way
 *   it can slip between two reads of a /proc scan.
 * Done when the helper ran its kill (it exits 0; a guest process may kill or stop it first) and a scan after it
 * finds nothing. Returns the pids still alive after `rounds`.
 */
export async function stopUser(u: { uid: number; gid: number }, rounds = 20): Promise<number[]> {
  for (let i = 0; i < rounds; i++) {
    for (const sig of ["SIGSTOP", "SIGKILL"] as const)
      for (const pid of uidPids(u.uid))
        try {
          process.kill(pid, sig);
        } catch {} // already gone
    const sweep = Bun.spawn(asUser(["sh", "-c", "kill -KILL -1 2>/dev/null; exit 0"], u), {
      cwd: "/", env: { PATH: process.env.PATH ?? "" }, stdin: "ignore", stdout: "ignore", stderr: "ignore" });
    // A guest process can also SIGSTOP the helper, so do not wait on it for long.
    const code = await Promise.race([sweep.exited, Bun.sleep(1000).then(() => sweep.kill("SIGKILL"))]);
    const swept = code === 0 && sweep.signalCode == null;
    if (swept && !uidPids(u.uid).length) return [];
    await Bun.sleep(20);
  }
  return uidPids(u.uid);
}

/** After the child exits, how long its pipes may stay open before the turn stops reading them. */
const PIPE_GRACE_MS = 500;
/** After a timeout or stop sends SIGINT, how long the child has to exit before it gets SIGKILL. */
export const KILL_GRACE_MS = 3000;

/** Read a stream until it ends or `stop` settles: a process the child left behind can hold the pipe open forever. */
async function readUntil(s: ReadableStream<Uint8Array>, stop: Promise<unknown>, on: (b: Uint8Array) => void) {
  const r = s.getReader();
  const stopped = stop.then(() => ({ done: true as const, value: undefined }));
  for (;;) {
    const { done, value } = await Promise.race([r.read(), stopped]);
    if (done) break;
    on(value);
  }
  r.cancel().catch(() => {});
}

export async function runTurn(o: TurnOptions, ev: TurnEvents = {}): Promise<TurnResult> {
  const args = [
    // The prompt goes over stdin, not argv: argv is visible to every uid in /proc/<pid>/cmdline.
    o.claude, "-p",
    "--output-format", "stream-json", "--include-partial-messages", "--verbose",
    "--permission-mode", "bypassPermissions",
    "--append-system-prompt", o.systemPrompt,
    "--mcp-config", o.mcpConfig, "--strict-mcp-config",
  ];
  if (o.model) args.push("--model", o.model);
  if (o.effort) args.push("--effort", o.effort);
  if (o.resume) args.push("--resume", o.resume);
  // A guest owns its work dir and can chmod it to 700, so the bot spawns from "/" and the guest enters it after setpriv.
  const p = Bun.spawn(o.isolate ? asUser(inDir(args, o.cwd), o.isolate) : args, {
    cwd: o.isolate ? "/" : o.cwd,
    env: o.isolate ? o.env : { ...process.env, ...o.env },
    stdin: new Blob([o.prompt]), stdout: "pipe", stderr: "pipe" });
  // A stopped process does not act on SIGINT, and a guest's Bash can SIGSTOP its own claude (same uid),
  // so escalate to SIGKILL, and for a guest turn stop every process of the uid from the bot's side.
  let killer: ReturnType<typeof setTimeout> | undefined;
  const kill = () => {
    p.kill("SIGINT");
    killer ??= setTimeout(() => {
      p.kill("SIGKILL");
      if (o.isolate) void stopUser(o.isolate);
    }, KILL_GRACE_MS);
  };
  const timer = setTimeout(kill, o.timeoutMs);
  o.signal?.addEventListener("abort", kill);

  let result: any;
  let sessionId: string | undefined;
  let done = ""; // text of finished assistant messages in this turn
  let live = ""; // text of the message being streamed
  const decoder = new TextDecoder();
  let buf = "";
  // A guest's Bash can reopen claude's stdout or stderr (same uid) from a detached child; stop reading
  // shortly after claude exits instead of waiting for EOF, so the turn ends and its cleanup runs.
  const drained = p.exited.then(() => Bun.sleep(PIPE_GRACE_MS));
  let stderr = "";
  const errDecoder = new TextDecoder();
  const errRead = readUntil(p.stderr, drained, (b) => (stderr += errDecoder.decode(b, { stream: true })));
  await readUntil(p.stdout, drained, (bytes) => {
    buf += decoder.decode(bytes, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      let e: any;
      try {
        e = JSON.parse(line);
      } catch {
        continue;
      }
      if (e.session_id) sessionId = e.session_id;
      if (e.type === "stream_event" && e.event?.type === "content_block_delta" && e.event.delta?.type === "text_delta") {
        live += e.event.delta.text;
        ev.onText?.(join(done, live));
      } else if (e.type === "assistant") {
        for (const c of e.message?.content ?? []) {
          if (c.type === "text" && c.text.trim()) done = join(done, c.text.trim());
          if (c.type === "tool_use") ev.onTool?.(c.name, c.input ?? {});
        }
        live = "";
        ev.onText?.(done);
      } else if (e.type === "result") result = e;
    }
  });
  await errRead;
  const code = await p.exited;
  clearTimeout(timer);
  clearTimeout(killer);
  if (o.signal?.aborted) throw new TurnError("Stopped.", "error");
  if (code !== 0 || !result || result.is_error) {
    const message = (result?.result || stderr.trim() || `claude exited ${code}`).slice(0, 500);
    throw new TurnError(message, classify(message, result?.api_error_status));
  }
  return { reply: String(result.result ?? "").trim(), sessionId };
}

const join = (a: string, b: string) => (a && b ? `${a}\n\n${b}` : a || b);

/** A stale --resume id fails fast; the caller retries once with a fresh session. */
export const isStaleSession = (e: unknown) =>
  e instanceof TurnError && /no conversation found|session.*not found|could not resume/i.test(e.message);
