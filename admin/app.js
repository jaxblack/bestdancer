let state = { config: {}, selected: [] };
let selectedOrder = [];
let specialIds = new Set();  // 特别加映（从 TOP 队列中排除）
function topOrder() { return selectedOrder.filter(id => !specialIds.has(id)); }
function moveInOrder(id, delta) {
  const arr = topOrder();
  const i = arr.indexOf(id); if (i < 0) return;
  const j = i + delta; if (j < 0 || j >= arr.length) return;
  [arr[i], arr[j]] = [arr[j], arr[i]];
  // rewrite selectedOrder: top order then specials (preserve specials relative order)
  selectedOrder = [...arr, ...selectedOrder.filter(x => specialIds.has(x))];
  renderCandidates();
}
function toggleSpecial(id) {
  if (specialIds.has(id)) specialIds.delete(id); else specialIds.add(id);
  // reorder selectedOrder to put tops first, specials at end
  selectedOrder = [...selectedOrder.filter(x => !specialIds.has(x)), ...selectedOrder.filter(x => specialIds.has(x))];
  renderCandidates();
}
function removeSelection(id) {
  selectedOrder = selectedOrder.filter(x => x !== id);
  specialIds.delete(id);
  renderCandidates();
}
const $ = (selector) => document.querySelector(selector);
const api = async (path, options = {}) => {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
};
const lines = (value) => value.split("\n").map((item) => item.trim()).filter(Boolean);
const number = (value) => Number(value || 0).toLocaleString();
// Buffer per-row edits keyed by candidate id; page-based rendering means only
// visible rows are in the DOM, so we merge visible-row values on top of the
// server-side state before sending to /api/save.
const editBuffer = new Map();
const deletedCandidateIds = new Set();

function collectVisibleEdits() {
  document.querySelectorAll("#candidate-list .candidate-card").forEach((row) => {
    editBuffer.set(row.dataset.id, {
      id: row.dataset.id,
      source: row.dataset.source,
      duration_sec: Number(row.dataset.duration || 0),
      like: Number(row.dataset.like || 0),
      play: Number(row.dataset.play || 0),
      tags: JSON.parse(row.dataset.tags || "[]"),
      url: row.dataset.url,
      local_path: row.dataset.localPath || "",
      manual_note: row.dataset.note || "",
      source_desc: row.dataset.sourceDesc || "",
      download_status: row.dataset.downloadStatus || "unknown",
      candidate_tier: row.dataset.candidateTier || "top",
      chosen: row.querySelector(".chosen").checked,
      is_special: row.querySelector(".is-special")?.checked || false,
      order: Number(row.querySelector(".order").value || 999),
      dance_type: row.querySelector(".dance-type").value.trim(),
      title: row.querySelector(".title").value.trim(),
      creator: row.querySelector(".creator").value.trim(),
      narration: row.querySelector(".narration").value.trim(),
      voice: $("#global-voice")?.value || "zh-CN-XiaoyiNeural",
      voice_rate: $("#global-rate")?.value || "+20%",
      clip_start_sec: Number(row.querySelector(".clip-start").value || 0),
      clip_end_sec: Number(row.querySelector(".clip-end").value || 0),
      difficulty: { stars: Number(row.querySelector(".stars").value), fit: row.querySelector(".dance-type").value.trim(), scores: {} },
    });
  });
}

function candidates() {
  collectVisibleEdits();
  // Merge visible edits over server state so unedited pages preserve their values
  const all = [...(state.config.this_week_candidates || []), ...(state.config.classics_pool || [])];
  return all.filter((item) => !deletedCandidateIds.has(item.id)).map((item) => {
    const edit = editBuffer.get(item.id);
    return edit ? { ...item, ...edit } : {
      ...item, chosen: selectedOrder.includes(item.id),
      order: selectedOrder.indexOf(item.id) + 1 || 999,
      difficulty: item.difficulty || { stars: 3, fit: item.dance_type || "街舞", scores: {} },
    };
  });
}
function selected() { return candidates().filter((item) => item.chosen).sort((left, right) => left.order - right.order).map((item) => item.id); }
function defaultNarration(item) {
  const order = selectedOrder.indexOf(item.id);
  const prefix = order === 5 ? "特别加映" : order >= 0 ? `第${order + 1}名` : "本周推荐";
  const creator = (item.creator || "这位编舞者").replace(/^@/, "");
  return `${prefix}，${item.dance_type || "街舞"}，来自 ${creator}。`;
}
let currentPage = 1;
const PAGE_SIZE = 12;

