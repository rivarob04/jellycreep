// Small helpers: live activity refresh, service tests, copy buttons.
(function () {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";

  const live = document.getElementById("live-streams");
  let touching = false;
  const wireHero = () => {
    const track = live && live.querySelector("[data-hero-track]");
    if (!track) return;
    const dots = [...live.querySelectorAll(".hero-dots a")];
    const sync = () => {
      const i = Math.round(track.scrollLeft / Math.max(track.clientWidth, 1));
      dots.forEach((d, n) => d.classList.toggle("on", n === i));
    };
    track.addEventListener("scroll", () => requestAnimationFrame(sync), { passive: true });
    track.addEventListener("touchstart", () => (touching = true), { passive: true });
    track.addEventListener("touchend", () => setTimeout(() => (touching = false), 400), { passive: true });
    dots.forEach((d, n) => d.addEventListener("click", (e) => {
      e.preventDefault(); track.scrollTo({ left: n * track.clientWidth, behavior: "smooth" });
    }));
    sync();
  };
  if (live) {
    wireHero();
    const refresh = async () => {
      // pause only while someone is using something in here (a menu, the stop form…), not for the
      // "Why is this converting?" box, which viewers see open by default
      if (document.hidden || touching || live.querySelector("details[open]:not(.hero-why)") ||
          (document.activeElement && live.contains(document.activeElement) && document.activeElement.matches("input, textarea, select"))) return;
      const whyOpen = [...live.querySelectorAll("details.hero-why")].map((d) => d.open);
      try {
        const r = await fetch(live.dataset.src || "/partials/activity", { headers: { "X-Requested-With": "fetch" } });
        if (r.status === 401) { location.reload(); return; }
        if (!r.ok) return;
        const old = live.querySelector("[data-hero-track]");
        const x = old ? old.scrollLeft : 0;
        live.innerHTML = await r.text();
        live.querySelectorAll("details.hero-why").forEach((d, i) => { if (i < whyOpen.length) d.open = whyOpen[i]; });
        const track = live.querySelector("[data-hero-track]");
        if (track && x) track.scrollLeft = x;
        wireHero();
      } catch (_) { /* keep last view */ }
    };
    // Live refresh: every N seconds (chosen per device), never overlapping, paused while the app is hidden.
    const ctl = document.querySelector("[data-live-ctl]");
    const sel = ctl && ctl.querySelector("select");
    const label = ctl && ctl.querySelector("[data-live-label]");
    const getEvery = () => { try { const v = localStorage.getItem("jc-live"); return v === null ? 10 : +v; } catch (_) { return 10; } };
    let every = getEvery(), timer = null, busy = false, lastAt = Date.now();
    if (sel) sel.value = String(every);
    const paint = () => {
      if (!label) return;
      const s = Math.round((Date.now() - lastAt) / 1000);
      label.textContent = every ? (s < 3 ? "Live · just updated" : `Live · updated ${s}s ago`) : "Auto-refresh off";
      ctl.classList.toggle("off", !every);
    };
    const tick = async () => {
      clearTimeout(timer);
      if (!every) { paint(); return; }
      if (!document.hidden && !busy) {
        busy = true; ctl && ctl.classList.add("busy");
        await refresh();
        busy = false; lastAt = Date.now(); ctl && ctl.classList.remove("busy");
      }
      timer = setTimeout(tick, every * 1000);
    };
    sel && sel.addEventListener("change", () => {
      every = +sel.value; try { localStorage.setItem("jc-live", String(every)); } catch (_) {}
      tick();
    });
    ctl && ctl.querySelector("[data-live-now]")?.addEventListener("click", async () => {
      busy = true; ctl.classList.add("busy"); await refresh(); busy = false; lastAt = Date.now(); ctl.classList.remove("busy"); paint();
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
    setInterval(paint, 1000);
    timer = setTimeout(tick, every * 1000);
    paint();
  }

  // Home page rows that need other apps load after the page is on screen.
  document.querySelectorAll("[data-lazy]").forEach(async (box) => {
    try {
      const r = await fetch(box.dataset.lazy, { headers: { "X-Requested-With": "fetch" } });
      if (r.status === 401) return;
      const html = r.ok ? (await r.text()).trim() : "";
      if (html) { const t = document.createElement("template"); t.innerHTML = html; box.replaceWith(t.content); }
      else box.remove();
    } catch (_) { box.remove(); }
  });

  document.querySelectorAll("[data-test]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const out = document.getElementById("status-" + btn.dataset.test);
      btn.disabled = true;
      if (out) out.textContent = "Testing…";
      try {
        const r = await fetch(`/settings/services/${btn.dataset.test}/test`, { method: "POST", headers: { "X-CSRF-Token": csrf } });
        const j = await r.json();
        if (out) {
          out.textContent = j.message || (j.ok ? "OK" : "Failed");
          out.className = "small " + (j.ok ? "" : "bad-text");
          const dot = document.getElementById("dot-" + btn.dataset.test);
          if (dot) dot.className = "dot " + (j.ok ? "ok" : "bad");
        }
      } catch (e) {
        if (out) out.textContent = "Request failed: " + e;
      } finally { btn.disabled = false; }
    });
  });

  const copy = async (text, btn) => {
    try { await navigator.clipboard.writeText(text); }
    catch (_) { // older iOS / non-https fallback
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    if (btn) { const t = btn.textContent; btn.textContent = "Copied ✓"; setTimeout(() => (btn.textContent = t), 1500); }
  };

  document.querySelectorAll("[data-copy]").forEach((btn) =>
    btn.addEventListener("click", () => copy(btn.dataset.copy, btn)));

  // Native share sheet on phones (Messages, WhatsApp…); falls back to copying.
  document.querySelectorAll("[data-share]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const url = btn.dataset.share;
      if (navigator.share) {
        try { await navigator.share({ title: btn.dataset.shareTitle || "Invite", text: btn.dataset.shareTitle || "", url }); return; }
        catch (e) { if (e.name === "AbortError") return; }
      }
      copy(url, btn);
    }));

  // Show only the proxy fields relevant to the selected mode.
  const mode = document.querySelector('select[name="proxy_auth"]');
  if (mode) {
    const sync = () => document.querySelectorAll("[data-proxy]").forEach((el) => {
      el.hidden = !el.dataset.proxy.split(" ").includes(mode.value);
    });
    mode.addEventListener("change", sync);
    sync();
  }

  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));
})();

