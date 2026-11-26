"use strict";
/*
 * The job contract of an organisation's own service, for Node (spec v2 §21): the same
 * surface as the Python module `amsyos`. A service on an organisation's space is
 * started once a job with $AMSYOS_JOB naming the job's folder; this reads and writes
 * that folder, so a service need not know its layout:
 *
 *   const { job } = require("amsyos");      // ESM: import { job } from "amsyos";
 *
 *   const text = job.params.text;            // the request's params, defaults filled in
 *   const plan = job.json("plan");           // a JSON input, parsed (text or file)
 *   const picture = job.input("image");      // an input file's path, or null
 *   job.inputName("image");                  // the name it was uploaded with, or null
 *   job.service, job.serviceVersion;         // which service the job is for, or null
 *   job.progress(40);                        // 0 to 100, optional
 *   fs.writeFileSync(job.output("result", ".txt"), text);   // one file an output
 *   job.fail("the picture is too small");    // ends the job with a hint
 *
 * Or a server (an image built with `serve`): started once, it loads what it needs, then
 * does the jobs one after the other; `job` is the job now, the handler may be async:
 *
 *   const { job, serve } = require("amsyos");
 *   const model = load();                    // once, not once a job
 *   serve(async (job) => { fs.writeFileSync(job.output("result", ".txt"), model(job.params.text)); });
 *
 * `serve` runs only in a server kept warm (`serve:` in the card): in a job started as a
 * command it fails the job at once, saying so. A served image several services share tells
 * their jobs apart by `job.service`.
 *
 * Each job is a line on stdin and its end a line on stdout (what `serve` reads and
 * writes); what the handler prints goes to stderr, the job's log. `job.fail` ends only
 * the job; an error thrown by the handler fails the job, and the server goes on.
 *
 * A structured input is one JSON input with a schema in the card (sdk/CONTRACT.md §7):
 * `job.json(name)` gives its value parsed, whether the call gave JSON text or a JSON file;
 * AMSYOS has checked it against the schema before the job started.
 *
 * Only Node's own modules; found through NODE_PATH on every job.
 */
const fs = require("fs");
const path = require("path");
const readline = require("readline");

const LINE_MAX = 4096; // a line of the server's protocol, each way
const JOB_ID = /^[A-Za-z0-9_-]{1,64}$/;
const NOT_SERVED =
  "serve() runs only in a server kept warm: declare serve: in the card (serve: node " +
  "server.js), or call your handler with job (handle(job)) under command:";

class Failed extends Error {}

class Job {
  constructor(folder) {
    this.folder = path.resolve(folder || process.env.AMSYOS_JOB || ".");
    this._told = null;
    this._serving = false;
  }

  /** job.json as it is: {params, inputs, input_names, service, ...}. */
  get told() {
    if (this._told === null) {
      this._told = JSON.parse(fs.readFileSync(path.join(this.folder, "job.json"), "utf8"));
    }
    return this._told;
  }

  /** The request's params, as the manifest's inputs name them. */
  get params() {
    return this.told.params || {};
  }

  /** The id of the service the job is for ("t_42.media.tts"), or null when job.json does
   * not say it: an image several services share tells their jobs apart by it. */
  get service() {
    return serviceOf(this.told)[0];
  }

  /** The version of that service ("1.2.0"), or null when job.json does not say it. */
  get serviceVersion() {
    return serviceOf(this.told)[1];
  }

  /** The path of the file given for the input `name`, or null when none was. */
  input(name) {
    const rel = (this.told.inputs || {})[name];
    return rel ? path.join(this.folder, rel) : null;
  }

  /** The value of the JSON input `name`, parsed: the JSON file given for it, or its JSON
   * text (a param). AMSYOS has already checked it against the input's schema when the card
   * gives one. null when the call gave neither; a value that is not JSON fails the job with
   * a hint the caller can act on (INVALID_PARAMS). */
  json(name) {
    const file = this.input(name);
    if (file !== null) {
      const text = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
      try {
        return JSON.parse(text);
      } catch (err) {
        this.fail(`${name}: the file is not JSON (${clip(err)}); give a JSON file`);
      }
    }
    const value = this.params[name];
    if (value === undefined || value === null) return null;
    if (typeof value !== "string") return value; // JSON already (a local run's example)
    try {
      return JSON.parse(value);
    } catch (err) {
      this.fail(`${name}: not JSON text (${clip(err)}); give it as JSON, like {"key": 1}`);
    }
  }

  /** The name the file of the input `name` was uploaded with ("chair v3.glb"), or null
   * when it is not known: a plain file name, at most 200 characters. */
  inputName(name) {
    return (this.told.input_names || {})[name] || null;
  }

