// Run inside the built image (see .github/workflows/ci.yml): checks what the entrypoint hands the bot.
// A guest turn must start from run/guest/work, run as the guest uid, and leave files the bot can read.
import { readdirSync, readFileSync } from "node:fs";
import { asUser, stopUser } from "../src/claude";

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
console.log("guest-smoke: ok");
