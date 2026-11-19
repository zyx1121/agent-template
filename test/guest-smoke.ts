// Run inside the built image (see .github/workflows/ci.yml): checks what the entrypoint hands the bot.
// A guest turn must start from run/guest/work, run as the guest uid, and leave files the bot can read.
import { readFileSync } from "node:fs";
import { asUser } from "../src/claude";

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
console.log("guest-smoke: ok");
