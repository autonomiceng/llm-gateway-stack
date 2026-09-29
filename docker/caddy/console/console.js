// Stack Console. Reads /status.json (Status v2: configured images and enabled state),
// /origins.json (browser origins) and probes /health/<id> through the Stack Gateway.
// Badge states and labels follow platform-edge docs/ui-kit.md. No secrets, no writes.
const LABELS = {
  healthy: "Healthy",
  degraded: "Degraded",
  unreachable: "Unreachable",
  unknown: "Unknown",
  configured: "Configured",
  disabled: "Disabled",
};

const probeState = (code) => (code === 200 ? "healthy" : [502, 503, 504].includes(code) ? "unreachable" : "unknown");
// Configuration comes only from the Status Document and reachability only from Health Paths:
// without an enabled entry an application stays Unknown, whatever its probes say.
function appState(component, probes) {
  if (!component) return "unknown";
  if (!component.enabled) return "disabled";
  const up = probes.filter((p) => p === "healthy").length;
  const down = probes.filter((p) => p === "unreachable").length;
  if (up === probes.length) return "healthy";
  if (down === probes.length) return "unreachable";
  return up && down ? "degraded" : "unknown";
}
const componentState = (component) => (!component ? "unknown" : component.enabled ? "configured" : "disabled");
const versionText = (component) =>
  !component ? "Version unknown" : component.version ? `Configured ${component.version}` : "Configured";
const originFor = (origins, name) => origins[name] || `${origins.scheme}://${name}.${origins.domain}${origins.port}`;
const utc = (time) => {
  const date = new Date(time);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toISOString().slice(0, 16).replace("T", " ") + " UTC";
};
const backupsText = (backups) =>
  !backups
    ? "Unknown"
    : !backups.configured
      ? "Not configured"
      : backups.lastCheckpointAt
        ? `Configured · last checkpoint ${utc(backups.lastCheckpointAt)}`
        : "Configured · no checkpoint recorded";
function summaryText(status, states) {
  if (!status) return "Status unavailable";
  const up = states.filter((state) => state === "healthy" || state === "degraded").length;
  const down = states.filter((state) => state === "unreachable").length;
  const unknown = states.filter((state) => state === "unknown").length;
  return [up + down && `${up} of ${up + down} reachable`, unknown && `${unknown} unknown`]
    .filter(Boolean).join(" · ") || "Nothing enabled";
}
// Status v2 closes the field set at every level; an unknown field rejects the document.
const ENVELOPE = ["contract", "stack", "configuredAt", "components", "features"];
const FIELDS = ["id", "name", "kind", "enabled", "image", "version", "health", "url"];
const FEATURES = { backups: ["configured", "lastCheckpointAt"], alerts: ["configured"] };
const MAX_STATUS_BYTES = 65536;
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const only = (value, keys) => !object(value) || Object.keys(value).every((key) => keys.includes(key));
const text = (value, max) => typeof value === "string" && value.length > 0 && value.length <= max;
function timestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)$/.test(value))
    return NaN;
  const time = Date.parse(value);
  // Date.parse silently normalizes nonexistent calendar dates.
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 19) === value.slice(0, 19) ? time : NaN;
}
const feature = (value, fields, valid) =>
  object(value) && fields.every((key) => Object.hasOwn(value, key)) && valid(value) ? value : undefined;
function origin(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && url.pathname === "/" &&
      !url.username && !url.password &&
      !url.search && !url.hash && !value.includes("?") && !value.includes("#");
  } catch {
    return false;
  }
}
const validComponent = (c) => object(c) &&
  typeof c.id === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(c.id) &&
  text(c.name, 64) &&
  ["app", "datastore", "gateway", "collector", "runtime"].includes(c.kind) &&
  typeof c.enabled === "boolean" &&
  text(c.image, 256) && !c.image.includes("@") &&
  (c.version === null || (typeof c.version === "string" && /^[A-Za-z0-9._+-]{1,128}$/.test(c.version))) &&
  c.health === `/health/${c.id}` &&
  (!Object.hasOwn(c, "url") || (text(c.url, 2048) && origin(c.url)));
