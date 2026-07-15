const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) {
    const msg = data.detail || data.message || data.error || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(msg, type = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function pill(status) {
  const s = (status || "unknown").toLowerCase();
  return `<span class="pill ${s}">${s}</span>`;
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── tabs ─────────────────────────────────────────────────────
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const name = tab.dataset.tab;
    $("#panel-batch").hidden = name !== "batch";
    $("#panel-single").hidden = name !== "single";
  });
});

// ── image2video ref image ────────────────────────────────────
function syncRefImagePanel() {
  const mode = $("#job-mode")?.value || "text2video";
  const panel = $("#ref-image-panel");
  if (panel) panel.hidden = mode !== "image2video";
}

$("#job-mode")?.addEventListener("change", syncRefImagePanel);
syncRefImagePanel();

$("#ref-image-file")?.addEventListener("change", async (ev) => {
  const file = ev.target.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch("/api/upload/image", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    $("#ref-image-path").value = data.image_path || data.path || "";
    $("#ref-image-url").value = ""; // prefer uploaded path
    const wrap = $("#ref-image-preview-wrap");
    const img = $("#ref-image-preview");
    const meta = $("#ref-image-meta");
    wrap.hidden = false;
    img.src = data.preview_url + "?t=" + Date.now();
    meta.textContent = `${data.filename} · ${fmtSize(data.size)} · saved`;
    toast("Đã upload ảnh tham chiếu");
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-clear-ref")?.addEventListener("click", () => {
  $("#ref-image-path").value = "";
  $("#ref-image-url").value = "";
  if ($("#ref-image-file")) $("#ref-image-file").value = "";
  const wrap = $("#ref-image-preview-wrap");
  if (wrap) wrap.hidden = true;
  if ($("#ref-image-preview")) $("#ref-image-preview").src = "";
});

function getRefImagePayload() {
  const mode = $("#job-mode").value;
  if (mode !== "image2video") {
    return { image_url: "", image_path: "" };
  }
  return {
    image_path: ($("#ref-image-path").value || "").trim(),
    image_url: ($("#ref-image-url").value || "").trim(),
  };
}

// ── stats / accounts / jobs ──────────────────────────────────
async function refreshStats() {
  const s = await api("/api/stats");
  const j = s.jobs || {};
  $("#s-queued").textContent = j.queued || 0;
  $("#s-running").textContent = (j.running || 0) + (j.polling || 0);
  $("#s-success").textContent = j.success || 0;
  $("#s-failed").textContent = j.failed || 0;
  $("#badge-worker").textContent = `worker: ${s.worker ? "on" : "off"}`;
  $("#badge-worker").className = `badge ${s.worker ? "ok" : "warn"}`;
  const live = s.live === true || s.demo_mode === false;
  $("#badge-demo").textContent = live ? "● LIVE PRODUCTION" : "mode: DEMO";
  $("#badge-demo").className = `badge ${live ? "ok" : "warn"}`;
  $("#badge-accounts").textContent = `accounts: ${s.accounts_ok || 0}/${s.accounts || 0}`;
  if (s.accounts_expired) {
    $("#badge-accounts").textContent += ` · expired ${s.accounts_expired}`;
  }
}

async function refreshAccounts() {
  const rows = await api("/api/accounts");
  const sel = $("#job-account");
  const cur = sel.value;
  sel.innerHTML = `<option value="">Auto</option>` + rows
    .map((a) => `<option value="${a.id}">${esc(a.label)} (${esc(a.status)})</option>`)
    .join("");
  sel.value = cur;

  const list = $("#account-list");
  if (!rows.length) {
    list.innerHTML = `<div class="empty">Chưa có account. Dán cookie sau khi login Gmail trên dola.com.</div>`;
    return;
  }
  list.innerHTML = rows.map((a) => {
    const q = a.quota_display || {};
    const hasQ = q.has_quota !== false && (q.left === null || q.left === undefined || q.left > 0);
    const qPill = hasQ
      ? `<span class="pill ok">quota ${q.left ?? "?"}${q.total != null ? "/" + q.total : ""}</span>`
      : `<span class="pill err">hết quota</span>`;
    return `
    <div class="item" data-id="${a.id}">
      <div class="item-top">
        <div>
          <div class="item-title">${esc(a.label)} ${pill(a.status)} ${qPill}</div>
          <div class="item-sub">
            ${esc(a.email || "—")} · used ${a.daily_used ?? 0}
            · ${a.enabled ? "enabled" : "disabled"}
            ${q.date ? ` · ngày ${esc(q.date)}` : ""}
            ${q.note ? ` · <span style="opacity:.85">${esc(String(q.note).slice(0, 80))}</span>` : ""}
            ${a.last_error ? ` · <span style="color:var(--err)">${esc(a.last_error)}</span>` : ""}
          </div>
        </div>
        <div class="item-actions">
          <button class="btn ghost" data-act="check">Session</button>
          <button class="btn ghost" data-act="quota">Quota</button>
          <button class="btn ghost" data-act="reset">Reset local</button>
          <button class="btn ghost" data-act="toggle">${a.enabled ? "Disable" : "Enable"}</button>
          <button class="btn danger" data-act="del">Xoá</button>
        </div>
      </div>
    </div>`;
  }).join("");

  list.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.closest(".item").dataset.id;
      const act = btn.dataset.act;
      try {
        if (act === "check") {
          await api(`/api/accounts/${id}/check`, { method: "POST" });
          toast("Đã check session");
        } else if (act === "quota") {
          const r = await api(`/api/accounts/${id}/check-quota`, { method: "POST" });
          const q = r.quota || {};
          toast(`Quota: left=${q.quota_left ?? "?"} · ${q.has_quota ? "còn" : "hết"}`);
        } else if (act === "reset") {
          await api(`/api/accounts/${id}/reset-quota`, { method: "POST" });
          toast("Reset local quota");
        } else if (act === "toggle") {
          const acc = rows.find((x) => x.id === id);
          await api(`/api/accounts/${id}`, {
            method: "PATCH",
            body: JSON.stringify({ enabled: !acc.enabled }),
          });
        } else if (act === "del") {
          if (!confirm("Xoá account?")) return;
          await api(`/api/accounts/${id}`, { method: "DELETE" });
          toast("Đã xoá");
        }
        await refreshAll();
      } catch (e) {
        toast(e.message, "err");
      }
    });
  });
}