// Live progress for the Jellystat import on the Settings page.
(function () {
  const box = document.getElementById("import-status");
  if (!box) return;
  const tick = async () => {
    try {
      const r = await fetch("/settings/import/status");
      if (!r.ok) return;
      const s = await r.json();
      if (s.message) { box.hidden = false; box.textContent = s.message + (s.imported ? ` (${s.imported} imported)` : ""); }
      box.className = "notice small" + (s.done ? " ok" : (/failed/i.test(s.message || "") ? " bad" : ""));
      if (s.running) setTimeout(tick, 1500);
    } catch (_) {}
  };
  tick();
})();

// Theme: Auto (follows the phone) → Light → Dark, remembered per device.
(function () {
  const order = ["auto", "light", "dark"];
  const label = { auto: "🌓 Auto", light: "☀️ Light", dark: "🌙 Dark" };
  const root = document.documentElement;
  const meta = document.getElementById("theme-color");
  const get = () => { try { return localStorage.getItem("jc-theme") || "auto"; } catch (_) { return "auto"; } };
  const apply = (t) => {
    if (t === "auto") delete root.dataset.theme; else root.dataset.theme = t;
    const light = t === "light" || (t === "auto" && matchMedia("(prefers-color-scheme: light)").matches);
    if (meta) meta.content = light ? "#eef0f7" : "#0a0a10";
    document.querySelectorAll("[data-theme-label]").forEach((el) => (el.textContent = el.closest(".drawer") ? "Theme: " + label[t].replace(/^\S+\s/, "") : label[t]));
  };
  apply(get());
  document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
    const next = order[(order.indexOf(get()) + 1) % order.length];
    try { localStorage.setItem("jc-theme", next); } catch (_) {}
    apply(next);
  }));
})();

