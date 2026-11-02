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
      if (document.hidden || touching || live.querySelector("details[open]")) return;
      try {
        const r = await fetch("/partials/activity", { headers: { "X-Requested-With": "fetch" } });
        if (!r.ok) return;
        const old = live.querySelector("[data-hero-track]");
        const x = old ? old.scrollLeft : 0;
        live.innerHTML = await r.text();
        const track = live.querySelector("[data-hero-track]");
        if (track && x) track.scrollLeft = x;
        wireHero();
      } catch (_) { /* keep last view */ }
    };
    setInterval(refresh, 5000);
  }

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
    document.querySelectorAll("[data-theme-label]").forEach((el) => (el.textContent = label[t]));
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
  const skip = ".posters, .seg, .health-strip, .tabs, .table-wrap, .more-sheet, input, textarea, select";
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
      el.textContent = el.closest(".sheet-btn") ? `✨ Glass effects: ${on ? "on" : "off"}` : (on ? "✨ Glass" : "▫️ Flat");
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
