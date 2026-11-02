// Small helpers: live activity refresh, service tests, copy buttons.
(function () {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";

  const live = document.getElementById("live-streams");
  if (live) {
    const refresh = async () => {
      if (document.hidden || live.querySelector("details[open]")) return;
      try {
        const r = await fetch("/partials/activity", { headers: { "X-Requested-With": "fetch" } });
        if (r.ok) live.innerHTML = await r.text();
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