function fmtSize(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function refreshJobs() {
  const jobs = await api("/api/jobs?limit=100");
  const list = $("#job-list");
  if (!jobs.length) {
    list.innerHTML = `<div class="empty">Chưa có job. Thêm prompt và submit.</div>`;
    return;
  }
  // remember playing videos to avoid full remount flicker when possible
  list.innerHTML = jobs.map((j) => {
    const pct = Math.min(100, Math.round(j.progress || 0));
    const hasVideo = j.has_video || (j.status === "success" && j.local_path);
    const stream = j.stream_url || (hasVideo ? `/api/jobs/${j.id}/stream` : "");
    const dl = j.download_url || (hasVideo ? `/api/jobs/${j.id}/download` : "");
    return `
      <div class="item" data-id="${j.id}">
        <div class="item-top">
          <div style="flex:1;min-width:0">
            <div class="item-title">${pill(j.status)} <span style="font-weight:500">${esc((j.prompt || "").slice(0, 90))}${(j.prompt||"").length>90?"…":""}</span></div>
            <div class="item-sub">
              #${esc(j.id)} · ${esc(j.mode)} · ${esc(j.ratio)} · ${esc(j.duration)}s
              ${j.account_id ? ` · acc ${esc(j.account_id)}` : ""}
              ${j.video_size ? ` · ${fmtSize(j.video_size)}` : ""}
              ${j.image_path || j.image_url ? " · 🖼 ref image" : ""}
              ${j.error ? ` · <span style="color:var(--err)">${esc(j.error)}</span>` : ""}
            </div>
            <div class="bar"><i style="width:${pct}%"></i></div>
            ${j.image_path ? (() => {
              const name = String(j.image_path).split(/[/\\\\]/).pop();
              return name ? `<img class="job-thumb" src="/api/uploads/${esc(name)}" alt="ref" />` : "";
            })() : (j.image_url && !String(j.image_url).startsWith("data:")
              ? `<img class="job-thumb" src="${esc(j.image_url)}" alt="ref" />` : "")}
            ${hasVideo && stream ? `
              <div class="job-video">
                <video controls playsinline preload="metadata" src="${esc(stream)}"></video>
              </div>` : ""}
          </div>
          <div class="item-actions">
            ${hasVideo && dl ? `<a class="btn primary" href="${esc(dl)}" download>Download</a>` : ""}
            ${j.video_url ? `<a class="btn" href="${esc(j.video_url)}" target="_blank" rel="noopener">CDN</a>` : ""}
            ${["failed","success"].includes(j.status) ? `<button class="btn ghost" data-act="retry">Retry</button>` : ""}
            <button class="btn danger" data-act="del">Xoá</button>
          </div>
        </div>
      </div>
    `;
  }).join("");

  list.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.closest(".item").dataset.id;
      try {
        if (btn.dataset.act === "retry") {
          await api(`/api/jobs/${id}/retry`, { method: "POST" });
          toast("Re-queued");
        } else if (btn.dataset.act === "del") {
          await api(`/api/jobs/${id}`, { method: "DELETE" });
        }
        await refreshAll();
      } catch (e) {
        toast(e.message, "err");
      }
    });
  });
}

