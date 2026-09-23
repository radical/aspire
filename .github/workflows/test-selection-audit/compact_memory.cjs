const fs = require("fs");
const path = require("path");

const memoryRoot = process.env.MEMORY_ROOT;
const auditDatePath = process.env.AUDIT_DATE_PATH;
const retentionText = process.env.RETENTION_DAYS || "14";
if (!/^[1-9][0-9]*$/.test(retentionText) || Number(retentionText) > 90) {
  throw new Error("retention days must be between 1 and 90");
}
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
const auditDate = new Date().toISOString().slice(0, 10);
const cutoff = new Date(
  Date.parse(`${auditDate}T00:00:00Z`) - Number(retentionText) * 86400000)
  .toISOString().slice(0, 10);
const requireUtcDate = (value, context) => {
  if (typeof value !== "string" || !datePattern.test(value)) {
    throw new Error(`${context} must be a UTC date`);
  }
  const milliseconds = Date.parse(`${value}T00:00:00Z`);
  if (!Number.isFinite(milliseconds) ||
      new Date(milliseconds).toISOString().slice(0, 10) !== value ||
      value > auditDate) {
    throw new Error(`${context} must be a real UTC date no later than ${auditDate}`);
  }
};
fs.mkdirSync(path.dirname(auditDatePath), { recursive: true });
fs.writeFileSync(auditDatePath, `${auditDate}\n`, { mode: 0o444 });

const readRows = fileName => {
  const filePath = path.join(memoryRoot, fileName);
  if (!fs.existsSync(filePath)) return { filePath, exists: false, rows: [] };
  const text = fs.readFileSync(filePath, "utf8");
  if (text && !text.endsWith("\n")) throw new Error(`${fileName} must end with a newline`);
  return {
    filePath,
    exists: true,
    rows: text ? text.trimEnd().split("\n").map(line => JSON.parse(line)) : []
  };
};
const writeRows = ({ filePath, exists }, rows) => {
  if (!exists && rows.length === 0) return;
  const temporaryPath = `${filePath}.compact-${process.pid}`;
  fs.writeFileSync(
    temporaryPath,
    rows.map(row => JSON.stringify(row)).join("\n") + (rows.length ? "\n" : ""));
  fs.renameSync(temporaryPath, filePath);
};

const processedFile = readRows("processed-runs.jsonl");
for (const [index, row] of processedFile.rows.entries()) {
  if (!Number.isSafeInteger(row.pr) ||
      typeof row.sha !== "string" ||
      !Array.isArray(row.over_paths) ||
      !Array.isArray(row.miss_edges)) {
    throw new Error(`processed-runs.jsonl:${index + 1} has an invalid compaction shape`);
  }
  requireUtcDate(row.seen, `processed-runs.jsonl:${index + 1}.seen`);
}
const retained = processedFile.rows.filter(row => row.seen >= cutoff);
const contributions = new Map();
const addContribution = (key, row) => {
  let value = contributions.get(key);
  if (!value) {
    value = { identities: new Set(), prs: new Set(), dates: [] };
    contributions.set(key, value);
  }
  value.identities.add(`${row.pr}:${row.sha}`);
  value.prs.add(row.pr);
  value.dates.push(row.seen);
};
for (const row of retained) {
  for (const pathValue of row.over_paths) addContribution(`over\u0000${pathValue}`, row);
  for (const edge of row.miss_edges) addContribution(`miss\u0000${edge.path}\u0000${edge.target}`, row);
}

const watchFile = readRows("watchlist.jsonl");
const watch = [];
for (const [index, existing] of watchFile.rows.entries()) {
  const row = { ...existing };
  if (row.kind !== "over-selection" && row.kind !== "under-selection") {
    throw new Error(`watchlist.jsonl:${index + 1} has an invalid kind`);
  }
  if (!Array.isArray(row.example_prs)) {
    throw new Error(`watchlist.jsonl:${index + 1} has an invalid compaction shape`);
  }
  requireUtcDate(row.first_seen, `watchlist.jsonl:${index + 1}.first_seen`);
  requireUtcDate(row.last_seen, `watchlist.jsonl:${index + 1}.last_seen`);
  if (row.first_seen > row.last_seen) {
    throw new Error(`watchlist.jsonl:${index + 1}.first_seen is after last_seen`);
  }
  const key = row.kind === "over-selection"
    ? `over\u0000${row.path}`
    : `miss\u0000${row.path}\u0000${row.target}`;
  const contribution = contributions.get(key);
  const count = contribution?.identities.size || 0;
  if (count === 0 && row.verdict === "watch") continue;
  if (row.kind === "over-selection") row.all_runs = count;
  else row.miss_runs = count;
  const priorExamples = row.example_prs.filter(pr => contribution?.prs.has(pr));
  const remainingExamples = [...(contribution?.prs || [])]
    .sort((left, right) => left - right)
    .filter(pr => !priorExamples.includes(pr));
  row.example_prs = [...priorExamples, ...remainingExamples].slice(0, 3);
  if (count > 0) {
    row.first_seen = contribution.dates.reduce((left, right) => left < right ? left : right);
    row.last_seen = contribution.dates.reduce((left, right) => left > right ? left : right);
  }
  watch.push(row);
}

writeRows(processedFile, retained);
writeRows(watchFile, watch);
console.log(`Retained ${retained.length}/${processedFile.rows.length} processed rows since ${cutoff}.`);
