import assert from "node:assert/strict";
import test from "node:test";

import { api, eventEntries } from "../src/ochecore/static/client.mjs";

function untilAborted(signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  });
}

test("API timeout aborts a request that never receives headers", { timeout: 2000 }, async (t) => {
  let signal;
  t.mock.method(globalThis, "fetch", (path, options) => {
    signal = options.signal;
    return untilAborted(signal);
  });
  await assert.rejects(api("/api/status", "GET", undefined, { timeout: 10 }), {
    message: "The service took too long to respond. Check its connection and try again.",
  });
  assert.equal(signal.aborted, true);
});

test("API timeout also covers a stalled response body", { timeout: 2000 }, async (t) => {
  let signal;
  let readingBody = false;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    signal = options.signal;
    return {
      ok: true,
      status: 200,
      headers: new Headers(),
      json() {
        readingBody = true;
        return untilAborted(signal);
      },
    };
  });
  await assert.rejects(api("/api/events/raw", "GET", undefined, { timeout: 10 }), {
    message: "The service took too long to respond. Check its connection and try again.",
  });
  assert.equal(readingBody, true);
  assert.equal(signal.aborted, true);
});

test("API exposes readable validation paths and preserves HTTP status", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({
    detail: [
      { loc: ["body", "devices", 0, "url"], msg: "A local HTTP address is required" },
      { loc: ["body", "devices", 0, "targets", 1, "brightness"], msg: "Must be at most 255" },
    ],
  }, { status: 422 }));
  await assert.rejects(api("/api/wled", "PATCH", { devices: [] }), {
    status: 422,
    message: "devices / 0 / url: A local HTTP address is required; devices / 0 / targets / 1 / brightness: Must be at most 255",
  });
});

test("API sends If-Match and returns response data with its new ETag", async (t) => {
  const body = { devices: [{ id: "ring", name: "Dartboard" }] };
  let request;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    request = { path, ...options };
    return Response.json({ saved: true }, { headers: { ETag: '"revision-2"' } });
  });
  const result = await api("/api/wled", "PATCH", body, {
    etag: '"revision-1"', metadata: true,
  });
  assert.equal(request.path, "/api/wled");
  assert.equal(request.method, "PATCH");
  assert.equal(request.headers["If-Match"], '"revision-1"');
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(request.body), body);
  assert.deepEqual(result, { data: { saved: true }, etag: '"revision-2"' });
  assert.equal(request.signal.aborted, false);
});

test("GET metadata includes ETag without adding a JSON body", async (t) => {
  let options;
  t.mock.method(globalThis, "fetch", async (path, value) => {
    options = value;
    return Response.json({ enabled: true }, { headers: { ETag: '"current"' } });
  });
  const result = await api("/api/wled", "GET", undefined, { metadata: true });
  assert.deepEqual(result, { data: { enabled: true }, etag: '"current"' });
  assert.equal(options.body, undefined);
  assert.equal(options.headers["Content-Type"], undefined);
  assert.equal(options.headers["If-Match"], undefined);
});

test("API returns plain JSON by default and null metadata for absent ETag", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ enabled: false }));
  assert.deepEqual(await api("/api/caller"), { enabled: false });
  assert.deepEqual(await api("/api/caller", "GET", undefined, { metadata: true }), {
    data: { enabled: false }, etag: null,
  });
});

test("stale ETag produces a 412 error without hiding the conflict message", async (t) => {
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(options.headers["If-Match"], '"stale"');
    return Response.json({ detail: "Settings changed in another client. Reload first." }, {
      status: 412,
    });
  });
  await assert.rejects(api("/api/wled", "PATCH", { devices: [] }, {
    etag: '"stale"', metadata: true,
  }), {
    status: 412,
    message: "Settings changed in another client. Reload first.",
  });
});

test("invalid JSON and network errors produce distinct failures", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response("unavailable", {
    status: 503,
  }));
  await assert.rejects(api("/api/status"), {
    message: "The service returned an invalid response (HTTP 503).",
  });
  const offline = new TypeError("Network unavailable");
  fetch.mock.mockImplementation(async () => { throw offline; });
  await assert.rejects(api("/api/status"), (error) => error === offline);
});

test("normalized event IDs survive rolling history and content changes", () => {
  const first = eventEntries([
    { id: "one", event: "throw", data: { points: 20 } },
    { id: "two", event: "turn_end", data: { score: 60 } },
  ], false);
  const updated = { id: "two", event: "turn_end", data: { score: 80 } };
  const second = eventEntries([updated, { id: "three", event: "player_changed" }], false, first);
  assert.equal(second[0].key, first[1].key);
  assert.equal(second[0].key, "event-two");
  assert.equal(second[1].key, "event-three");
  assert.equal(second[0].event, updated);
  assert.notEqual(second[0].signature, first[1].signature);
});

test("unchanged identical raw frames keep distinct occurrence keys", () => {
  const frames = [{ type: "ping" }, { type: "ping" }, { type: "ping" }];
  const first = eventEntries(frames, true);
  assert.equal(new Set(first.map((entry) => entry.key)).size, frames.length);
  const second = eventEntries(structuredClone(frames), true, first);
  assert.deepEqual(second.map((entry) => entry.key), first.map((entry) => entry.key));
  assert.deepEqual(second.map((entry) => entry.event), frames);
});

test("an appended duplicate raw frame receives its own key", () => {
  const frames = [{ type: "ping" }, { channel: "board", data: { count: 1 } }];
  const first = eventEntries(frames, true);
  const second = eventEntries([...frames, { type: "ping" }], true, first);
  assert.deepEqual(second.slice(0, 2).map((entry) => entry.key), first.map((entry) => entry.key));
  assert.equal(new Set(second.map((entry) => entry.key)).size, 3);
});

test("rolling raw history preserves the surviving duplicate, not the discarded occurrence", () => {
  const duplicate = { type: "ping" };
  const anchor = { channel: "board", data: { count: 1 } };
  const first = eventEntries([duplicate, anchor, duplicate], true);
  const second = eventEntries([anchor, duplicate, { type: "next" }], true, first);
  assert.equal(second[0].key, first[1].key);
  assert.equal(second[1].key, first[2].key);
  assert.ok(!first.some((entry) => entry.key === second[2].key));
  assert.equal(new Set(second.map((entry) => entry.key)).size, second.length);
});

test("the rolling 100-frame window stays bounded and retains overlapping row keys", () => {
  let frames = Array.from({ length: 100 }, (_, index) => ({ data: { index } }));
  let previous = eventEntries(frames, true);
  for (let index = 100; index < 105; index++) {
    frames = [...frames.slice(1), { data: { index } }];
    const current = eventEntries(frames, true, previous);
    assert.equal(current.length, 100);
    assert.deepEqual(
      current.slice(0, -1).map((entry) => entry.key),
      previous.slice(1).map((entry) => entry.key),
    );
    assert.ok(!previous.some((entry) => entry.key === current.at(-1).key));
    previous = current;
  }
});

test("clearing raw history does not reuse rows from an earlier recording", () => {
  const frames = [{ type: "ping" }];
  const first = eventEntries(frames, true);
  const cleared = eventEntries([], true, first);
  assert.deepEqual(cleared, []);
  const restarted = eventEntries(frames, true, cleared);
  assert.notEqual(restarted[0].key, first[0].key);
});
