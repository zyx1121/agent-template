// Minimal OTLP/HTTP JSON exporter: logs, spans and gauges, batched every 5 seconds.
// A no-op unless SENSORIUM_URL (or any OTLP/HTTP base URL) and SENSORIUM_TOKEN are set.

const URL_BASE = process.env.SENSORIUM_URL ?? "";
const TOKEN = process.env.SENSORIUM_TOKEN ?? "";
const enabled = Boolean(URL_BASE && TOKEN);

type Attrs = Record<string, string | number | boolean | undefined>;
const SERVICE = process.env.OTEL_SERVICE_NAME || (process.env.AGENT_NAME ?? "agent").toLowerCase().replace(/[^a-z0-9-]+/g, "-");
const resource = { attributes: attrs({ "service.name": SERVICE, "host.name": process.env.HOSTNAME }) };
const scope = { name: "agent-template" };

function attrs(a: Attrs) {
  return Object.entries(a)
    .filter(([, v]) => v !== undefined)
    .map(([key, v]) => ({
      key,
      value:
        typeof v === "number"
          ? Number.isInteger(v)
            ? { intValue: String(v) }
            : { doubleValue: v }
          : typeof v === "boolean"
            ? { boolValue: v }
            : { stringValue: String(v) },
    }));
}

const nanos = (ms = Date.now()) => `${BigInt(Math.round(ms)) * 1_000_000n}`;
const hex = (bytes: number) => Buffer.from(crypto.getRandomValues(new Uint8Array(bytes))).toString("hex");

let logs: object[] = [];
let spans: object[] = [];
let points: object[] = [];
let warned = false;

export function log(body: string, a: Attrs = {}, severity: "INFO" | "WARN" | "ERROR" = "INFO") {
  if (!enabled) return;
  const severityNumber = { INFO: 9, WARN: 13, ERROR: 17 }[severity];
  logs.push({ timeUnixNano: nanos(), severityText: severity, severityNumber, body: { stringValue: body }, attributes: attrs(a) });
}

/** Start a span; call the returned function to end it with more attributes. */
export function span(name: string, a: Attrs = {}) {
  const start = Date.now();
  return (end: Attrs = {}, error?: string) => {
    if (!enabled) return;
    spans.push({
      traceId: hex(16),
      spanId: hex(8),
      name,
      kind: 1,
      startTimeUnixNano: nanos(start),
      endTimeUnixNano: nanos(),
      attributes: attrs({ ...a, ...end }),
      status: error ? { code: 2, message: error } : { code: 1 },
    });
  };
}

export function gauge(name: string, value: number, unit = "1", a: Attrs = {}) {
  if (!enabled) return;
  points.push({ name, unit, gauge: { dataPoints: [{ timeUnixNano: nanos(), asDouble: value, attributes: attrs(a) }] } });
}

async function post(path: string, body: object) {
  const res = await fetch(`${URL_BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
}

export async function flush() {
  if (!enabled) return;
  const [l, s, p] = [logs, spans, points];
  logs = [];
  spans = [];
  points = [];
  try {
    if (l.length) await post("/v1/logs", { resourceLogs: [{ resource, scopeLogs: [{ scope, logRecords: l }] }] });
    if (s.length) await post("/v1/traces", { resourceSpans: [{ resource, scopeSpans: [{ scope, spans: s }] }] });
    if (p.length) await post("/v1/metrics", { resourceMetrics: [{ resource, scopeMetrics: [{ scope, metrics: p }] }] });
    warned = false;
  } catch (e) {
    // Telemetry must never take the bot down; say so once per outage.
    if (!warned) console.error(`otel export failed: ${e}`);
    warned = true;
  }
}

if (enabled) setInterval(flush, 5000);
