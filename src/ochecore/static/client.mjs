/* Shared transport and small UI helpers. Game and device decisions stay in the service. */
export const $ = (id) => document.getElementById(id);

export async function api(path, method = "GET", body, settings = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), settings.timeout ?? 30000);
  const options = { method, signal: controller.signal, headers: {} };
  if (settings.etag) options.headers["If-Match"] = settings.etag;
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body ?? {});
  }
  try {
    const response = await fetch(path, options);
    let data;
    try {
      data = await response.json();
    } catch (cause) {
      if (controller.signal.aborted) throw cause;
      throw new Error(
        `The service returned an invalid response (HTTP ${response.status}).`,
      );
    }
    if (!response.ok) {
      const detail = data?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail
                .map((item) => `${item.loc.slice(1).join(" / ")}: ${item.msg}`)
                .join("; ")
            : `Request rejected (HTTP ${response.status}).`;
      throw Object.assign(new Error(message), { status: response.status });
    }
    return settings.metadata
      ? { data, etag: response.headers.get("ETag") }
      : data;
  } catch (cause) {
    if (controller.signal.aborted) {
      throw new Error(
        "The service took too long to respond. Check its connection and try again.",
      );
    }
    throw cause;
  } finally {
    clearTimeout(timer);
  }
}

export function poll(callback, { background = false } = {}) {
  const tick = () => {
    if (background || !document.hidden) callback();
  };
  const timer = setInterval(tick, 2000);
  document.addEventListener("visibilitychange", tick);
  tick();
  return () => {
    clearInterval(timer);
    document.removeEventListener("visibilitychange", tick);
  };
}

let nextFrame = 0;

export function eventEntries(events, raw, previous = []) {
  const signatures = events.map((event) => JSON.stringify(event));
  // The API returns a chronological sliding window. Match its retained suffix,
  // including duplicate frames, before assigning identities to new arrivals.
  let overlap = raw ? Math.min(previous.length, events.length) : 0;
  while (
    overlap &&
    !signatures
      .slice(0, overlap)
      .every(
        (signature, index) =>
          signature === previous[previous.length - overlap + index].signature,
      )
  ) {
    overlap -= 1;
  }
  return events.map((event, index) => {
    const signature = signatures[index];
    const key = raw
      ? index < overlap
        ? previous[previous.length - overlap + index].key
        : `raw-${++nextFrame}`
      : `event-${event.id}`;
    return { event, signature, key };
  });
}