function parseStatus(doc) {
  if (
    !object(doc) ||
    doc.contract !== 2 ||
    doc.stack !== "gateway" ||
    !ENVELOPE.every((key) => Object.hasOwn(doc, key)) ||
    !only(doc, ENVELOPE) ||
    !Number.isFinite(timestamp(doc.configuredAt)) ||
    !Array.isArray(doc.components) ||
    doc.components.length > 32 ||
    !object(doc.features) ||
    !only(doc.features, Object.keys(FEATURES)) ||
    Object.entries(doc.features).some(([key, value]) => object(value) && !only(value, FEATURES[key])) ||
    doc.components.some((component) => !only(component, FIELDS))
  )
    throw new Error("Unsupported status");
  const ids = doc.components.map((component) => component?.id).filter((id) => typeof id === "string");
  if (new Set(ids).size !== ids.length) throw new Error("Duplicate component");
  const backups = feature(doc.features.backups, FEATURES.backups,
    (value) => typeof value.configured === "boolean" &&
      (value.lastCheckpointAt === null || Number.isFinite(timestamp(value.lastCheckpointAt))));
  const alerts = feature(doc.features.alerts, FEATURES.alerts, (value) => typeof value.configured === "boolean");
  return {
    ...doc,
    components: new Map(doc.components.filter(validComponent).map((c) => [c.id, c])),
    features: { ...(backups && { backups }), ...(alerts && { alerts }) },
  };
}
async function readStatus(response) {
  if (response.status !== 200 ||
    !/^application\/json(?:\s*;|\s*$)/i.test(response.headers.get("Content-Type") || ""))
    throw new Error("Status unavailable");
  if (Number(response.headers.get("Content-Length")) > MAX_STATUS_BYTES) {
    void response.body?.cancel().catch(() => {});
    throw new Error("Status too large");
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_STATUS_BYTES) throw new Error("Status too large");
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } finally {
    void reader.cancel().catch(() => {});
  }
}
async function getStatus(fetcher = fetch) {
  const response = await fetcher("/status.json", {
    cache: "no-store", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(4000),
  });
  return readStatus(response);
}
// Each refresh starts empty. A missing or malformed document schedules no Health Path probes.
async function load(getStatus, probe, cards) {
  let status = null;
  try {
    status = parseStatus(await getStatus());
  } catch {}
  const ids = [...new Set(cards.filter((card) => status?.components.get(card.id)?.enabled)
    .flatMap((card) => card.health))];
  return { status, health: Object.fromEntries(await Promise.all(ids.map(async (id) => [id, await probe(id)]))) };
}

if (typeof module !== "undefined")
  module.exports = { probeState, appState, componentState, versionText, originFor, backupsText, summaryText, parseStatus, readStatus, getStatus, load };

if (typeof document !== "undefined") {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  const COPY_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/></svg>';
  let checking = false;

  // Live regions announce every write, so text changes only when it differs.
  const text = (element, value) => {
    if (element.textContent !== value) element.textContent = value;
  };
  const badge = (element, state) => {
    element.dataset.state = state;
    text(element, LABELS[state]);
  };
  const request = (path) =>
    fetch(path, { cache: "no-store", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(4000) });
  async function json(path) {
    const response = await request(path);
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }
  async function probe(id) {
    try {
      return probeState((await request(`/health/${id}`)).status);
    } catch {
      return "unreachable";
    }
  }
  async function origins() {
    const value = await json("/origins.json");
    for (const link of $$("[data-link]")) link.href = originFor(value, link.dataset.link) + link.dataset.path;
    for (const code of $$("[data-url]")) {
      code.textContent = originFor(value, code.dataset.url) + (code.dataset.path || "");
      code.nextElementSibling.disabled = false;
    }
    // The Edge console is the platform home; a standalone stack shows no link.
    let platform = $("[data-platform]");
    if (/^https?:\/\/[^/]+$/.test(value.platform || "")) {
      if (!platform) {
        platform = Object.assign(document.createElement("a"), { textContent: "Platform" });
        platform.dataset.platform = "";
        $(".pk-header-links").prepend(platform);
      }
      platform.href = value.platform + "/";
    } else platform?.remove();
  }

  function render(status, health) {
    const components = status?.components;
    const states = [];
    for (const card of $$("[data-app]")) {
      const component = components?.get(card.dataset.app);
      const state = appState(component, card.dataset.health.split(" ").map((id) => health[id] ?? "unknown"));
      badge(card.querySelector(".pk-badge"), state);
      text(card.querySelector("[data-version]"), versionText(component));
      if (component?.enabled) states.push(state);
    }
    for (const item of $$("[data-component]")) {
      const component = components?.get(item.dataset.component);
      badge(item.querySelector(".pk-badge"), componentState(component));
      text(item.querySelector("[data-version]"), component?.version ?? "");
    }
    text($("[data-summary]"), summaryText(status, states));
    text($("[data-configured-at]"), status ? utc(status.configuredAt) : "Status unavailable");
    text($("[data-backups]"), backupsText(status?.features?.backups));
  }

  async function check() {
    if (checking) return;
    checking = true;
    $("[data-refresh]").disabled = true;
    text($("[data-refresh]"), "Checking…");
    try {
      const [{ status, health }] = await Promise.all([
        load(getStatus, probe,
          $$("[data-app]").map((card) => ({ id: card.dataset.app, health: card.dataset.health.split(" ") }))),
        origins().catch(() => {}),
      ]);
      render(status, health);
      text($("[data-checked]"), `Checked ${new Date().toLocaleTimeString()}`);
    } finally {
      $("[data-refresh]").disabled = false;
      text($("[data-refresh]"), "Refresh");
      checking = false;
    }
  }

  for (const copy of $$(".pk-copy")) copy.innerHTML = COPY_ICON;
  text($("[data-scheme]"), location.protocol === "https:" ? "HTTPS" : "HTTP");
  document.addEventListener("click", async (event) => {
    if (event.target.closest("[data-refresh]")) return check();
    const copy = event.target.closest(".pk-copy");
    if (!copy) return;
    const value = copy.previousElementSibling.textContent;
    try {
      await navigator.clipboard.writeText(value);
      $("[data-announce]").textContent = `Copied ${value}`;
      copy.dataset.copied = "";
      setTimeout(() => delete copy.dataset.copied, 1500);
    } catch {
      $("[data-announce]").textContent = "Copy failed. Select the endpoint text instead.";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) check();
  });
  // Repaint only when a check completes; no continuous animation.
  setInterval(() => {
    if (!document.hidden) check();
  }, 30000);
  check();
}
