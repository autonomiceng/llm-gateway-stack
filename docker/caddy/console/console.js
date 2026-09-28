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
function parseStatus(doc) {
  if (doc?.contract !== 2 || doc.stack !== "gateway" || !Array.isArray(doc.components))
    throw new Error("Unsupported status");
  const valid = (c) => typeof c?.id === "string" && typeof c.enabled === "boolean";
  return { ...doc, components: new Map(doc.components.filter(valid).map((c) => [c.id, c])) };
}

if (typeof module !== "undefined") module.exports = { probeState, appState, componentState, versionText, originFor, parseStatus };

if (typeof document !== "undefined") {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  const COPY_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/></svg>';
  const health = {}; // Health Path id -> probe state
  let status = null,
    checking = false;

  // Live regions announce every write, so text changes only when it differs.
  const text = (element, value) => {
    if (element.textContent !== value) element.textContent = value;
  };
  const badge = (element, state) => {
    element.dataset.state = state;
    text(element, LABELS[state]);
  };
  const utc = (time) => {
    const date = new Date(time);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toISOString().slice(0, 16).replace("T", " ") + " UTC";
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
      health[id] = probeState((await request(`/health/${id}`)).status);
    } catch {
      health[id] = "unreachable";
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

  function render() {
    const components = status?.components;
    let up = 0,
      total = 0;
    for (const card of $$("[data-app]")) {
      const component = components?.get(card.dataset.app);
      const state = appState(component, card.dataset.health.split(" ").map((id) => health[id] ?? "unknown"));
      badge(card.querySelector(".pk-badge"), state);
      text(card.querySelector("[data-version]"), versionText(component));
      if (state !== "disabled") total++;
      if (state === "healthy" || state === "degraded") up++;
    }
    for (const item of $$("[data-component]")) {
      const component = components?.get(item.dataset.component);
      badge(item.querySelector(".pk-badge"), componentState(component));
      text(item.querySelector("[data-version]"), component?.version ?? "");
    }
    text($("[data-summary]"), `${up} of ${total} reachable`);
    text($("[data-configured-at]"), status ? utc(status.configuredAt) : "Status unavailable");
    const backups = status?.features?.backups;
    text(
      $("[data-backups]"),
      !backups
        ? "Unknown"
        : !backups.configured
          ? "Not configured"
          : backups.lastCheckpointAt
            ? `Configured · last checkpoint ${utc(backups.lastCheckpointAt)}`
            : "Configured · no checkpoint recorded",
    );
  }

  async function check() {
    if (checking) return;
    checking = true;
    $("[data-refresh]").disabled = true;
    text($("[data-refresh]"), "Checking…");
    const ids = new Set($$("[data-health]").flatMap((card) => card.dataset.health.split(" ")));
    try {
      await Promise.all([
        json("/status.json").then(
          (doc) => void (status = parseStatus(doc)),
          // An absent or invalid document is unknown, never a previous answer.
          () => void (status = null),
        ),
        origins().catch(() => {}),
        ...[...ids].map(probe),
      ]);
      render();
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
