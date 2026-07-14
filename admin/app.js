let state = { config: {}, selected: [] };
let selectedOrder = [];
const $ = (selector) => document.querySelector(selector);
const api = async (path, options = {}) => {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
};
const lines = (value) => value.split("\n").map((item) => item.trim()).filter(Boolean);
function candidates() { return [...document.querySelectorAll("#candidate-list .candidate-card")].map((row) => ({
  id: row.dataset.id, source: row.dataset.source, duration_sec: Number(row.dataset.duration || 0), like: Number(row.dataset.like || 0), play: Number(row.dataset.play || 0), tags: JSON.parse(row.dataset.tags || "[]"), url: row.dataset.url, local_path: row.dataset.localPath || "", manual_note: row.dataset.note || "", source_desc: row.dataset.sourceDesc || "", download_status: row.dataset.downloadStatus || "unknown", chosen: row.querySelector(".chosen").checked, order: Number(row.querySelector(".order").value || 999),
  dance_type: row.querySelector(".dance-type").value.trim(), title: row.querySelector(".title").value.trim(), creator: row.querySelector(".creator").value.trim(), narration: row.querySelector(".narration").value.trim(), difficulty: { stars: Number(row.querySelector(".stars").value), fit: row.querySelector(".dance-type").value.trim(), scores: {} }
})); }
function selected() { return candidates().filter((item) => item.chosen).sort((left, right) => left.order - right.order).map((item) => item.id); }
function renderCandidates() {
  const list = $("#candidate-list"); list.replaceChildren();
  const items = [...(state.config.this_week_candidates || []), ...(state.config.classics_pool || [])].sort((a,b) => (b.like || 0) - (a.like || 0));
  $("#candidate-count").textContent = items.length;
  for (const item of items) {
    const row = $("#candidate-template").content.firstElementChild.cloneNode(true);
    row.dataset.id=item.id; row.dataset.source=item.source || "抖音"; row.dataset.duration=item.duration_sec || 0; row.dataset.like=item.like || 0; row.dataset.play=item.play || 0; row.dataset.tags=JSON.stringify(item.tags || []); row.dataset.url=item.url || ""; row.dataset.localPath=item.local_path || ""; row.dataset.note=item.manual_note || ""; row.dataset.sourceDesc=item.source_desc || ""; row.dataset.downloadStatus=item.download_status || "unknown";
    row.querySelector(".chosen").checked=selectedOrder.includes(item.id); row.querySelector(".heat").innerHTML=`${Number(item.like || 0).toLocaleString()} <small>${Number(item.play || 0).toLocaleString()} 播放</small>`;
    const status = document.createElement("span"); status.className=`download ${row.dataset.downloadStatus}`; status.textContent={ready:"可下载",downloaded:"已下载",unavailable:"不可下载",failed:"下载失败",link_only:"已采集链接"}[row.dataset.downloadStatus] || "待检测"; row.querySelector(".download-cell").append(status);
    const danceType = item.dance_type || "Urban";
    row.querySelector(".dance-type").value=[...row.querySelector(".dance-type").options].some((option) => option.value === danceType) ? danceType : "Urban";
    row.querySelector(".order").value=selectedOrder.indexOf(item.id) + 1 || ""; row.querySelector(".title").value=item.title || ""; row.querySelector(".creator").value=item.creator || ""; row.querySelector(".narration").value=item.narration || item.source_desc || ""; row.querySelector(".stars").value=Math.round(item.difficulty?.stars || 3);
    const link=row.querySelector(".source-link"); link.href=item.url || "#"; link.textContent=item.url ? "打开" : "待补";
    row.querySelector(".chosen").addEventListener("change", (event) => { if (event.target.checked) selectedOrder.push(item.id); else selectedOrder = selectedOrder.filter((id) => id !== item.id); renderCandidates(); });
    row.querySelector(".order").addEventListener("change", () => { selectedOrder = selected(); }); list.append(row);
  }
}
function payload() { return { week: $("#week").value, episode: { week: $("#week").value }, candidates: candidates(), selected: selected().slice(0,6) }; }
function selectedPlatforms() { return [...document.querySelectorAll(".platform:checked")].map((input) => input.value); }
function renderPlatforms(platforms) { document.querySelectorAll(".platform").forEach((input) => { input.checked = platforms.includes(input.value); }); $("#platform-all").checked = selectedPlatforms().length === document.querySelectorAll(".platform").length; }
function renderWorkspaces(workspaces, activeWeek) {
  const weekSelect = $("#week");
  weekSelect.replaceChildren(...workspaces.map((workspace) => {
    const option = document.createElement("option");
    option.value = workspace.week; option.textContent = workspace.week; option.selected = workspace.week === activeWeek;
    return option;
  }));
  const list = $("#workspace-list"); list.replaceChildren();
  for (const workspace of workspaces) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "workspace-item";
    button.classList.toggle("active", workspace.week === activeWeek);
    button.textContent = workspace.configured ? `${workspace.week} · ${workspace.candidates} 候选 · ${workspace.selected} 已编排` : `${workspace.week} · 未开始`;
    button.onclick = () => { if (workspace.week !== $("#week").value) { $("#week").value = workspace.week; load(); } };
    list.append(button);
  }
}
async function load() { state=await api(`/api/state?week=${encodeURIComponent($("#week").value)}`); selectedOrder=[...state.selected]; const settings=state.settings; $("#keywords").value=settings.keywords.join("\n"); $("#top-limit").value=settings.top_limit; $("#min-likes").value=settings.min_likes || 0; $("#recent-days").value=settings.recent_days || 7; $("#sort-by").value=settings.sort_by || "heat_desc"; $("#videos-only").checked=settings.videos_only !== false; renderPlatforms(settings.platforms || ["douyin"]); renderCandidates(); const workspaceData=await api(`/api/workspaces?recent=${encodeURIComponent($("#workspace-range").value)}`); renderWorkspaces(workspaceData.workspaces, state.week); $("#status").textContent=`${state.week} 已载入`; }
async function saveConfig() { const result=await api("/api/save",{method:"POST",body:JSON.stringify(payload())}); state.config=result.config; state.selected=selected(); $("#status").textContent="本期编排已保存"; }
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
$("#week").append(new Option(currentIsoWeek(), currentIsoWeek()));
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
$("#week").onchange=load; $("#workspace-range").onchange=load; $("#reload").onclick=load; $("#save-config").onclick=saveConfig; $("#render").onclick=()=>action("render"); $("#render-bottom").onclick=()=>action("render");
$("#platform-all").onchange=(event)=>{document.querySelectorAll(".platform").forEach((input)=>{input.checked=event.target.checked;});};
document.querySelectorAll(".platform").forEach((input)=>input.addEventListener("change",()=>{$("#platform-all").checked=selectedPlatforms().length===document.querySelectorAll(".platform").length;}));
$("#save-settings").onclick=async()=>{const settings={keywords:lines($("#keywords").value),platforms:selectedPlatforms(),top_limit:Number($("#top-limit").value),min_likes:Number($("#min-likes").value || 0),recent_days:Number($("#recent-days").value),sort_by:$("#sort-by").value,videos_only:$("#videos-only").checked};await api("/api/settings",{method:"POST",body:JSON.stringify(settings)});$("#status").textContent="发现规则已保存";};
$("#discover-action").onclick=async()=>{await $("#save-settings").onclick();action("discover");}; $("#download-action").onclick=()=>action("download"); $("#import-action").onclick=async()=>{const result=await api("/api/import",{method:"POST",body:JSON.stringify({week:$("#week").value})});state.config=result.config;state.selected=[];renderCandidates();$("#status").textContent="已同步候选与下载状态";};
$("#submission-form").onsubmit=async(event)=>{event.preventDefault();const url=$("#manual-url").value.trim();if(!url)return;const result=await api("/api/manual-link",{method:"POST",body:JSON.stringify({week:$("#week").value,url,note:$("#manual-note").value.trim()})});state.config=result.config;renderCandidates();event.currentTarget.reset();$("#status").textContent="投稿已加入候选池";};
load().catch((error)=>$("#status").textContent=error.message);