  /** Where to write the output `name`: one file, with the extension of its kind. */
  output(name, suffix = "") {
    const out = path.join(this.folder, "out");
    fs.mkdirSync(out, { recursive: true });
    const ext = suffix && !suffix.startsWith(".") ? `.${suffix}` : suffix;
    return path.join(out, `${name}${ext}`);
  }

  progress(percent) {
    const at = Math.max(0, Math.min(100, Number(percent) || 0));
    fs.writeFileSync(path.join(this.folder, "progress"), String(Math.round(at)));
  }

  /** End the job as failed; `hint` is what the caller reads to fix its request. In a
   * server, the server goes on with the next job. */
  fail(hint, code = "INVALID_PARAMS") {
    const said = JSON.stringify({ error: String(code), hint: String(hint) });
    fs.writeFileSync(path.join(this.folder, "error.json"), said);
    if (this._serving) throw new Failed(String(hint));
    process.exit(1);
  }

  /** Do the jobs the machine hands this process, one after the other, with
   * `await handler(job)`, until the machine ends it. Only in a server kept warm, which the
   * machine starts with $AMSYOS_SERVE_STATE: anywhere else stdin holds no job, so the job
   * fails at once with NOT_SERVED instead of ending with no output. */
  async serve(handler) {
    if (!process.env.AMSYOS_SERVE_STATE) {
      if (process.env.AMSYOS_JOB) {
        try {
          const said = JSON.stringify({ error: "SERVICE_ERROR", hint: NOT_SERVED });
          fs.writeFileSync(path.join(this.folder, "error.json"), said);
        } catch {}
      }
      console.error(NOT_SERVED);
      process.exit(1);
    }
    // What the handler prints is its output (stderr), not the protocol (fd 1).
    process.stdout.write = process.stderr.write.bind(process.stderr);
    this._serving = true;
    const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    for await (const line of lines) {
      const told = jobLine(line);
      if (told === null) continue;
      this.folder = told.folder;
      this._told = null;
      // A served job's temporary files in its own folder (removed with it), as a job
      // started as a command has them: os.tmpdir() reads TMPDIR each time.
      const tmp = path.join(this.folder, "tmp");
      if (fs.existsSync(tmp)) {
        for (const name of ["TMPDIR", "TEMP", "TMP"]) process.env[name] = tmp;
      }
      try {
        await handler(this);
      } catch (err) {
        if (!(err instanceof Failed)) {
          console.error(err);
          this._failed(String((err && err.message) || err).slice(0, 500));
        }
      }
      protocol(JSON.stringify({ done: told.job }));
    }
  }

  _failed(hint) {
    const error = path.join(this.folder, "error.json");
    if (!fs.existsSync(error)) {
      fs.writeFileSync(error, JSON.stringify({ error: "SERVICE_ERROR", hint }));
    }
  }
}

/** [id, version] of `service`: {id, version}; an older node wrote the id alone, with
 * `version` beside it; an older space, `service_id` and `service_version`. */
function serviceOf(told) {
  const given = told.service;
  let id, version;
  if (given && typeof given === "object") {
    [id, version] = [given.id, given.version];
  } else if (typeof given === "string") {
    [id, version] = [given, told.version];
  } else {
    [id, version] = [told.service_id, told.service_version];
  }
  const text = (x) => (typeof x === "string" && x ? x : null);
  return [text(id), text(version)];
}

function clip(err, most = 120) {
  const said = String((err && err.message) || err);
  return said.length <= most ? said : said.slice(0, most - 1) + "…";
}

/** One line to the machine on fd 1, whole (a pipe Node made non-blocking may say EAGAIN). */
function protocol(text) {
  const data = Buffer.from(text + "\n");
  let off = 0;
  while (off < data.length) {
    try {
      off += fs.writeSync(1, data, off);
    } catch (err) {
      if (err.code !== "EAGAIN") throw err;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
    }
  }
}

/** {job, folder} of the machine's line, or null for anything else. */
function jobLine(line) {
  if (line.length > LINE_MAX) return null;
  let told;
  try {
    told = JSON.parse(line);
  } catch {
    return null;
  }
  if (!told || typeof told.job !== "string" || !JOB_ID.test(told.job)) return null;
  if (typeof told.folder !== "string") return null;
  return { job: told.job, folder: told.folder };
}

const job = new Job();

/** `job.serve(handler)`: a server's loop (an image built with `serve`). */
function serve(handler) {
  return job.serve(handler);
}

module.exports = { Job, job, serve };
