// One headless Claude Code turn: `claude -p --output-format stream-json`, with streamed text and
// tool steps reported as they happen, per-chat --resume, and failures classified for delivery.

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

/** Wrap argv so it runs as another uid with every capability dropped. */
export const asUser = (args: string[], u: { uid: number; gid: number }) => [
  "setpriv", "--reuid", String(u.uid), "--regid", String(u.gid), "--clear-groups",
  "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", ...withUmask022(args),
];

/**
 * Stop every process of a uid, so nothing a guest turn left running (`setsid ... &`) can watch later
 * turns through /proc. The shell runs as that uid and kill(-1) spares only itself.
 */
export async function stopUser(u: { uid: number; gid: number }) {
  await Bun.spawn(asUser(["sh", "-c", "kill -KILL -1 2>/dev/null; true"], u), { env: { PATH: process.env.PATH ?? "" }, stdout: "ignore", stderr: "ignore" }).exited;
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
  const p = Bun.spawn(o.isolate ? asUser(args, o.isolate) : args, {
    cwd: o.cwd,
    env: o.isolate ? o.env : { ...process.env, ...o.env },
    stdin: new Blob([o.prompt]), stdout: "pipe", stderr: "pipe" });
  const kill = () => p.kill("SIGINT");
  const timer = setTimeout(kill, o.timeoutMs);
  o.signal?.addEventListener("abort", kill);

  let result: any;
  let sessionId: string | undefined;
  let done = ""; // text of finished assistant messages in this turn
  let live = ""; // text of the message being streamed
  const decoder = new TextDecoder();
  let buf = "";
  for await (const bytes of p.stdout) {
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
  }
  const stderr = await new Response(p.stderr).text();
  const code = await p.exited;
  clearTimeout(timer);
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