async function refreshGallery() {
  const el = $("#gallery");
  if (!el) return;
  const items = await api("/api/gallery?limit=48");
  if (!items.length) {
    el.innerHTML = `<div class="empty">Chưa có video local. Gen xong worker sẽ tự download và hiện ở đây.</div>`;
    return;
  }
  el.innerHTML = items.map((v) => `
    <div class="gallery-card" data-id="${esc(v.id)}">
      <video controls playsinline preload="metadata" src="${esc(v.stream_url)}"></video>
      <div class="gallery-meta">
        <strong title="${esc(v.prompt)}">${esc((v.prompt || "").slice(0, 60))}${(v.prompt||"").length>60?"…":""}</strong>
        #${esc(v.id)} · ${esc(v.ratio)} · ${fmtSize(v.size_bytes)}
      </div>
      <div class="gallery-actions">
        <a class="btn primary" href="${esc(v.download_url)}" download>Download</a>
      </div>
    </div>
  `).join("");
}

async function refreshAll() {
  await Promise.all([
    refreshStats(),
    refreshAccounts(),
    refreshJobs(),
    refreshGallery().catch(() => {}),
  ]);
}

// ── actions ──────────────────────────────────────────────────
function setLoginStatus(text, kind = "running") {
  const el = $("#login-status");
  el.hidden = !text;
  el.className = `login-status ${kind}`;
  el.innerHTML = kind === "running"
    ? `<span class="spin"></span>${esc(text)}`
    : esc(text);
}

let loginPollTimer = null;

$("#btn-add-acc").addEventListener("click", async () => {
  const label = $("#acc-label").value.trim() || `acc-${Date.now().toString(36)}`;
  const cookies = $("#acc-cookies").value.trim();
  const email = $("#acc-email").value.trim();
  const daily_limit = Number($("#acc-limit").value || 2);
  if (!cookies) return toast("Cần cookies (chỉ dùng fallback)", "err");
  try {
    await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ label, cookies, email, daily_limit }),
    });
    $("#acc-cookies").value = "";
    toast("Đã thêm account");
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  }
});

let assistPollTimer = null;

$("#btn-assisted-login").addEventListener("click", async () => {
  const count = Number($("#assist-count").value || 1);
  const label_prefix = ($("#assist-prefix").value || "acc").trim() || "acc";
  const btn = $("#btn-assisted-login");
  btn.disabled = true;
  setLoginStatus(
    `Bắt đầu assisted login × ${count}. Mỗi lượt: mở Chrome → BẠN tự login → app lấy session + quota.`,
    "running",
  );
  try {
    const r = await api("/api/accounts/assisted-login", {
      method: "POST",
      body: JSON.stringify({
        count,
        label_prefix,
        timeout_sec: 360,
        headless: false,
      }),
    });
    const qid = r.queue?.id;
    if (!qid) throw new Error("No queue id");
    if (assistPollTimer) clearInterval(assistPollTimer);
    assistPollTimer = setInterval(async () => {
      try {
        const st = await api(`/api/accounts/assisted-login/${qid}`);
        const msg = st.message || st.status;
        if (st.status === "done" || st.status === "cancelled" || st.status === "error") {
          clearInterval(assistPollTimer);
          assistPollTimer = null;
          btn.disabled = false;
          const kind = st.status === "done" ? "done" : "error";
          setLoginStatus(
            `${st.status}: ${msg} · done ${st.done}/${st.total}`,
            kind,
          );
          toast(`Assisted login xong: ${st.done}/${st.total}`);
          await refreshAll();
        } else {
          setLoginStatus(`[${st.current}/${st.total}] ${msg}`, "running");
        }
      } catch (e) {
        clearInterval(assistPollTimer);
        assistPollTimer = null;
        btn.disabled = false;
        setLoginStatus(e.message, "error");
      }
    }, 1500);
  } catch (e) {
    btn.disabled = false;
    setLoginStatus(e.message, "error");
    toast(e.message, "err");
  }
});

