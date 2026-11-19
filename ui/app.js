/* Socks Gateway — 界面逻辑（pywebview js_api 桥） */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const TOKEN = new URLSearchParams(location.search).get("t") || "";

  let C = null;          // 配置副本
  let running = false;
  let sysApplied = false;
  let pollTimer = null;
  let saveTimer = null;
  let tick = 0;
  let lastLogKey = "";

  const PAGE_META = {
    overview: ["概览", "本地 SOCKS5h 网关运行状态与实时流量"],
    requests: ["最近请求", "实时查看网关转发的每一条连接"],
    server: ["代理服务器", "上游 SOCKS5 地址、网关监听与不代理列表"],
    switch: ["切换规则", "按域名决定走代理还是直连"],
    rulelist: ["规则列表", "订阅 AutoProxy / gfwlist 规则并自动分流"],
    system: ["系统代理", "把 Windows 全局流量交给本地网关"],
  };

  const TYPE_LABEL = { domain: "域名", wildcard: "域名通配符", regex: "正则表达式" };
  const ACTION_LABEL = { proxy: "通过代理", direct: "直接连接" };

  /* ---------------- 工具 ---------------- */
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return Math.round(n) + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(2) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function fmtUptime(sec) {
    sec = Number(sec) || 0;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const pad = (x) => String(x).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  let toastTimer = null;
  function toast(msg, kind) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show " + (kind || "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, 3200);
  }
  async function call(name, ...args) {
    try {
      const res = await fetch("/api/" + name, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-SG-Token": TOKEN },
        body: JSON.stringify(args),
        cache: "no-store",
      });
      if (!res.ok) return { ok: false, message: `本地接口返回 HTTP ${res.status}` };
      return await res.json();
    } catch (e) {
      const msg = `${name} 调用失败: ${e && e.message ? e.message : e}`;
      console.error(msg);
      return { ok: false, message: msg };
    }
  }

  function report(msg) {
    try {
      fetch("/api/log_js", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-SG-Token": TOKEN },
        body: JSON.stringify([msg]),
      });
    } catch (_) { /* ignore */ }
  }

  window.addEventListener("error", (e) => report(`error: ${e.message} @${e.filename || ""}:${e.lineno}`));
  window.addEventListener("unhandledrejection", (e) => report(`rejection: ${e.reason}`));

  /* ---------------- 渲染 ---------------- */
  function renderServer() {
    $("#f-scheme").value = C.upstream.scheme || "socks5h";
    $("#f-host").value = C.upstream.host || "";
    $("#f-port").value = C.upstream.port || "";
    $("#f-listen-port").value = C.listen.port || "";
    $("#f-user").value = C.upstream.username || "";
    $("#f-pass").value = C.upstream.password || "";
    $("#f-dns").checked = !!C.dns_via_proxy;
    $("#f-bypass").value = (C.bypass || []).join("\n");
    $("#f-bat-path").value = (C.upstream_launcher || {}).bat_path || "";
    $("#hero-upstream").textContent = `${C.upstream.scheme}://${C.upstream.host}:${C.upstream.port}`;
  }

  function renderRules() {
    const body = $("#rules-body");
    if (!C.rules || !C.rules.length) {
      body.innerHTML = `<tr class="empty"><td colspan="5">还没有条件，全部流量按默认情景模式处理</td></tr>`;
    } else {
      body.innerHTML = C.rules.map((r, i) => `
        <tr data-i="${i}">
          <td><span class="drag">≡</span></td>
          <td>
            <select data-act="type">
              ${Object.entries(TYPE_LABEL).map(([v, l]) =>
                `<option value="${v}"${r.type === v ? " selected" : ""}>${l}</option>`).join("")}
            </select>
          </td>
          <td><input type="text" data-act="value" value="${esc(r.value)}" placeholder="例如 *.example.com" spellcheck="false"></td>
          <td>
            <select data-act="action">
              ${Object.entries(ACTION_LABEL).map(([v, l]) =>
                `<option value="${v}"${r.action === v ? " selected" : ""}>${l}</option>`).join("")}
            </select>
          </td>
          <td>
            <button class="icon-btn" data-act="up" ${i === 0 ? "disabled" : ""} title="上移">↑</button>
            <button class="icon-btn" data-act="down" ${i === C.rules.length - 1 ? "disabled" : ""} title="下移">↓</button>
            <button class="icon-btn del" data-act="del" title="删除">✕</button>
          </td>
        </tr>`).join("");
    }
    $("#f-default").value = C.default_action || "proxy";
  }

  // 正文预览上限，仅用于挡住极端大的列表（正常 gfwlist 约 4~5 千条，全部渲染）
  const PREVIEW_MAX = 20000;

  function fmtNextUpdate(rl) {
    const last = String(rl.last_update || "");
    if (!last) return "启动后检查";
    const t = new Date(last.replace(" ", "T"));
    if (isNaN(t.getTime())) return "—";
    const hours = Number(rl.update_interval_hours) >= 1 ? Number(rl.update_interval_hours) : 24;
    const next = new Date(t.getTime() + hours * 3600 * 1000);
    if (next.getTime() <= Date.now()) return "即将更新";
    const pad = (x) => String(x).padStart(2, "0");
    return `${pad(next.getMonth() + 1)}-${pad(next.getDate())} ${pad(next.getHours())}:${pad(next.getMinutes())}`;
  }

  function renderRuleList() {
    const rl = C.rule_list || {};
    const rules = rl.rules || [];
    $("#f-rl-enabled").checked = !!rl.enabled;
    $("#f-rl-url").value = rl.url || "";
    $$('input[name="rlfmt"]').forEach((el) => { el.checked = (el.value === (rl.format || "gfwlist")); });
    $("#rl-count").textContent = String(rl.count || rules.length || 0);
    $("#rl-time").textContent = rl.last_update || "尚未更新";
    $("#rl-next").textContent = fmtNextUpdate(rl);
    $("#rl-sample").textContent = rules.length
      ? rules.slice(0, 3).map((r) => r.value).join("、") + (rules.length > 3 ? " …" : "")
      : "—";
    $("#rl-preview-count").textContent = rules.length ? `共 ${rules.length} 条` : "—";
    const shown = rules.length > PREVIEW_MAX ? rules.slice(0, PREVIEW_MAX) : rules;
    $("#rl-preview").textContent = rules.length
      ? shown.map((r) => `${r.action === "direct" ? "@@ " : ""}${r.value}`).join("\n")
        + (rules.length > PREVIEW_MAX ? `\n… 仅预览前 ${PREVIEW_MAX} 条，共 ${rules.length} 条` : "")
      : "尚未下载规则列表。";
  }

  function renderSystem(info) {
    const cur = (info && info.current) || {};
    $("#sys-enable").textContent = String(cur.ProxyEnable == null ? 0 : cur.ProxyEnable);
    $("#sys-server").textContent = cur.ProxyServer || "—";
    const ov = $("#sys-override");
    ov.textContent = cur.ProxyOverride || "—";
    ov.title = cur.ProxyOverride || "";
    ov.classList.add("clamp");
    $("#sys-admin").textContent = info && info.admin ? "管理员" : "普通用户";
    const pill = $("#sys-pill");
    pill.classList.toggle("on", !!sysApplied);
    pill.querySelector("span").textContent = sysApplied ? "已接管" : "未接管";
  }

  function applyStatus(st) {
    running = !!st.running;
    const pills = [$("#top-status"), $("#side-status")];
    pills.forEach((p) => {
      p.classList.toggle("on", running);
      p.querySelector("span").textContent = running ? "运行中" : "已停止";
    });
    $("#btn-toggle").textContent = running ? "停止网关" : "启动网关";
    $("#btn-toggle").classList.toggle("primary", !running);
    $("#btn-toggle").classList.toggle("danger", running);
    const ring = $("#hero-ring");
    ring.classList.toggle("on", running);
    $("#hero-ring-txt").textContent = running ? "ON" : "OFF";
    $("#hero-listen").textContent = st.listen || `${C.listen.host}:${C.listen.port}`;
    $("#hero-uptime").textContent = running ? fmtUptime(st.uptime) : "—";
  }

  const prevStats = { up: 0, down: 0, t: 0 };
  function applyStats(s, uptime) {
    $("#s-up").textContent = fmtBytes(s.up);
    $("#s-down").textContent = fmtBytes(s.down);
    $("#s-active").textContent = String(s.active || 0);
    $("#s-total").textContent = String(s.total || 0);
    $("#s-fail").textContent = `失败 ${s.failed || 0}`;
    const now = performance.now();
    if (prevStats.t) {
      const dt = (now - prevStats.t) / 1000;
      if (dt > 0.2) {
        const upRate = Math.max(0, (s.up - prevStats.up) / dt);
        const downRate = Math.max(0, (s.down - prevStats.down) / dt);
        $("#s-up-rate").textContent = "≈ " + fmtBytes(upRate) + "/s";
        $("#s-down-rate").textContent = "≈ " + fmtBytes(downRate) + "/s";
      }
    }
    prevStats.up = s.up; prevStats.down = s.down; prevStats.t = now;
    if (running) $("#hero-uptime").textContent = fmtUptime(uptime || 0);
  }

  function renderLog(log) {
    const body = $("#log-body");
    const key = (log && log.length ? `${log.length}:${log[0].time}:${log[0].host}:${log[0].status}` : "0");
    if (key === lastLogKey) return;
    lastLogKey = key;
    if (!log || !log.length) {
      body.innerHTML = `<tr class="empty"><td colspan="7">暂无请求，启动网关后这里会实时刷新</td></tr>`;
      $("#log-count").textContent = "0 条";
      return;
    }
    const wrap = $(".log-wrap");
    const atTop = wrap.scrollTop < 8;
    body.innerHTML = log.map((e) => {
      const statusTag = e.status === "失败" ? "err"
        : e.status === "完成" || e.status === "已连接" ? "ok"
        : e.status === "转发中" || e.status === "连接中" ? "working" : "idle";
      return `<tr title="${esc(e.error || e.why || "")}">
        <td class="num">${esc(e.time)}</td>
        <td>${esc(e.method)}</td>
        <td class="host">${esc(e.host)}<span class="muted">:${esc(e.port)}</span></td>
        <td><span class="tag ${e.action === "direct" ? "direct" : "proxy"}">${e.action === "direct" ? "直连" : "代理"}</span></td>
        <td><span class="tag ${statusTag}">${esc(e.status)}</span></td>
        <td class="num">${fmtBytes(e.up)} / ${fmtBytes(e.down)}</td>
        <td class="num">${e.ms ? e.ms + "ms" : "—"}</td>
      </tr>`;
    }).join("");
    $("#log-count").textContent = log.length + " 条";
    if (atTop) wrap.scrollTop = 0;
  }

  /* ---------------- 状态刷新 ---------------- */
  async function refreshAll(silent) {
    const st = await call("get_state");
    if (!st || !st.ok) return;
    try {
      C = st.config;
      sysApplied = !!st.system_proxy.applied;
      renderServer();
      renderRules();
      renderRuleList();
      renderSystem(st.system_proxy);
      applyStatus(st.status);
      applyStats(st.status.stats, st.status.uptime);
      $("#ver").textContent = "v" + (st.env.version || "1.0.0");
      $("#cfg-dir").textContent = st.env.appdata || "%APPDATA%\\SocksGateway";
      $("#tg-applystart").checked = !!C.system_proxy.apply_on_start;
      $("#tg-autostart").checked = !!C.autostart;
      $("#tg-autostart-gw").checked = !!C.auto_start_gateway;
      $("#tg-sysproxy").checked = sysApplied;
      $("#tg-dns").checked = !!C.dns_via_proxy;
    } catch (err) {
      window.__sgLastError = String(err && err.stack ? err.stack : err);
      throw err;
    }
    if (!silent) console.log("state loaded");
    if (!pollTimer) startPolling();
  }

  function startPolling() {
    pollTimer = setInterval(async () => {
      const st = await call("poll");
      if (!st) return;
      running = !!st.running;
      applyStatus(st);
      applyStats(st.stats, st.uptime);
      renderLog(st.log);
      if (st.system_proxy_applied !== sysApplied) {
        sysApplied = st.system_proxy_applied;
        $("#tg-sysproxy").checked = sysApplied;
        const pill = $("#sys-pill");
        pill.classList.toggle("on", sysApplied);
        pill.querySelector("span").textContent = sysApplied ? "已接管" : "未接管";
      }
      if (++tick % 12 === 0) {   // 每 12 秒同步一次配置（外部改动/多窗口）
        const full = await call("get_state");
        if (full && full.ok) {
          C = full.config;
          renderSystem(full.system_proxy);
        }
      }
    }, 1000);
  }

  function saveCfg(silent) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      const r = await call("save_config", JSON.parse(JSON.stringify(C)));
      if (!r.ok) toast(r.message || "保存失败", "err");
      else if (!silent) toast(r.message || "已保存", "ok");
    }, 260);
  }

  /* ---------------- 事件绑定 ---------------- */
  function showPage(page) {
    if (!PAGE_META[page]) page = "overview";
    $$(".nav-item[data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + page));
    const meta = PAGE_META[page] || ["", ""];
    $("#page-title").textContent = meta[0];
    $("#page-desc").textContent = meta[1];
    $(".scroll").scrollTop = 0;
    if (location.hash.slice(1) !== page) history.replaceState(null, "", "#" + page);
  }

  function bindNav() {
    $$(".nav-item[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => showPage(btn.dataset.page));
    });
    window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
  }

  function bindServer() {
    const up = () => C.upstream;
    $("#f-scheme").addEventListener("change", (e) => {
      up().scheme = e.target.value;
      C.dns_via_proxy = true;
      $("#f-dns").checked = true;
      $("#tg-dns").checked = true;
      renderServer(); saveCfg(true);
    });
    $("#f-host").addEventListener("change", (e) => { up().host = e.target.value.trim(); renderServer(); saveCfg(true); });
    $("#f-port").addEventListener("change", (e) => { up().port = Number(e.target.value) || 18080; saveCfg(); });
    $("#f-listen-port").addEventListener("change", (e) => { C.listen.port = Number(e.target.value) || 18081; saveCfg(); });
    $("#f-user").addEventListener("change", (e) => { up().username = e.target.value.trim(); saveCfg(true); });
    $("#f-pass").addEventListener("change", (e) => { up().password = e.target.value; saveCfg(true); });
    $("#f-dns").addEventListener("change", (e) => {
      C.dns_via_proxy = e.target.checked;
      $("#tg-dns").checked = C.dns_via_proxy;
      saveCfg(true);
    });
    $("#f-bypass").addEventListener("change", (e) => {
      C.bypass = e.target.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      saveCfg(true);
    });

    $("#f-bat-path").addEventListener("change", (e) => {
      C.upstream_launcher = C.upstream_launcher || {};
      C.upstream_launcher.bat_path = e.target.value.trim();
      saveCfg(true);
    });
    $("#btn-bat-pick").addEventListener("click", async (e) => {
      const btn = e.target;
      btn.disabled = true; btn.textContent = "选择中…";
      try {
        const r = await call("pick_bat_file");
        if (r.ok && r.path) {
          C.upstream_launcher = C.upstream_launcher || {};
          C.upstream_launcher.bat_path = r.path;
          $("#f-bat-path").value = r.path;
          saveCfg(true);
        }
      } finally {
        btn.disabled = false; btn.textContent = "选择…";
      }
    });
    $("#btn-up-start").addEventListener("click", async (e) => {
      const btn = e.target;
      btn.disabled = true; btn.textContent = "启动中…";
      try {
        const r = await call("start_upstream");
        toast(r.message, r.ok ? "ok" : "err");
      } finally {
        btn.disabled = false; btn.textContent = "启动代理服务";
      }
    });
  }

  function bindRules() {
    $("#btn-add-rule").addEventListener("click", () => {
      C.rules.push({ type: "domain", value: "", action: "proxy" });
      renderRules();
      const inputs = $$('#rules-body input[data-act="value"]');
      if (inputs.length) inputs[inputs.length - 1].focus();
      saveCfg(true);
    });

    $("#rules-body").addEventListener("change", (e) => {
      const tr = e.target.closest("tr[data-i]");
      if (!tr) return;
      const i = Number(tr.dataset.i);
      const act = e.target.dataset.act;
      if (!act) return;
      C.rules[i][act] = e.target.value.trim();
      if (act === "value") renderRules();
      saveCfg(true);
    });

    $("#rules-body").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const i = Number(btn.closest("tr").dataset.i);
      const act = btn.dataset.act;
      if (act === "del") C.rules.splice(i, 1);
      else if (act === "up" && i > 0) [C.rules[i - 1], C.rules[i]] = [C.rules[i], C.rules[i - 1]];
      else if (act === "down" && i < C.rules.length - 1) [C.rules[i + 1], C.rules[i]] = [C.rules[i], C.rules[i + 1]];
      renderRules();
      saveCfg(true);
    });

    $("#f-default").addEventListener("change", (e) => { C.default_action = e.target.value; saveCfg(true); });
  }

  function bindRuleList() {
    $("#f-rl-enabled").addEventListener("change", (e) => {
      C.rule_list.enabled = e.target.checked;
      saveCfg(true);
      toast(e.target.checked ? "规则列表已启用" : "规则列表已停用", "ok");
    });
    $("#f-rl-url").addEventListener("change", (e) => { C.rule_list.url = e.target.value.trim(); saveCfg(true); });
    $$('input[name="rlfmt"]').forEach((el) => el.addEventListener("change", (e) => {
      if (e.target.checked) { C.rule_list.format = e.target.value; saveCfg(true); }
    }));
    $("#btn-rl-update").addEventListener("click", async (e) => {
      const btn = e.target;
      btn.disabled = true; btn.textContent = "下载中…";
      const r = await call("update_rule_list");
      btn.disabled = false; btn.textContent = "立即更新";
      toast(r.message, r.ok ? "ok" : "err");
      if (r.ok) await refreshAll(true);
    });
  }

  function bindSystem() {
    $("#btn-sys-on").addEventListener("click", async () => {
      if (!running) { toast("请先启动网关", "err"); return; }
      const r = await call("apply_system_proxy", true);
      toast(r.message, r.ok ? "ok" : "err");
      if (r.ok) { sysApplied = true; $("#tg-sysproxy").checked = true; refreshAll(true); }
    });
    $("#btn-sys-off").addEventListener("click", async () => {
      const r = await call("apply_system_proxy", false);
      toast(r.message, r.ok ? "ok" : "err");
      if (r.ok) { sysApplied = false; $("#tg-sysproxy").checked = false; refreshAll(true); }
    });
    $("#btn-winhttp").addEventListener("click", async () => {
      const r = await call("set_winhttp", true);
      toast(r.message, r.ok ? "ok" : "err");
    });
    $("#btn-env").addEventListener("click", async () => {
      const r = await call("export_env_script");
      toast(r.message, r.ok ? "ok" : "err");
    });
    $("#tg-usrenv").addEventListener("change", async (e) => {
      const r = await call("set_user_env", e.target.checked);
      toast(r.message, r.ok ? "ok" : "err");
    });
    ["#btn-pac", "#btn-pac2"].forEach((sel) => $(sel).addEventListener("click", async () => {
      const r = await call("export_pac");
      toast(r.message, r.ok ? "ok" : "err");
    }));
    ["#btn-folder", "#btn-folder2"].forEach((sel) => $(sel).addEventListener("click", () => call("open_folder", "")));
  }

  function bindToggles() {
    $("#btn-toggle").addEventListener("click", async () => {
      const btn = $("#btn-toggle");
      btn.disabled = true;
      const r = await call(running ? "stop" : "start");
      btn.disabled = false;
      toast(r.message, r.ok ? "ok" : "err");
      await refreshAll(true);
    });
    $("#tg-sysproxy").addEventListener("change", async (e) => {
      if (e.target.checked && !running) {
        e.target.checked = false;
        toast("请先启动网关", "err");
        return;
      }
      const r = await call("apply_system_proxy", e.target.checked);
      if (!r.ok) e.target.checked = !e.target.checked;
      toast(r.message, r.ok ? "ok" : "err");
      refreshAll(true);
    });
    $("#tg-applystart").addEventListener("change", (e) => {
      C.system_proxy.apply_on_start = e.target.checked; saveCfg(true);
    });
    $("#tg-dns").addEventListener("change", (e) => {
      C.dns_via_proxy = e.target.checked;
      $("#f-dns").checked = e.target.checked;
      saveCfg(true);
    });
    $("#tg-autostart").addEventListener("change", async (e) => {
      const r = await call("set_autostart", e.target.checked);
      if (!r.ok) e.target.checked = !e.target.checked;
      toast(r.message, r.ok ? "ok" : "err");
    });
    $("#tg-autostart-gw").addEventListener("change", (e) => {
      C.auto_start_gateway = e.target.checked; saveCfg(true);
    });
  }

  function bindTests() {
    const out = $("#test-out");
    const setOut = (msg, kind) => { out.className = "test-out " + (kind || ""); out.textContent = msg; };
    $("#btn-test-up").addEventListener("click", async (e) => {
      e.target.disabled = true;
      setOut("正在与上游 SOCKS5 握手…");
      const r = await call("test_upstream");
      e.target.disabled = false;
      setOut((r.ok ? "✓ " : "✗ ") + r.message, r.ok ? "ok" : "err");
    });
    $("#btn-test-gw").addEventListener("click", async (e) => {
      e.target.disabled = true;
      setOut("正在经本地网关访问 http://www.gstatic.com/generate_204 …");
      const r = await call("test_gateway", "http://www.gstatic.com/generate_204");
      e.target.disabled = false;
      setOut((r.ok ? "✓ " : "✗ ") + r.message, r.ok ? "ok" : "err");
    });
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    bindNav(); bindServer(); bindRules(); bindRuleList(); bindSystem(); bindToggles(); bindTests();
    if (location.hash.length > 1) showPage(location.hash.slice(1));
    refreshAll(false).then(async () => {
      if (C && C.auto_start_gateway && !running) {
        const r = await call("start");
        if (r.ok) toast(r.message, "ok");
        await refreshAll(true);
      }
    });
  }

  let booted = false;
  function tryBoot() {
    if (booted) return;
    booted = true;
    try { boot(); } catch (e) { toast("界面初始化失败：" + e, "err"); }
  }

  function waitApi(tries) {
    // 兼容仍在等待就绪的场景：直接探测本地接口是否可连
    call("poll").then((r) => {
      if (r && typeof r.running === "boolean") tryBoot();
      else if (tries > 0) setTimeout(() => waitApi(tries - 1), 120);
      else tryBoot();
    });
  }

  window.addEventListener("pywebviewready", () => waitApi(0), { once: true });
  waitApi(6);   // 接口就绪后立即渲染，避免启动早期空白
})();