// Pull to refresh (mostly for the installed home-screen app, which has no reload button).
(function () {
  if (!("ontouchstart" in window)) return;
  const ind = document.createElement("div");
  ind.className = "ptr"; ind.innerHTML = "<span>↻</span>";
  document.body.appendChild(ind);
  let y0 = null, dy = 0;
  const skip = ".posters, .prow, .seg, .fchips, .health-strip, .tabs, .table-wrap, .more-sheet, .drawer, input, textarea, select";
  window.addEventListener("touchstart", (e) => {
    if (window.scrollY > 0 || e.touches.length !== 1 || e.target.closest(skip)) { y0 = null; return; }
    y0 = e.touches[0].clientY; dy = 0;
  }, { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (y0 === null) return;
    dy = e.touches[0].clientY - y0;
    if (dy <= 0 || window.scrollY > 0) { ind.style.transform = ""; return; }
    ind.classList.add("pulling");
    ind.classList.toggle("ready", dy > 80);
    ind.style.transform = `translateY(${Math.min(dy * 0.6, 70)}px) rotate(${dy * 2}deg)`;
  }, { passive: true });
  window.addEventListener("touchend", () => {
    if (y0 === null) return;
    ind.classList.remove("pulling");
    if (dy > 80) { ind.classList.add("spin"); ind.style.transform = "translateY(56px)"; location.reload(); }
    else { ind.style.transform = ""; ind.classList.remove("ready"); }
    y0 = null; dy = 0;
  });
})();

// Invites: only show libraries for the media servers that are ticked.
(function () {
  const boxes = document.querySelectorAll('input[name="server_ids"]');
  if (!boxes.length) return;
  const sync = () => {
    const on = new Set([...boxes].filter((b) => b.checked).map((b) => b.value));
    document.querySelectorAll(".lib-group[data-server]").forEach((g) => {
      const show = on.has(g.dataset.server);
      g.hidden = !show;
      if (!show) g.querySelectorAll("input").forEach((i) => (i.checked = false));
    });
  };
  boxes.forEach((b) => b.addEventListener("change", sync));
  sync();
})();

// Glass effects on/off (per device) – off = no blur, for slower phones.
(function () {
  const root = document.documentElement;
  const get = () => { try { return localStorage.getItem("jc-fx") === "off" ? "off" : "on"; } catch (_) { return "on"; } };
  const paint = () => {
    const on = get() === "on";
    if (on) delete root.dataset.fx; else root.dataset.fx = "off";
    document.querySelectorAll("[data-fx-label]").forEach((el) => {
      el.textContent = el.closest(".drawer") ? `Glass effects: ${on ? "on" : "off"}` : el.closest(".sheet-btn") ? `✨ Glass effects: ${on ? "on" : "off"}` : (on ? "✨ Glass" : "▫️ Flat");
    });
  };
  paint();
  document.querySelectorAll("[data-fx-toggle]").forEach((b) => b.addEventListener("click", (e) => {
    e.preventDefault();
    try { localStorage.setItem("jc-fx", get() === "on" ? "off" : "on"); } catch (_) {}
    paint();
  }));
})();

// Quality scan progress: poll while scanning, reload when done.
(function () {
  const box = document.getElementById("q-status");
  if (!box) return;
  const bar = document.getElementById("q-bar");
  let wasRunning = !box.hidden;
  const tick = async () => {
    try {
      const r = await fetch("/media/quality/status");
      if (!r.ok) return;
      const s = await r.json();
      if (s.running) {
        box.hidden = false; box.textContent = s.message || "Scanning…";
        if (bar && s.total) bar.style.width = Math.max(3, (s.done / s.total) * 100) + "%";
        wasRunning = true; setTimeout(tick, 1500);
      } else if (wasRunning) { location.reload(); }
    } catch (_) {}
  };
  if (wasRunning || document.getElementById("q-bar")) tick();
})();

