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
  id: row.dataset.id, source: row.dataset.source, duration_sec: Number(row.dataset.duration || 0), like: Number(row.dataset.like || 0), play: Number(row.dataset.play || 0), tags: JSON.parse(row.dataset.tags || "[]"), url: row.dataset.url, local_path: row.dataset.localPath || "", manual_note: row.dataset.note || "", source_desc: row.dataset.sourceDesc || "", download_status: row.dataset.downloadStatus || "unknown", candidate_tier: row.dataset.candidateTier || "top", chosen: row.querySelector(".chosen").checked, order: Number(row.querySelector(".order").value || 999),
  dance_type: row.querySelector(".dance-type").value.trim(), title: row.querySelector(".title").value.trim(), creator: row.querySelector(".creator").value.trim(), narration: row.querySelector(".narration").value.trim(), voice: row.querySelector(".voice").value, voice_rate: row.querySelector(".voice-rate").value, clip_start_sec: Number(row.querySelector(".clip-start").value || 0), clip_end_sec: Number(row.querySelector(".clip-end").value || 0), difficulty: { stars: Number(row.querySelector(".stars").value), fit: row.querySelector(".dance-type").value.trim(), scores: {} }
})); }
function selected() { return candidates().filter((item) => item.chosen).sort((left, right) => left.order - right.order).map((item) => item.id); }
function defaultNarration(item) {
  const order = selectedOrder.indexOf(item.id);
  const prefix = order === 5 ? "特别加映" : order >= 0 ? `第${order + 1}名` : "本周推荐";
  const creator = (item.creator || "这位编舞者").replace(/^@/, "");
  return `${prefix}，${item.dance_type || "街舞"}，来自 ${creator}。`;
}
function renderCandidates() {
  const list = $("#candidate-list"); list.replaceChildren();
  const items = [...(state.config.this_week_candidates || []), ...(state.config.classics_pool || [])].sort((a,b) => (a.candidate_tier === "backup") - (b.candidate_tier === "backup") || (b.like || 0) - (a.like || 0));
  $("#candidate-count").textContent = items.length;
  for (const item of items) {
    const row = $("#candidate-template").content.firstElementChild.cloneNode(true);
    row.dataset.id=item.id; row.dataset.source=item.source || "抖音"; row.dataset.duration=item.duration_sec || 0; row.dataset.like=item.like || 0; row.dataset.play=item.play || 0; row.dataset.tags=JSON.stringify(item.tags || []); row.dataset.url=item.url || ""; row.dataset.localPath=item.local_path || ""; row.dataset.note=item.manual_note || ""; row.dataset.sourceDesc=item.source_desc || ""; row.dataset.downloadStatus=item.download_status || "unknown"; row.dataset.candidateTier=item.candidate_tier || "top";
    row.querySelector(".chosen").checked=selectedOrder.includes(item.id); row.querySelector(".heat").innerHTML=`${Number(item.like || 0).toLocaleString()} <small>${Number(item.play || 0).toLocaleString()} 播放</small>`;
    const status = document.createElement("span"); status.className=`download ${row.dataset.downloadStatus}`; status.textContent={ready:"可下载",downloaded:"已下载",unavailable:"不可下载",failed:"下载失败",link_only:"已采集链接"}[row.dataset.downloadStatus] || "待检测"; row.querySelector(".download-cell").append(status);
    const tier = document.createElement("span"); tier.className=`candidate-tier ${row.dataset.candidateTier}`; tier.textContent=row.dataset.candidateTier === "backup" ? "备选" : "TOP10"; row.querySelector(".heat").before(tier);
    const danceType = item.dance_type || "Urban";
    row.querySelector(".dance-type").value=[...row.querySelector(".dance-type").options].some((option) => option.value === danceType) ? danceType : "Urban";
    row.querySelector(".order").value=selectedOrder.indexOf(item.id) + 1 || ""; row.querySelector(".title").value=item.title || ""; row.querySelector(".creator").value=item.creator || ""; row.querySelector(".narration").value=item.narration || defaultNarration(item); row.querySelector(".clip-start").value=item.clip_start_sec || 0; row.querySelector(".clip-end").value=item.clip_end_sec || item.duration_sec || ""; row.querySelector(".voice").value=item.voice || "zh-CN-XiaoyiNeural"; row.querySelector(".voice-rate").value=item.voice_rate || "+20%"; row.querySelector(".stars").value=Math.round(item.difficulty?.stars || 3);
    const link=row.querySelector(".source-link"); link.href=item.url || "#"; link.textContent=item.url ? "原平台预览" : "原链接待补";
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
        const result = await api("/api/voice-preview", { method: "POST", body: JSON.stringify({ week: $("#week").value, candidate_id: item.id, text: row.querySelector(".narration").value.trim(), voice: row.querySelector(".voice").value, rate: row.querySelector(".voice-rate").value }) });
        player.src = `${result.audio_url}?v=${Date.now()}`; player.hidden = false; await player.play(); status.textContent = "试听已生成";
      } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; }
    });
    row.querySelector(".chosen").addEventListener("change", (event) => { if (event.target.checked) selectedOrder.push(item.id); else selectedOrder = selectedOrder.filter((id) => id !== item.id); renderCandidates(); });
    row.querySelector(".order").addEventListener("change", () => { selectedOrder = selected(); }); list.append(row);
  }
}
function payload() { return { week: $("#week").value, episode: { week: $("#week").value }, candidates: candidates(), selected: selected().slice(0,6), video_description: $("#video-description").value.trim() }; }
function buildVideoDescription() {
  const all = new Map(candidates().map((item) => [item.id, item]));
  const ranked = selected().map((id) => all.get(id)).filter(Boolean);
  const week = $("#week").value.replace(/^(\d{4})-W(\d{2})(?:-([AB]))?$/, (_, year, number, edition) => `${year} 年第${number}周${edition === "A" ? "上部" : edition === "B" ? "下部" : ""}`);
  if (!ranked.length) return `${week}热舞又来啦！先在候选池勾选并排好本期视频，再生成排行榜。`;
  const list = ranked.map((item, index) => {
    const prefix = index < 5 ? `${index + 1}.` : "特别加映：";
    const title = item.title ? `《${item.title.replace(/[《》]/g, "")}》` : item.dance_type || "本周编舞";
    const creator = item.creator || "原作者待补充";
    return `${prefix} ${title} · ${creator}\n${item.url || "原链接待补充"}`;
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
async function load() { state=await api(`/api/state?week=${encodeURIComponent($("#week").value)}`); selectedOrder=[...state.selected]; syncEdition(); const settings=state.settings; $("#keywords").value=settings.keywords.join("\n"); $("#top-limit").value=settings.top_limit; $("#min-likes").value=settings.min_likes || 0; $("#recent-days").value=settings.recent_days || 7; $("#sort-by").value=settings.sort_by || "heat_desc"; $("#videos-only").checked=settings.videos_only !== false; renderPlatforms(settings.platforms || ["douyin"]); renderCandidates(); $("#video-description").value=state.config.metadata?.video_description || buildVideoDescription(); const workspaceData=await api(`/api/workspaces?recent=${encodeURIComponent($("#workspace-range").value)}`); renderWorkspaces(workspaceData.workspaces, state.week); $("#status").textContent=`${state.week} 已载入`; }
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
$("#week").onchange=load; $("#edition").onchange=setEdition; $("#workspace-range").onchange=load; $("#reload").onclick=load; $("#save-config").onclick=saveConfig; $("#render").onclick=()=>action("render"); $("#render-bottom").onclick=()=>action("render");
$("#generate-description").onclick=()=>{$("#video-description").value=buildVideoDescription(); $("#status").textContent="已按本期顺序生成视频简介";};
$("#copy-description").onclick=async()=>{const text=$("#video-description").value.trim(); if(!text)return; await navigator.clipboard.writeText(text); $("#status").textContent="视频简介已复制";};
$("#platform-all").onchange=(event)=>{document.querySelectorAll(".platform").forEach((input)=>{input.checked=event.target.checked;});};
document.querySelectorAll(".platform").forEach((input)=>input.addEventListener("change",()=>{$("#platform-all").checked=selectedPlatforms().length===document.querySelectorAll(".platform").length;}));
$("#save-settings").onclick=async()=>{const settings={keywords:lines($("#keywords").value),platforms:selectedPlatforms(),top_limit:Number($("#top-limit").value),min_likes:Number($("#min-likes").value || 0),recent_days:Number($("#recent-days").value),sort_by:$("#sort-by").value,videos_only:$("#videos-only").checked};await api("/api/settings",{method:"POST",body:JSON.stringify(settings)});$("#status").textContent="发现规则已保存";};
$("#discover-action").onclick=async()=>{await $("#save-settings").onclick();action("discover");}; $("#download-action").onclick=()=>action("download"); $("#import-action").onclick=async()=>{const result=await api("/api/import",{method:"POST",body:JSON.stringify({week:$("#week").value})});state.config=result.config;state.selected=[];renderCandidates();$("#status").textContent="已同步候选与下载状态";};
$("#submission-form").onsubmit=async(event)=>{event.preventDefault();const url=$("#manual-url").value.trim();if(!url)return;const result=await api("/api/manual-link",{method:"POST",body:JSON.stringify({week:$("#week").value,url,note:$("#manual-note").value.trim()})});state.config=result.config;renderCandidates();event.currentTarget.reset();$("#status").textContent="投稿已加入候选池";};
load().catch((error)=>$("#status").textContent=error.message);