function ensureCandidateSortControl() {
  if ($("#candidate-sort")) return;
  const label = document.createElement("label");
  label.innerHTML = `排序<select id="candidate-sort"><option value="likes_desc">点赞从高到低</option><option value="likes_asc">点赞从低到高</option><option value="plays_desc">观看从高到低</option><option value="plays_asc">观看从低到高</option></select>`;
  $("#candidate-tier").closest("label").insertAdjacentElement("afterend", label);
}

ensureCandidateSortControl();

function candidateFilters() {
  return {
    platform: $("#candidate-platform").value,
    query: $("#candidate-query").value.trim().toLowerCase(),
    minLikes: Number($("#candidate-min-likes").value || 0),
    tier: $("#candidate-tier").value,
    download: $("#candidate-download").value,
    selectedOnly: $("#candidate-selected").checked,
    sort: $("#candidate-sort").value,
  };
}

function populateCandidatePlatforms(items) {
  const select = $("#candidate-platform");
  const selected = select.value;
  const platforms = [...new Set(items.map((item) => item.source).filter(Boolean))].sort();
  select.replaceChildren(new Option("全部平台", ""), ...platforms.map((platform) => new Option(platform, platform)));
  select.value = platforms.includes(selected) ? selected : "";
}

function matchesCandidateFilters(item, filters) {
  if (filters.platform && item.source !== filters.platform) return false;
  if (filters.minLikes && Number(item.like || 0) < filters.minLikes) return false;
  if (filters.tier && item.candidate_tier !== filters.tier) return false;
  if (filters.download && item.download_status !== filters.download) return false;
  if (filters.selectedOnly && !selectedOrder.includes(item.id)) return false;
  if (!filters.query) return true;
  const text = [item.title, item.creator, item.dance_type, item.source_desc, item.url, ...(item.tags || [])]
    .filter(Boolean).join(" ").toLowerCase();
  return text.includes(filters.query);
}