$("#btn-assist-cancel").addEventListener("click", async () => {
  try {
    await api("/api/accounts/assisted-login/cancel", { method: "POST" });
    toast("Đã gửi huỷ queue login");
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-check-quota-all").addEventListener("click", async () => {
  setLoginStatus("Đang check quota tất cả account…", "running");
  try {
    const r = await api("/api/accounts/check-quota-all", { method: "POST" });
    const ok = (r.results || []).filter((x) => x.has_quota).length;
    setLoginStatus(`Quota check: ${ok}/${r.count} còn lượt`, "done");
    toast(`Quota: ${ok}/${r.count} còn`);
    await refreshAll();
  } catch (e) {
    setLoginStatus(e.message, "error");
    toast(e.message, "err");
  }
});

$("#btn-browser-login").addEventListener("click", async () => {
  const email = $("#acc-email").value.trim();
  const label = $("#acc-label").value.trim()
    || (email ? email.split("@")[0] : `gmail-${Date.now().toString(36)}`);
  const daily_limit = Number($("#acc-limit").value || 2);
  const btn = $("#btn-browser-login");
  btn.disabled = true;
  setLoginStatus("1-shot browser: mở Chrome — bạn login, app lấy session.", "running");
  try {
    const r = await api("/api/accounts/browser-login", {
      method: "POST",
      body: JSON.stringify({
        label,
        email,
        password: "",
        daily_limit,
        headless: false,
        timeout_sec: 360,
        async_mode: true,
      }),
    });
    if (r.async && r.session) {
      const sid = r.session.id;
      if (loginPollTimer) clearInterval(loginPollTimer);
      loginPollTimer = setInterval(async () => {
        try {
          const st = await api(`/api/accounts/login-status/${sid}`);
          if (st.status === "done") {
            clearInterval(loginPollTimer);
            loginPollTimer = null;
            btn.disabled = false;
            setLoginStatus(`✓ ${st.message || "OK"}`, "done");
            toast("Login OK");
            await refreshAll();
          } else if (st.status === "error") {
            clearInterval(loginPollTimer);
            loginPollTimer = null;
            btn.disabled = false;
            setLoginStatus(st.error || "Login lỗi", "error");
          } else {
            setLoginStatus(st.message || st.status, "running");
          }
        } catch (e) {
          clearInterval(loginPollTimer);
          loginPollTimer = null;
          btn.disabled = false;
          setLoginStatus(e.message, "error");
        }
      }, 1500);
    } else {
      btn.disabled = false;
      await refreshAll();
    }
  } catch (e) {
    btn.disabled = false;
    setLoginStatus(e.message, "error");
    toast(e.message, "err");
  }
});

$("#btn-submit-jobs").addEventListener("click", async () => {
  const mode = $("#job-mode").value;
  const account_id = $("#job-account").value || null;
  const ratio = $("#job-ratio").value;
  const duration = Number($("#job-duration").value);
  const resolution = $("#job-res").value;
  const model = $("#job-model").value;
  const isBatch = !$("#panel-batch").hidden;

  try {
    const ref = getRefImagePayload();
    if (mode === "image2video" && !ref.image_path && !ref.image_url) {
      return toast("Image → Video cần upload ảnh hoặc dán Image URL", "err");
    }
    if (isBatch) {
      const prompts = $("#batch-prompts").value.split("\n").map((x) => x.trim()).filter(Boolean);
      if (!prompts.length) return toast("Chưa có prompt", "err");
      const r = await api("/api/jobs/batch", {
        method: "POST",
        body: JSON.stringify({
          prompts, mode, account_id, ratio, duration, resolution, model,
          image_url: ref.image_url,
          image_path: ref.image_path,
        }),
      });
      toast(`Đã queue ${r.count} jobs${mode === "image2video" ? " (kèm ảnh ref)" : ""}`);
      $("#batch-prompts").value = "";
    } else {
      const prompt = $("#single-prompt").value.trim();
      if (!prompt) return toast("Chưa có prompt", "err");
      await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({
          prompt, mode, account_id, ratio, duration, resolution, model,
          image_url: ref.image_url,
          image_path: ref.image_path,
        }),
      });
      toast("Đã queue 1 job");
      $("#single-prompt").value = "";
    }
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-clear-done").addEventListener("click", async () => {
  const jobs = await api("/api/jobs?limit=500");
  const done = jobs.filter((j) => ["success", "failed"].includes(j.status));
  for (const j of done) {
    await api(`/api/jobs/${j.id}`, { method: "DELETE" });
  }
  toast(`Đã xoá ${done.length} jobs`);
  await refreshAll();
});

$("#btn-refresh-acc").addEventListener("click", () => refreshAccounts().catch((e) => toast(e.message, "err")));
$("#btn-refresh-jobs").addEventListener("click", () => refreshJobs().catch((e) => toast(e.message, "err")));
const btnGal = $("#btn-refresh-gallery");
if (btnGal) btnGal.addEventListener("click", () => refreshGallery().catch((e) => toast(e.message, "err")));

// auto refresh
refreshAll().catch((e) => toast(e.message, "err"));
setInterval(() => {
  refreshAll().catch(() => {});
}, 4000);
