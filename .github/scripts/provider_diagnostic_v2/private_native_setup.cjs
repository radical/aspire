"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

function owned(file, directory = false) {
  const stat = fs.lstatSync(file);
  if (stat.uid !== process.getuid() || (!directory && stat.nlink !== 1) ||
      (directory ? !stat.isDirectory() : !stat.isFile()) ||
      (stat.mode & 0o777) !== (directory ? 0o700 : 0o600)) {
    throw new Error("private setup boundary");
  }
  return stat;
}

function same(file, before) {
  const after = owned(file);
  if (after.dev !== before.dev || after.ino !== before.ino || after.size > 4 * 1024 * 1024) {
    throw new Error("private setup boundary");
  }
}

function privateRoot() {
  const root = path.join(process.env.RUNNER_TEMP, "provider-diagnostic-v2");
  owned(root, true);
  if (fs.realpathSync(root) !== root) {
    throw new Error("private setup boundary");
  }
  return root;
}

function create(file) {
  return fs.openSync(file, fs.constants.O_RDWR | fs.constants.O_CREAT |
    fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
}

function nativeValues(text) {
  // Native core.setOutput uses name<<ghadelimiter_<uuid>, value, delimiter.
  const lines = text.split("\n");
  if (lines.pop() !== "") {
    throw new Error("native output framing");
  }
  const result = [];
  for (let index = 0; index < lines.length;) {
    const match = /^(visibility|min_integrity|repos)<<(ghadelimiter_[0-9a-f-]{36})$/.exec(lines[index++]);
    if (!match || index + 1 >= lines.length ||
        !lines[index] || /[\r\0]/.test(lines[index]) || lines[index].length > 4096 ||
        lines[index + 1] !== match[2]) {
      throw new Error("native output framing");
    }
    result.push([match[1], lines[index]]);
    index += 2;
  }
  return result;
}

async function resolveGuard(github, context, core) {
  const output = process.env.GITHUB_OUTPUT;
  const start = fs.statSync(output).size;
  let outcome;
  let calls = 0;
  // The pinned resolver catches I/O errors as well as API errors. Observe only
  // request completion, without replacing the native client, core, or request.
  // This prevents an output/summary failure being accepted as an API fallback.
  github.hook.wrap("request", async (request, options) => {
    calls++;
    try {
      const response = await request(options);
      outcome = "resolved";
      return response;
    } catch (error) {
      outcome = "rejected";
      throw error;
    }
  });
  const determine = require(path.join(process.env.RUNNER_TEMP, "gh-aw/actions/determine_automatic_lockdown.cjs"));
  await determine(github, context, core);
  const values = nativeValues(fs.readFileSync(output, "utf8").slice(start));
  const order = values.map(([name]) => name).join(",");
  const summarySize = owned(process.env.GITHUB_STEP_SUMMARY).size;
  if (calls !== 1 || !core.summary.isEmptyBuffer() ||
      (outcome === "resolved" ? order !== "visibility,min_integrity,repos" || summarySize === 0 :
        outcome !== "rejected" || order !== "min_integrity,repos,visibility" || summarySize !== 0)) {
    throw new Error("private guard finalization");
  }
  const guard = path.join(privateRoot(), "guard.json");
  const fd = create(guard);
  try {
    fs.writeFileSync(fd, JSON.stringify(Object.fromEntries(values)));
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function runNative(stage, script, environment = {}) {
  if (!/^(guard|config|tools)$/.test(stage) || !process.env.GITHUB_OUTPUT ||
      !path.isAbsolute(process.argv[1])) {
    throw new Error("private setup boundary");
  }
  const root = privateRoot();
  const log = path.join(root, `private-${stage}.log`);
  const summary = path.join(root, `private-${stage}-summary.md`);
  const logFd = create(log);
  let summaryFd;
  let failure;
  try {
    summaryFd = create(summary);
    const logIdentity = owned(log);
    const summaryIdentity = owned(summary);
    // Both child streams, including github-script's outer catch and automatic
    // result output errors, stay private. GITHUB_OUTPUT is inherited unchanged.
    const environmentForChild = { ...process.env, ...environment, INPUT_SCRIPT: script,
      GITHUB_STEP_SUMMARY: summary, INPUT_DEBUG: "false" };
    if (stage !== "guard") {
      // These exact pinned generators and setupGlobals perform only local
      // configuration/file generation. Native github-script still requires a
      // nonempty input to construct its unused client; no job credential is
      // needed in either generator process.
      environmentForChild["INPUT_GITHUB-TOKEN"] = "credential-free-generator";
      for (const key of ["GITHUB_TOKEN", "GH_TOKEN", "GITHUB_MCP_SERVER_TOKEN"]) {
        delete environmentForChild[key];
      }
    }
    const child = spawnSync(process.execPath, [process.argv[1]], {
      env: environmentForChild,
      stdio: ["ignore", logFd, logFd],
      timeout: 120000,
      killSignal: "SIGKILL",
    });
    fs.fsyncSync(logFd);
    fs.fsyncSync(summaryFd);
    same(log, logIdentity);
    same(summary, summaryIdentity);
    if (child.error || child.signal || child.status !== 0) {
      throw new Error("private setup child");
    }
  } catch (error) {
    failure = error;
  } finally {
    for (const fd of [summaryFd, logFd]) {
      if (fd !== undefined) {
        try {
          fs.closeSync(fd);
        } catch (error) {
          failure = error;
        }
      }
    }
  }
  if (failure) {
    throw new Error("private setup failed");
  }
}

async function run(core) {
  privateRoot();
  const inputs = require("./real_setup_inputs.json");
  const actionsDir = path.join(process.env.RUNNER_TEMP, "gh-aw/actions");
  const safeDir = path.join(process.env.RUNNER_TEMP, "gh-aw/safeoutputs");
  fs.mkdirSync(safeDir, { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(safeDir, "upload-artifacts"), { recursive: true, mode: 0o700 });
  core.setSecret(process.env.GITHUB_OUTPUT);
  try {
    runNative("guard", `await require(${JSON.stringify(__filename)}).resolveGuard(github, context, core);`);
    runNative("config", inputs.config.script, {
      ...inputs.config.env, GH_AW_FILE_ROOT: path.dirname(actionsDir),
    });
    runNative("tools", inputs.tools.script, inputs.tools.env);
  } finally {
    // The runner processes native file commands after the action terminates.
    // Register native masking before then, including partial failure output.
    // Never transport these values through a rendered step env/run expression.
    try {
      const text = fs.readFileSync(process.env.GITHUB_OUTPUT, "utf8");
      if (text.length > 65536) {
        throw new Error("native output boundary");
      }
      if (text) {
        core.setSecret(text);
      }
    } catch (error) {
      fs.truncateSync(process.env.GITHUB_OUTPUT, 0);
      throw new Error("native output boundary");
    }
  }
}

module.exports = { run, runNative, resolveGuard };