// Seerr season picker: "All seasons" toggles every free season
document.querySelectorAll("[data-seasons]").forEach((form) => {
  const all = form.querySelector("[data-all]");
  const boxes = [...form.querySelectorAll('input[name="season"]:not([disabled])')];
  const sync = () => { all.checked = boxes.every((b) => b.checked); };
  all?.addEventListener("change", () => boxes.forEach((b) => (b.checked = all.checked)));
  boxes.forEach((b) => b.addEventListener("change", sync));
});


// Slide-out menu: hamburger, tap outside / Esc to close, swipe in from the left edge, swipe left to close.
(function () {
  const root = document.documentElement, drawer = document.getElementById("drawer");
  if (!drawer) return;
  const scrim = document.querySelector(".drawer-scrim"), btn = document.querySelector("[data-drawer-open]");
  const isOpen = () => root.classList.contains("drawer-open");
  const set = (open) => {
    root.classList.toggle("drawer-open", open);
    drawer.setAttribute("aria-hidden", open ? "false" : "true");
    btn && btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { const f = drawer.querySelector("a.on") || drawer.querySelector("a"); f && f.focus({ preventScroll: true }); }
  };
  btn && btn.addEventListener("click", () => set(true));
  scrim && scrim.addEventListener("click", () => set(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) { set(false); btn && btn.focus(); } });
  drawer.addEventListener("click", (e) => { if (e.target.closest("a")) set(false); });
  window.addEventListener("pageshow", () => set(false));     // closed again when coming back with the back button

  if (!("ontouchstart" in window)) return;
  let x0 = null, y0 = 0, dx = 0, mode = null, w = 0;
  const EDGE = 28;
  window.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1 || matchMedia("(min-width: 1000px)").matches) return;
    const t = e.touches[0];
    if (isOpen()) mode = "close";
    else if (t.clientX <= EDGE) mode = "open";
    else { mode = null; return; }
    x0 = t.clientX; y0 = t.clientY; dx = 0; w = drawer.offsetWidth || 300;
  }, { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (x0 === null || !mode) return;
    const t = e.touches[0];
    dx = t.clientX - x0;
    if (Math.abs(t.clientY - y0) > Math.abs(dx) && Math.abs(dx) < 12) return;   // vertical scroll, leave it
    if (e.cancelable) e.preventDefault();   // the menu wins over carousels / back-swipe while dragging it
    const pos = mode === "open" ? Math.min(0, -w + Math.max(0, dx)) : Math.min(0, dx);
    drawer.classList.add("dragging"); scrim.classList.add("dragging");
    drawer.style.visibility = "visible";
    drawer.style.transform = `translateX(${pos}px)`;
    scrim.style.opacity = String(1 + pos / w); scrim.style.pointerEvents = "auto";
  }, { passive: false });
  const finish = () => {
    if (x0 === null || !mode) return;
    drawer.classList.remove("dragging"); scrim.classList.remove("dragging");
    drawer.style.transform = ""; drawer.style.visibility = ""; scrim.style.opacity = ""; scrim.style.pointerEvents = "";
    if (mode === "open" && dx > 60) set(true);
    else if (mode === "close" && dx < -60) set(false);
    x0 = null; mode = null;
  };
  window.addEventListener("touchend", finish);
  window.addEventListener("touchcancel", finish);
})();

