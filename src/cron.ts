// 5-field cron (minute hour dom month dow) against the process's local clock.
// `*`, numbers, lists, ranges and steps; dow 0 and 7 are both Sunday; when dom and dow are
// both restricted, matching either is enough (standard cron).

const BOUNDS: [number, number][] = [
  [0, 59],
  [0, 23],
  [1, 31],
  [1, 12],
  [0, 7],
];
const NAMES = ["minute", "hour", "dom", "month", "dow"];

function parseItem(item: string, lo: number, hi: number): number[] {
  let step = 1;
  let range = item;
  if (item.includes("/")) {
    const [r, s] = item.split("/", 2);
    step = Number(s);
    range = r;
    if (!Number.isInteger(step) || step <= 0) throw new Error(`bad step in ${item}`);
  }
  let start: number;
  let end: number;
  if (range === "*") [start, end] = [lo, hi];
  else if (range.includes("-")) {
    [start, end] = range.split("-", 2).map(Number);
    if (start > end) throw new Error(`range start > end: ${item}`);
  } else start = end = Number(range);
  if (![start, end].every((v) => Number.isInteger(v) && v >= lo && v <= hi))
    throw new Error(`value out of range [${lo},${hi}]: ${item}`);
  const out: number[] = [];
  for (let v = start; v <= end; v += step) out.push(v);
  return out;
}

function parse(expr: string) {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) throw new Error(`cron needs 5 fields (minute hour dom month dow), got ${parts.length}`);
  const sets = parts.map((part, i) => {
    try {
      return new Set(part.split(",").flatMap((item) => parseItem(item, ...BOUNDS[i])));
    } catch (e) {
      throw new Error(`${NAMES[i]} field ${part}: ${(e as Error).message}`);
    }
  });
  if (sets[4].delete(7)) sets[4].add(0);
  return { sets, domStar: parts[2] === "*", dowStar: parts[4] === "*" };
}

export function validateCron(expr: string) {
  parse(expr);
}

export function cronMatches(expr: string, at: Date) {
  const { sets, domStar, dowStar } = parse(expr);
  const [min, hour, dom, month, dow] = sets;
  if (!min.has(at.getMinutes()) || !hour.has(at.getHours()) || !month.has(at.getMonth() + 1)) return false;
  const domOk = dom.has(at.getDate());
  const dowOk = dow.has(at.getDay());
  if (!domStar && !dowStar) return domOk || dowOk;
  return domOk && dowOk;
}
