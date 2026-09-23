// Stack Console. Reads /status.json (Status v2, configured images written at bootstrap) and
// polls /health/<service> through the Stack Gateway. No secrets, no writes.
(() => {
  const base = `${location.protocol}//${location.host}`;
  for (const code of document.querySelectorAll('[data-url]')) {
    const cell = code.parentElement; cell.classList.add('endpoint');
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'copy';
    copy.innerHTML = "<svg viewBox=\"0 0 24 24\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.6\" aria-hidden=\"true\"><rect x=\"8\" y=\"8\" width=\"12\" height=\"12\" rx=\"2\"/><path d=\"M16 8V4H4v12h4\"/></svg>";
    copy.setAttribute('aria-label', 'Copy ' + code.closest('[data-service]').querySelector('h2').textContent.trim() + ' endpoint');
    copy.title = 'Copy endpoint';
    const status = document.createElement('span'); status.className = 'copy-status'; status.setAttribute('role', 'status');
    let timer;
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(code.textContent); status.textContent = 'Copied'; }
      catch (_) { status.textContent = 'Select the address to copy'; }
      clearTimeout(timer); timer = setTimeout(() => { status.textContent = ''; }, 3000);
    });
    copy.disabled = true;
    cell.append(copy, status);
  }
  const origins = async () => {
    const response = await fetch("/origins.json", { cache: "no-store", signal: AbortSignal.timeout(4000) });
    if (!response.ok) throw new Error(String(response.status));
    const origin = await response.json();
    const consoleLink = document.querySelector('[data-rustfs-console]');
    const consoleEnabled = origin.rustfsConsole === 'on';
    document.querySelector('[data-rustfs-disabled]').hidden = consoleEnabled;
    const hostFor = (sub) => origin[sub] || `${origin.scheme}://${sub}.${origin.domain}${origin.port}`;
    for (const link of document.querySelectorAll("[data-link]")) {
      link.dataset.targetUrl = hostFor(link.dataset.link) + (link.dataset.path || "/");
      if (link === consoleLink && !consoleEnabled) {
        link.removeAttribute("href");
        link.setAttribute("aria-disabled", "true");
        continue;
      }
      const optional = link.closest?.("[data-optional]");
      if (!optional || optional.dataset.ready === "true") {
        link.href = link.dataset.targetUrl;
        link.removeAttribute("aria-disabled");
      }
    }
    for (const code of document.querySelectorAll("[data-url]")) {
      code.textContent = hostFor(code.dataset.url) + (code.dataset.path || "");
      code.parentElement.querySelector(".copy").disabled = false;
    }
  };

  const badge = (li, state, label) => {
    const b = li.querySelector("[data-badge]");
    b.dataset.state = state;
    b.textContent = label;
    if (li.hasAttribute("data-optional")) {
      li.dataset.ready = String(state === "ok");
      for (const link of li.querySelectorAll("[data-link]")) {
        if (state === "ok" && link.dataset.targetUrl) {
          link.href = link.dataset.targetUrl;
          link.removeAttribute("aria-disabled");
        } else {
          link.removeAttribute("href");
          link.setAttribute("aria-disabled", "true");
        }
      }
    }
  };
  // A service this browser has seen healthy is "down" when it stops answering;
  // one it has never seen is "not installed". Optional cards only.
  const seenKey = (s) => `console.seen.${s}`;
  const seenThisPage = new Set();
  const seen = (s) => {
    if (seenThisPage.has(s)) return true;
    try { return localStorage.getItem(seenKey(s)) !== null; }
    catch { return false; }
  };
  const remember = (s) => {
    seenThisPage.add(s);
    try { localStorage.setItem(seenKey(s), new Date().toISOString()); }
    catch { /* Browser policy can disable persistent storage. */ }
  };

  const check = async (li) => {
    const service = li.dataset.service;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 4000);
    try {
      const res = await fetch(`${base}/health/${service}`, { signal: ctl.signal, cache: "no-store" });
      if (res.ok) { remember(service); return badge(li, "ok", "healthy"); }
      if (res.status === 502 || res.status === 503) {
        const absent = li.hasAttribute("data-optional") && !seen(service);
        return badge(li, absent ? "absent" : "down", absent ? "not installed" : "unreachable");
      }
      badge(li, "degraded", `http ${res.status}`);
    } catch (err) {
      badge(li, err.name === "AbortError" ? "stale" : "down",
        err.name === "AbortError" ? "no answer" : "unreachable");
    } finally {
      clearTimeout(timer);
    }
  };

  const checkAll = async () => {
    await origins().catch(() => {});
    await Promise.all([...document.querySelectorAll("[data-service]")].map(check));
    const t = new Date();
    document.querySelector("[data-checked]").textContent =
      `Health checked ${t.toLocaleTimeString()}.`;
  };

  const versions = async () => {
    try {
      const res = await fetch(`${base}/status.json`, { cache: "no-store", signal: AbortSignal.timeout(4000) });
      if (!res.ok) throw new Error(String(res.status));
      const v = await res.json();
      if (v.contract !== 2) throw new Error("unsupported status contract");
      const components = new Map((Array.isArray(v.components) ? v.components : []).map((c) => [c.id, c]));
      for (const el of document.querySelectorAll("[data-version]")) {
        const c = components.get(el.dataset.version);
        el.textContent = c?.enabled === false ? "disabled" : (c?.version ?? "unknown");
      }
      const when = document.querySelector("[data-pinned]");
      when.dateTime = v.configuredAt ?? "";
      when.textContent = v.configuredAt ? new Date(v.configuredAt).toLocaleString() : "unknown";
    } catch {
      for (const el of document.querySelectorAll("[data-version]")) el.textContent = "unknown";
      document.querySelector("[data-pinned]").textContent = "unknown";
    }
  };

  versions();
  checkAll();
  // Repaint only when a check completes; no continuous animation.
  setInterval(checkAll, 30000);
})();