function renderCandidates() {
  const list = $("#candidate-list"); list.replaceChildren();
  const raw = [...(state.config.this_week_candidates || []), ...(state.config.classics_pool || [])]
    .filter((item) => !(typeof deletedCandidateIds !== "undefined" && deletedCandidateIds.has && deletedCandidateIds.has(item.id)))
    .map((item) => { const e = editBuffer.get(item.id); return e ? { ...item, ...e } : item; });
  populateCandidatePlatforms(raw);
  const filters = candidateFilters();
  const filtered = raw.filter((item) => matchesCandidateFilters(item, filters));
  // Chosen items ALWAYS pinned to the front (in selected order), so they stay
  // visible regardless of like-based sorting or pagination.
  const isChosen = (item) => selectedOrder.includes(item.id);
  const items = filtered.slice().sort((a, b) => {
    const ac = isChosen(a), bc = isChosen(b);
    if (ac !== bc) return ac ? -1 : 1;
    if (ac && bc) return selectedOrder.indexOf(a.id) - selectedOrder.indexOf(b.id);
    const field = filters.sort.startsWith("plays") ? "play" : "like";
    const direction = filters.sort.endsWith("_asc") ? 1 : -1;
    const difference = (Number(a[field] || 0) - Number(b[field] || 0)) * direction;
    return difference || ((a.candidate_tier === "backup") - (b.candidate_tier === "backup")) || ((b.like || 0) - (a.like || 0));
  });
  const chosenCount = selectedOrder.length;
  $("#candidate-count").textContent = `${items.length} / ${raw.length}（已入选 ${chosenCount}）`;
  const totalPages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = 1;
  const pageItems = items.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  for (const item of pageItems) {
    const row = $("#candidate-template").content.firstElementChild.cloneNode(true);
    row.dataset.id=item.id;
    if (selectedOrder.includes(item.id)) row.classList.add("is-chosen"); row.dataset.source=item.source || "抖音"; row.dataset.duration=item.duration_sec || 0; row.dataset.like=item.like || 0; row.dataset.play=item.play || 0; row.dataset.tags=JSON.stringify(item.tags || []); row.dataset.url=item.url || ""; row.dataset.localPath=item.local_path || ""; row.dataset.note=item.manual_note || ""; row.dataset.sourceDesc=item.source_desc || ""; row.dataset.downloadStatus=item.download_status || "unknown"; row.dataset.candidateTier=item.candidate_tier || "top";
    row.querySelector(".chosen").checked=selectedOrder.includes(item.id);
    const specialCb = row.querySelector(".is-special");
    if (specialCb) specialCb.checked = (editBuffer.get(item.id)?.is_special) ?? item.is_special ?? false;
    row.querySelector(".heat").innerHTML=`<small>点赞</small> ${number(item.like)} <small>观看</small> ${number(item.play)}`;
    const status = document.createElement("span"); status.className=`download ${row.dataset.downloadStatus}`; status.textContent={ready:"可下载",downloaded:"已下载",unavailable:"不可下载",failed:"下载失败",link_only:"已采集链接"}[row.dataset.downloadStatus] || "待检测"; row.querySelector(".download-cell").append(status);
    const tier = document.createElement("span"); tier.className=`candidate-tier ${row.dataset.candidateTier}`; tier.textContent=row.dataset.candidateTier === "backup" ? "备选" : "TOP10"; row.querySelector(".heat").before(tier);
    const source = document.createElement("span"); source.className="source-platform"; source.textContent=`来源：${item.source || "待补充"}`; row.querySelector(".heat").before(source);
    const danceType = item.dance_type || "Urban";
    row.querySelector(".dance-type").value=[...row.querySelector(".dance-type").options].some((option) => option.value === danceType) ? danceType : "Urban";
    row.querySelector(".order").value=selectedOrder.indexOf(item.id) + 1 || ""; row.querySelector(".title").value=item.title || ""; row.querySelector(".creator").value=item.creator || ""; row.querySelector(".narration").value=item.narration || defaultNarration(item); row.querySelector(".clip-start").value=item.clip_start_sec || 0; row.querySelector(".clip-end").value=item.clip_end_sec || item.duration_sec || ""; row.querySelector(".stars").value=Math.round(item.difficulty?.stars || 3);
    const link=row.querySelector(".source-link"); link.href=item.url || "#"; link.textContent=item.url ? "原平台预览" : "原链接待补";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button"; deleteButton.className = "delete-candidate"; deleteButton.textContent = "删除";
    deleteButton.title = "从本期候选池删除";
    link.before(deleteButton);
    deleteButton.addEventListener("click", async () => {
      deletedCandidateIds.add(item.id);
      selectedOrder = selectedOrder.filter((id) => id !== item.id);
      editBuffer.delete(item.id);
      renderCandidates();
      try {
        const result = await api("/api/delete-candidate", { method: "POST", body: JSON.stringify({ week: $("#week").value, id: item.id }) });
        state.config = result.config;
        state.selected = selected();
        deletedCandidateIds.delete(item.id);
        renderCandidates();
        $("#status").textContent = "候选已删除并保存";
      } catch (error) {
        deletedCandidateIds.delete(item.id);
        renderCandidates();
        $("#status").textContent = `删除失败：${error.message}`;
      }
    });
    row.querySelector(".preview-clip").addEventListener("click", async () => {
      const button = row.querySelector(".preview-clip"); const status = row.querySelector(".clip-status"); const player = row.querySelector(".clip-player");
      const start = Number(row.querySelector(".clip-start").value || 0); const end = Number(row.querySelector(".clip-end").value || 0);
      if (end <= start) { status.textContent = "结束秒数要大于开始秒数"; return; }
      button.disabled = true; status.textContent = "生成片段...";
      try {
        const result = await api("/api/clip-preview", { method: "POST", body: JSON.stringify({ week: $("#week").value, candidate_id: item.id, start, end }) });
        player.src = `${result.video_url}?v=${Date.now()}`; player.hidden = false; await player.play(); status.textContent = "片段已生成";
      } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; }
    });
    row.querySelector(".preview-voice").addEventListener("click", async () => {
      const button = row.querySelector(".preview-voice"); const status = row.querySelector(".voice-status"); const player = row.querySelector(".voice-player");
      button.disabled = true; status.textContent = "生成中...";
      try {
        const result = await api("/api/voice-preview", { method: "POST", body: JSON.stringify({ week: $("#week").value, candidate_id: item.id, text: row.querySelector(".narration").value.trim(), voice: $("#global-voice")?.value || "zh-CN-XiaoyiNeural", rate: $("#global-rate")?.value || "+20%" }) });
        player.src = `${result.audio_url}?v=${Date.now()}`; player.hidden = false; await player.play(); status.textContent = "试听已生成";
      } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; }
    });
    // 已入选卡片：注入位置控件 (⬆ TOPn ⬇  ⭐特别加映  ✕)
    if (selectedOrder.includes(item.id)) {
      const bar = document.createElement("div");
      bar.className = "pin-bar";
      const isSp = specialIds.has(item.id);
      const tops = topOrder();
      const pos = tops.indexOf(item.id);  // -1 if special
      const label = isSp ? "特别加映" : `TOP ${pos + 1} / ${tops.length}`;
      bar.innerHTML = `
        <button type="button" class="pin-up" ${isSp||pos<=0?"disabled":""} title="上移">⬆</button>
        <span class="pin-label ${isSp?"is-special":""}">${label}</span>
        <button type="button" class="pin-down" ${isSp||pos<0||pos>=tops.length-1?"disabled":""} title="下移">⬇</button>
        <button type="button" class="pin-special ${isSp?"active":""}" title="切换 特别加映">⭐</button>
        <button type="button" class="pin-remove" title="移出入选">✕</button>
      `;
      row.querySelector(".candidate-card-header").prepend(bar);
      bar.querySelector(".pin-up").onclick = () => moveInOrder(item.id, -1);
      bar.querySelector(".pin-down").onclick = () => moveInOrder(item.id, +1);
      bar.querySelector(".pin-special").onclick = () => toggleSpecial(item.id);
      bar.querySelector(".pin-remove").onclick = () => removeSelection(item.id);
    }
    row.querySelector(".chosen").addEventListener("change", (event) => {
      if (event.target.checked) { if (!selectedOrder.includes(item.id)) selectedOrder.push(item.id); }
      else { selectedOrder = selectedOrder.filter((id) => id !== item.id); specialIds.delete(item.id); }
      renderCandidates();
    });
    row.querySelector(".order").addEventListener("change", () => { selectedOrder = selected(); }); list.append(row);
  }
  // pagination bar
  const bar = document.createElement("div");
  bar.className = "pagination";
  const from = items.length === 0 ? 0 : (currentPage - 1) * PAGE_SIZE + 1;
  const to = Math.min(items.length, currentPage * PAGE_SIZE);
  bar.innerHTML = `<button type="button" class="pg-prev" ${currentPage === 1 ? "disabled" : ""}>◀ 上一页</button>
    <span class="pg-info">第 ${currentPage} / ${totalPages} 页 · 显示 ${from}-${to} / 共 ${items.length}</span>
    <label class="pg-jump">跳至 <input class="pg-input" type="number" min="1" max="${totalPages}" value="${currentPage}" aria-label="跳转页码"> 页</label>
    <button type="button" class="pg-go">跳转</button>
    <button type="button" class="pg-next" ${currentPage === totalPages ? "disabled" : ""}>下一页 ▶</button>`;
  bar.querySelector(".pg-prev").onclick = () => { collectVisibleEdits(); currentPage = Math.max(1, currentPage - 1); renderCandidates(); window.scrollTo({top: $("#candidates").offsetTop - 20, behavior: "smooth"}); };
  bar.querySelector(".pg-next").onclick = () => { collectVisibleEdits(); currentPage = Math.min(totalPages, currentPage + 1); renderCandidates(); window.scrollTo({top: $("#candidates").offsetTop - 20, behavior: "smooth"}); };
  const jump = () => { const page = Number(bar.querySelector(".pg-input").value); if (!Number.isInteger(page) || page < 1 || page > totalPages) return; collectVisibleEdits(); currentPage = page; renderCandidates(); window.scrollTo({top: $("#candidates").offsetTop - 20, behavior: "smooth"}); };
  bar.querySelector(".pg-go").onclick = jump;
  bar.querySelector(".pg-input").onkeydown = (event) => { if (event.key === "Enter") jump(); };
  list.append(bar);
}
function payload() { return { week: $("#week").value, episode: { week: $("#week").value }, candidates: candidates(), selected: selectedOrder.slice(0,6), special_ids: [...specialIds], video_description: $("#video-description").value.trim(),
  global_voice: $("#global-voice")?.value || "",
  global_voice_rate: $("#global-rate")?.value || "",
  vo_template: $("#vo-tpl")?.value.trim() || "",
  vo_template_classic: $("#vo-tpl-classic")?.value.trim() || "",
  intro: { title1: $("#intro-title1")?.value.trim() || "", title2: $("#intro-title2")?.value.trim() || "", foot: $("#intro-foot")?.value.trim() || "", vo: $("#intro-vo")?.value.trim() || "" },
  outro: { title1: $("#outro-title1")?.value.trim() || "", sub: $("#outro-sub")?.value.trim() || "", vo: $("#outro-vo")?.value.trim() || "" },
}; }
function buildVideoDescription() {
  const all = new Map(candidates().map((item) => [item.id, item]));
  const ranked = selected().map((id) => all.get(id)).filter(Boolean);
  const week = $("#week").value.replace(/^(\d{4})-W(\d{2})(?:-([AB]))?$/, (_, year, number, edition) => `${year} 年第${number}周${edition === "A" ? "上部" : edition === "B" ? "下部" : ""}`);
  if (!ranked.length) return `${week}热舞又来啦！先在候选池勾选并排好本期视频，再生成排行榜。`;
  const list = ranked.map((item, index) => {
    const prefix = index < 5 ? `${index + 1}.` : "特别加映：";
    const title = item.title ? `《${item.title.replace(/[《》]/g, "")}》` : item.dance_type || "本周编舞";
    const creator = item.creator || "原作者待补充";
    return `${prefix} ${title} · ${creator}\n来源：${item.source || "待补充"} · 点赞：${number(item.like)} · 观看：${number(item.play)}\n${item.url || "原链接待补充"}`;
  }).join("\n\n");
  return `${week}热舞又来啦！这周的编舞里，有没有一支让你忍不住想跟跳？\n\n本期排行榜：\n${list}\n\n#热舞榜 #编舞 #街舞 #BestDancer`;
}
function selectedPlatforms() { return [...document.querySelectorAll(".platform:checked")].map((input) => input.value); }
function renderPlatforms(platforms) { document.querySelectorAll(".platform").forEach((input) => { input.checked = platforms.includes(input.value); }); $("#platform-all").checked = selectedPlatforms().length === document.querySelectorAll(".platform").length; }
function renderWorkspaces(workspaces, activeWeek) {
  const weekSelect = $("#week");
  const available = workspaces.some((workspace) => workspace.week === activeWeek) ? workspaces : [{ week: activeWeek, configured: false }, ...workspaces];
  weekSelect.replaceChildren(...available.map((workspace) => {
    const option = document.createElement("option");
    option.value = workspace.week; option.textContent = workspace.week; option.selected = workspace.week === activeWeek;
    return option;
  }));
  const list = $("#workspace-list"); list.replaceChildren();
  for (const workspace of available) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "workspace-item";
    button.classList.toggle("active", workspace.week === activeWeek);
    button.textContent = workspace.configured ? `${workspace.week} · ${workspace.candidates} 候选 · ${workspace.selected} 已编排` : `${workspace.week} · 未开始`;
    button.onclick = () => { if (workspace.week !== $("#week").value) { $("#week").value = workspace.week; load(); } };
    list.append(button);
  }
}
function baseWeek(value) { return value.replace(/-[AB]$/, ""); }
function syncEdition() { const match = $("#week").value.match(/-([AB])$/); $("#edition").value = match ? match[1] : ""; }
function setEdition() {
  const target = `${baseWeek($("#week").value)}${$("#edition").value ? `-${$("#edition").value}` : ""}`;
  if (![...$("#week").options].some((option) => option.value === target)) $("#week").append(new Option(`${target} · 未开始`, target));
  $("#week").value = target;
  load();
}
async function load() { state=await api(`/api/state?week=${encodeURIComponent($("#week").value)}`); selectedOrder=[...state.selected]; specialIds = new Set(); const sp = state.config.classic_comeback?.id; if (sp) { specialIds.add(sp); if (!selectedOrder.includes(sp)) selectedOrder.push(sp); } deletedCandidateIds.clear(); editBuffer.clear();
  // 反哺 picks / classic_comeback 里的 difficulty 到候选池条目（后端 picks 才是权威）
  const pickStars = {};
  (state.config.picks || []).forEach(p => { if (p.difficulty?.stars != null) pickStars[p.id] = p.difficulty.stars; });
  if (state.config.classic_comeback?.id && state.config.classic_comeback.difficulty?.stars != null) pickStars[state.config.classic_comeback.id] = state.config.classic_comeback.difficulty.stars;
  [...(state.config.this_week_candidates||[]), ...(state.config.classics_pool||[])].forEach(c => {
    if (pickStars[c.id] != null) { c.difficulty = c.difficulty || {}; c.difficulty.stars = pickStars[c.id]; }
  });
  syncEdition(); const settings=state.settings; $("#keywords").value=settings.keywords.join("\n"); $("#top-limit").value=settings.top_limit; $("#min-likes").value=settings.min_likes || 0; $("#recent-days").value=settings.recent_days || 7; $("#sort-by").value=settings.sort_by || "heat_desc"; $("#videos-only").checked=settings.videos_only !== false; renderPlatforms(settings.platforms || ["douyin"]); renderCandidates(); $("#video-description").value=state.config.metadata?.video_description || buildVideoDescription();
  // 全局设置回填
  const m=state.config.metadata||{};
  if($("#global-voice")) $("#global-voice").value = m.global_voice || "zh-CN-XiaoyiNeural";
  if($("#global-rate")) $("#global-rate").value = m.global_voice_rate || "+20%";
  if($("#vo-tpl")) $("#vo-tpl").value = m.vo_template || "第{rank}名，{dance_type}街舞{title}，来自 {creator}。";
  if($("#vo-tpl-classic")) $("#vo-tpl-classic").value = m.vo_template_classic || "特别加映，{dance_type}街舞{title}，来自 {creator}。";
  const intro=m.intro||{}, outro=m.outro||{};
  if($("#intro-title1")) $("#intro-title1").value = intro.title1 || "本周热舞";
  if($("#intro-title2")) $("#intro-title2").value = intro.title2 || "WEEKLY DANCE";
  if($("#intro-foot")) $("#intro-foot").value = intro.foot || "TOP5 + 特别加映";
  if($("#intro-vo")) $("#intro-vo").value = intro.vo || "本周热舞榜，五支正片，加一支特别加映。";
  if($("#outro-title1")) $("#outro-title1").value = outro.title1 || "关注追更";
  if($("#outro-sub")) $("#outro-sub").value = outro.sub || "下周同一时间见";
  if($("#outro-vo")) $("#outro-vo").value = outro.vo || "你最喜欢哪一支？评论区见。";
  const workspaceData=await api(`/api/workspaces?recent=${encodeURIComponent($("#workspace-range").value)}`); renderWorkspaces(workspaceData.workspaces, state.week); $("#status").textContent=`${state.week} 已载入`; }