// Phone / browser notifications (Web Push)
(function () {
  const csrf = () => (document.querySelector('meta[name="csrf"]') || {}).content || "";
  const post = (url, body) => fetch(url, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() }, body: JSON.stringify(body || {}) });
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const viewer = location.pathname.startsWith("/me");
  const base = viewer ? "/me" : "";
  const swUrl = base + "/sw.js", scope = base + "/";
  const reg = () => supported ? navigator.serviceWorker.getRegistration(scope) : Promise.resolve(null);

  // Signing out on a shared phone: stop this device getting the previous person's notifications.
  document.querySelectorAll('a[href="/logout"], a[href="/auth/logout"]').forEach((a) => a.addEventListener("click", async (e) => {
    if (!supported) return;
    e.preventDefault();
    try {
      const r = await reg(); const sub = r && await r.pushManager.getSubscription();
      if (sub) { await post(base + "/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
    } catch (_) {}
    location.href = a.href;
  }));

  const box = document.getElementById("push");
  if (!box) return;
  const $ = (s) => box.querySelector(s);
  const on = $("[data-push-on]"), off = $("[data-push-off]"), test = $("[data-push-test]");
  const show = (icon, title, text, state) => {
    $("[data-push-ico]").textContent = icon; $("[data-push-title]").textContent = title; $("[data-push-text]").textContent = text || "";
    on.hidden = state !== "off"; off.hidden = test.hidden = state !== "on"; box.classList.toggle("on", state === "on");
  };
  const key = (b64) => { const p = "=".repeat((4 - b64.length % 4) % 4); const raw = atob((b64 + p).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0)); };
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent), standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;

  async function refresh() {
    if (!window.isSecureContext) return show("⚠️", "Needs HTTPS", "Open JellyCreep through its https:// address to use notifications.", "none");
    if (!supported) return show("📵", ios && !standalone ? "Add JellyCreep to your Home Screen first" : "This browser can't do notifications",
      ios && !standalone ? "On iPhone, tap Share → Add to Home Screen, open JellyCreep from there, then come back here." : "Try Chrome on Android, or Chrome, Edge, Firefox or Safari on a computer.", "none");
    if (Notification.permission === "denied") return show("🚫", "Notifications are blocked", "Allow them in this site's settings (🔒 in the address bar → Notifications), then reload.", "none");
    const r = await reg(); const sub = r && await r.pushManager.getSubscription();
    if (sub) show("🔔", "On for this device", "You'll get the alerts ticked below.", "on");
    else show("🔕", "Off for this device", "Turn on to get alerts here.", "off");
  }

  on.addEventListener("click", async () => {
    on.disabled = true;
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { await refresh(); return; }
      const r = await navigator.serviceWorker.register(swUrl, { scope });
      await navigator.serviceWorker.ready;
      let sub = await r.pushManager.getSubscription();
      if (!sub) sub = await r.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key(box.dataset.vapid) });
      const res = await post(base + "/push/subscribe", { subscription: sub.toJSON(), device: navigator.userAgent });
      if (!res.ok) throw new Error((await res.text()).slice(0, 200));
      location.reload();
    } catch (err) {
      show("⚠️", "Couldn't turn notifications on", String(err && err.message || err), "off");
    } finally { on.disabled = false; }
  });
  off.addEventListener("click", async () => {
    const r = await reg(); const sub = r && await r.pushManager.getSubscription();
    if (sub) { await post(base + "/push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
    location.reload();
  });
  test.addEventListener("click", async () => {
    test.disabled = true; const t = test.textContent; test.textContent = "Sending…";
    const res = await post(base + "/push/test").then((x) => x.json()).catch(() => ({}));
    test.textContent = res.delivered ? "Sent ✓" : "Couldn't send"; setTimeout(() => { test.textContent = t; test.disabled = false; }, 2500);
  });
  document.querySelectorAll("[data-push-remove]").forEach((b) => b.addEventListener("click", async () => {
    await post(base + "/push/unsubscribe", { id: b.dataset.pushRemove });
    const r = await reg(); const sub = r && await r.pushManager.getSubscription();
    if (sub && b.closest("[data-device-endpoint]").dataset.deviceEndpoint === sub.endpoint) await sub.unsubscribe();
    location.reload();
  }));
  refresh();
})();
