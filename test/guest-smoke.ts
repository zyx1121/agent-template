// Run inside the built image (see .github/workflows/ci.yml): checks what the entrypoint hands the bot.
// A guest turn must start from run/guest/work, run as the guest uid, and leave files the bot can read.
import { chmodSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { asUser, runTurn, stopUser } from "../src/claude";

const fail = (m: string) => {
  console.error(`guest-smoke: ${m}`);
  process.exit(1);
};
process.umask(0o077); // as src/main.ts
if (process.env.HOME !== "/home/agent") fail(`bot HOME is ${process.env.HOME}`);
const uid = Number(process.env.AGENT_GUEST_UID);
if (!uid) fail("AGENT_GUEST_UID is not set");
const probe = "/app/run/guest/sched/probe.json";
const p = Bun.spawn(asUser(["sh", "-c", 'id -u; echo "{}" > "$0"', probe], { uid, gid: Number(process.env.AGENT_GUEST_GID) }), {
  cwd: "/app/run/guest/work",
  env: { PATH: process.env.PATH ?? "" },
  stdout: "pipe",
  stderr: "pipe",
});
const [code, out, err] = await Promise.all([p.exited, new Response(p.stdout).text(), new Response(p.stderr).text()]);
if (code !== 0) fail(`guest spawn exited ${code}: ${err.trim()}`);
if (out.trim() !== String(uid)) fail(`guest ran as uid ${out.trim()}`);
if (readFileSync(probe, "utf8").trim() !== "{}") fail("bot cannot read what the guest wrote");

// A guest that chmods its own work dir to 700 must not lock out its next turn.
{
  const g = { uid, gid: Number(process.env.AGENT_GUEST_GID) };
  const marker = "/app/run/guest/sched/after-chmod";
  await Bun.spawn(asUser(["sh", "-c", 'chmod 700 /app/run/guest/work; printf "#!/bin/sh\\ntouch %s\\n" "$0" > /app/run/guest/work/mark-claude; chmod 755 /app/run/guest/work/mark-claude', marker], g), { cwd: "/" }).exited;
  await runTurn({ claude: "/app/run/guest/work/mark-claude", cwd: "/app/run/guest/work", prompt: "p", systemPrompt: "s", mcpConfig: "m",
    timeoutMs: 5000, env: { PATH: process.env.PATH ?? "" }, isolate: g }).catch(() => {});
  await Bun.spawn(asUser(["chmod", "770", "/app/run/guest/work"], g), { cwd: "/" }).exited;
  if (!statSync(marker, { throwIfNoEntry: false })) fail("a guest turn did not start after the guest chmod 700 its work dir");
}

// A guest turn's background process must not outlive the turn (it could read later owner turns in /proc).
const guest = { uid, gid: Number(process.env.AGENT_GUEST_GID) };
const guestPids = () =>
  readdirSync("/proc").filter((p) => {
    try {
      return /^\d+$/.test(p) && new RegExp(`^Uid:\\s+${uid}\\s`, "m").test(readFileSync(`/proc/${p}/status`, "utf8"));
    } catch {
      return false;
    }
  });
await Bun.spawn(asUser(["sh", "-c", "setsid sleep 300 </dev/null >/dev/null 2>&1 &"], guest), { cwd: "/app/run/guest/work", env: { PATH: process.env.PATH ?? "" } }).exited;
await Bun.sleep(200);
if (guestPids().length === 0) fail("the background guest process did not start");
if ((await stopUser(guest)).length) fail("stopUser reported guest processes left");
await Bun.sleep(200);
if (guestPids().length) fail(`guest processes survived stopUser: ${guestPids().join(" ")}`);

// A guest process that kills everything of its uid in a loop must not survive either: the bot signals from its own uid.
await Bun.spawn(asUser(["sh", "-c", "setsid sh -c 'while :; do kill -KILL -1; done' </dev/null >/dev/null 2>&1 &"], guest), { cwd: "/app/run/guest/work", env: { PATH: process.env.PATH ?? "" } }).exited;
await Bun.sleep(200);
if (guestPids().length === 0) fail("the guest kill loop did not start");
if ((await stopUser(guest)).length) fail("stopUser reported the guest kill loop left");
await Bun.sleep(200);
if (guestPids().length) fail(`the guest kill loop survived stopUser: ${guestPids().join(" ")}`);
// A leader that calls pthread_exit shows as a zombie while its worker thread runs: it must not be skipped.
const work = "/app/run/guest/work";
const put = (name: string, body: string) => {
  writeFileSync(`${work}/${name}`, body);
  chmodSync(`${work}/${name}`, 0o644);
};
put("zl-worker.ts", `setInterval(() => require("node:fs").appendFileSync("${work}/zl.beat", "."), 20);`);
put("zl.ts", `import { dlopen, FFIType } from "bun:ffi";
new Worker(new URL("./zl-worker.ts", import.meta.url).href);
await Bun.sleep(500);
dlopen("libc.so.6", { pthread_exit: { args: [FFIType.ptr], returns: FFIType.void } }).symbols.pthread_exit(null);`);
await Bun.spawn(asUser(["sh", "-c", `setsid bun ${work}/zl.ts </dev/null >/dev/null 2>&1 &`], guest), { cwd: work, env: { PATH: process.env.PATH ?? "" } }).exited;
await Bun.sleep(1500);
const zl = guestPids().find((p) => /^State:\s+Z/m.test(readFileSync(`/proc/${p}/status`, "utf8")));
if (!zl) fail("the guest zombie leader did not start");
if ((await stopUser(guest)).length) fail("stopUser reported the zombie leader left");
const beat = statSync(`${work}/zl.beat`).size;
await Bun.sleep(300);
if (statSync(`${work}/zl.beat`).size !== beat) fail("the worker of a zombie leader survived stopUser");
if (guestPids().length) fail(`the zombie leader survived stopUser: ${guestPids().join(" ")}`);

// A chain of processes that fork and exit at once can sit between pids while /proc is scanned.
put("hop.sh", `echo >> ${work}/hop.beat; (sh ${work}/hop.sh &); exit 0\n`);
for (let i = 0; i < 40; i++) {
  await Bun.spawn(asUser(["sh", "-c", `for n in $(seq 32); do setsid sh ${work}/hop.sh </dev/null >/dev/null 2>&1 & done`], guest), { cwd: work, env: { PATH: process.env.PATH ?? "" } }).exited;
  await Bun.sleep(100);
  if ((await stopUser(guest)).length) fail("stopUser reported the hop chain left");
  const beat = statSync(`${work}/hop.beat`).size;
  await Bun.sleep(300); // a scan of guestPids can miss a hopping chain too, so watch what it appends
  if (statSync(`${work}/hop.beat`).size !== beat || guestPids().length) fail(`a hop chain survived stopUser in round ${i}`);
}

// A guest can SIGSTOP its own claude, which then ignores the timeout's SIGINT: the turn must still end.
put("stopped-claude", `#!/bin/sh\ncat >/dev/null\nsetsid sh -c "sleep 0.5; kill -STOP $$" </dev/null >/dev/null 2>&1 &\nsleep 30\n`);
chmodSync(`${work}/stopped-claude`, 0o755);
const t0 = Date.now();
const stopped = await runTurn({ claude: `${work}/stopped-claude`, cwd: work, prompt: "p", systemPrompt: "s", mcpConfig: "m",
  timeoutMs: 1000, env: { PATH: process.env.PATH ?? "" }, isolate: guest }).then(() => "returned", () => "threw");
if (stopped !== "threw") fail("a stopped guest claude did not fail its turn");
if (Date.now() - t0 > 8000) fail(`a stopped guest claude held its turn for ${Date.now() - t0} ms`);
await Bun.sleep(200);
if (guestPids().length) fail(`guest processes survived a stopped turn: ${guestPids().join(" ")}`);
console.log("guest-smoke: ok");