async function saveConfig() { collectVisibleEdits(); const result=await api("/api/save",{method:"POST",body:JSON.stringify(payload())}); state.config=result.config; state.selected=selected(); $("#status").textContent="本期编排已保存"; }
async function action(name) { await saveConfig(); const result=await api("/api/action",{method:"POST",body:JSON.stringify({week:$("#week").value,action:name})}); const labels={render:"正在生成视频",discover:"正在粗筛候选",download:"正在下载入选视频"}; $("#status").textContent=`${labels[name]}（任务 ${result.job.id}）`; pollJobs(); }
async function pollJobs(){ const result=await api("/api/jobs"); const job=result.jobs.at(-1); if(!job)return; $("#job-output").textContent=job.output || "任务启动中..."; $("#status").textContent=`${job.name}: ${job.status}`; if(job.status === "running") setTimeout(pollJobs,1200); }
function currentIsoWeek() {
  const today = new Date();
  const date = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()));
  date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}
async function initializeWorkspace() {
  const result = await api(`/api/workspaces?recent=${encodeURIComponent($("#workspace-range").value)}`);
  const latest = result.workspaces.find((workspace) => workspace.configured && workspace.candidates > 0)
    || result.workspaces.find((workspace) => workspace.configured)
    || { week: currentIsoWeek() };
  $("#week").replaceChildren(new Option(latest.week, latest.week));
  await load();
}
const workflowLinks = [...document.querySelectorAll("aside nav a")];
const workflowSections = workflowLinks.map((link) => document.querySelector(link.getAttribute("href"))).filter(Boolean);
function updateWorkflowStep() {
  const readingLine = 160;
  const current = workflowSections.find((section) => {
    const rect = section.getBoundingClientRect();
    return rect.top <= readingLine && rect.bottom > readingLine;
  }) || workflowSections[0];
  workflowLinks.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${current.id}`));
}
window.addEventListener("scroll", updateWorkflowStep, { passive: true });
updateWorkflowStep();
$("#week").onchange=load; $("#edition").onchange=setEdition; $("#workspace-range").onchange=load; $("#reload").onclick=load; $("#save-config").onclick=saveConfig; $("#render").onclick=()=>action("render"); $("#render-bottom").onclick=()=>action("render");
$("#generate-description").onclick=()=>{$("#video-description").value=buildVideoDescription(); $("#status").textContent="已按本期顺序生成视频简介";};
$("#copy-description").onclick=async()=>{const text=$("#video-description").value.trim(); if(!text)return; await navigator.clipboard.writeText(text); $("#status").textContent="视频简介已复制";};
document.getElementById("save-global")?.addEventListener("click", saveConfig);
document.getElementById("regen-vo")?.addEventListener("click", () => {
  const tpl = $("#vo-tpl")?.value.trim() || "第{rank}名，{dance_type}街舞{title}，来自 {creator}。";
  const tplC = $("#vo-tpl-classic")?.value.trim() || "特别加映，{dance_type}街舞{title}，来自 {creator}。";
  const topIds = selectedOrder.filter(id => !specialIds.has(id)).slice(0, 5);
  const spId = [...specialIds][0];
  // 直接从 state.config 拿最新数据（不通过 candidates()，避免它内部 collectVisibleEdits 把 DOM 旧值再写回）
  const all = [...(state.config.this_week_candidates || []), ...(state.config.classics_pool || [])];
  const byId = new Map(all.map(c => [c.id, editBuffer.get(c.id) || c]));
  const fmt = (t, o) => t.replace(/\{(\w+)\}/g, (_, k) => o[k] ?? "");
  let n = 0;
  const upd = (id, vo) => {
    const src = byId.get(id) || {};
    const buf = { ...src, narration: vo };
    editBuffer.set(id, buf); n++;
  };
  topIds.forEach((id, i) => {
    const c = byId.get(id); if (!c) return;
    upd(id, fmt(tpl, { rank: i+1, dance_type: c.dance_type || "街舞", title: (c.song || c.title || "").replace(/[《》]/g, "").trim(), creator: (c.creator || "").replace(/^@/, "") }));
  });
  if (spId) {
    const c = byId.get(spId);
    if (c) upd(spId, fmt(tplC, { dance_type: c.dance_type || "街舞", title: (c.song || c.title || "").replace(/[《》]/g, "").trim(), creator: (c.creator || "").replace(/^@/, "") }));
  }
  renderCandidates();
  $("#status").textContent = `已按模板重新生成 ${n} 支入选口播，记得点保存`;
});
$("#platform-all").onchange=(event)=>{document.querySelectorAll(".platform").forEach((input)=>{input.checked=event.target.checked;});};
document.querySelectorAll(".platform").forEach((input)=>input.addEventListener("change",()=>{$("#platform-all").checked=selectedPlatforms().length===document.querySelectorAll(".platform").length;}));
document.querySelectorAll("#candidate-platform, #candidate-query, #candidate-min-likes, #candidate-tier, #candidate-sort, #candidate-download, #candidate-selected").forEach((input) => input.addEventListener(input.type === "search" || input.type === "number" ? "input" : "change", () => { collectVisibleEdits(); currentPage = 1; renderCandidates(); }));
$("#clear-candidate-filters").onclick = () => { $("#candidate-platform").value = ""; $("#candidate-query").value = ""; $("#candidate-min-likes").value = "0"; $("#candidate-tier").value = ""; $("#candidate-sort").value = "likes_desc"; $("#candidate-download").value = ""; $("#candidate-selected").checked = false; collectVisibleEdits(); currentPage = 1; renderCandidates(); };
$("#save-settings").onclick=async()=>{const settings={keywords:lines($("#keywords").value),platforms:selectedPlatforms(),top_limit:Number($("#top-limit").value),min_likes:Number($("#min-likes").value || 0),recent_days:Number($("#recent-days").value),sort_by:$("#sort-by").value,videos_only:$("#videos-only").checked};await api("/api/settings",{method:"POST",body:JSON.stringify(settings)});$("#status").textContent="发现规则已保存";};
$("#discover-action").onclick=async()=>{await $("#save-settings").onclick();action("discover");}; $("#download-action").onclick=()=>action("download"); $("#import-action").onclick=async()=>{const result=await api("/api/import",{method:"POST",body:JSON.stringify({week:$("#week").value})});state.config=result.config;state.selected=[];renderCandidates();$("#status").textContent="已同步候选与下载状态";};
$("#submission-form").onsubmit=async(event)=>{event.preventDefault();const url=$("#manual-url").value.trim();if(!url)return;try{const result=await api("/api/manual-link",{method:"POST",body:JSON.stringify({week:$("#week").value,url,note:$("#manual-note").value.trim()})});state.config=result.config;state.selected=(result.config.picks||[]).map(p=>p.id);selectedOrder=[...state.selected];currentPage=1;renderCandidates();event.currentTarget.reset();const p=result.parsed||{};$("#status").textContent=`已解析并入选：${p.source||""} ${p.creator||""} ${p.title||""}`.trim();document.getElementById("candidates")?.scrollIntoView({behavior:"smooth"});}catch(e){$("#status").textContent="投稿失败："+e.message;}};
initializeWorkspace().catch((error)=>$("#status").textContent=error.message);