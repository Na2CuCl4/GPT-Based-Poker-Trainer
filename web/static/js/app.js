/* ============================================================
   Poker Trainer — Frontend Logic
   ============================================================ */
"use strict";

const socket = io();
const SHOW_STYLES = window.APP_CONFIG?.show_opponent_styles !== false;

// Seat positions [left%, top%] distributed around the 2:1 oval table.
// Calculated at 75% oval radius (r=38%) from center, clockwise from bottom-left.
const SEAT_POSITIONS = {
  1: [[50, 12]],
  2: [[23, 23], [77, 23]],
  3: [[23, 23], [50, 12], [77, 23]],
  4: [[12, 50], [23, 23], [77, 23], [88, 50]],
  5: [[30, 70], [30, 30], [50, 12], [70, 30], [70, 70]],
};

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
let gameState = null;
let validActions = [];
let raiseMin = 0, raiseMax = 1000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function isHumanTurn(state) {
  if (!state) return false;
  const human = state.players.find(p => p.is_human);
  return human && state.current_player_idx === human.idx && state.street !== "showdown";
}

/** Enable/disable ALL action buttons based on whether it's the human's turn. */
function setActionButtonsEnabled(enabled) {
  ["btn-fold", "btn-check", "btn-call", "btn-raise", "btn-allin", "btn-hint"].forEach(id => {
    document.getElementById(id).disabled = !enabled;
  });
  if (!enabled) {
    document.getElementById("call-amount").textContent = "";
  }
}

// ---------------------------------------------------------------------------
// Socket events
// ---------------------------------------------------------------------------
socket.on("state_update", ({ state, valid_actions, street_changed }) => {
  gameState = state;
  validActions = valid_actions;
  renderTable(state);
  if (isHumanTurn(state)) {
    updateActions(valid_actions);
  } else {
    setActionButtonsEnabled(false);
  }
  if (street_changed) logStreet(state.street);
});

socket.on("ai_action", ({ player_name, action, amount }) => {
  addLog(player_name, action, amount);
});

socket.on("hand_result", ({ state, result }) => {
  gameState = state;
  renderTable(state, true);
  setActionButtonsEnabled(false);
  logHandEnd(result, state);
  showHandResult(result, state);
  showNextHandButton();
  refreshStats();
});

socket.on("hand_analysis", ({ analysis }) => {
  appendAnalysisToModal(analysis);
});

socket.on("run_it_twice_prompt", ({ ai_run_twice, ai_reasoning }) => {
  showRunItTwiceDialog(ai_run_twice, ai_reasoning);
});

// ---------------------------------------------------------------------------
// Session management
// ---------------------------------------------------------------------------
async function startSession() {
  hideHint();
  clearLog();
  document.getElementById("btn-next").style.display = "none";
  document.getElementById("result-modal").style.display = "none";
  document.getElementById("rit-modal").style.display = "none";

  const res = await fetch("/api/session/start", { method: "POST" });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  gameState = data.state;
  validActions = data.valid_actions;
  renderTable(data.state);
  if (isHumanTurn(data.state)) {
    updateActions(data.valid_actions);
  } else {
    setActionButtonsEnabled(false);
  }
  logStreet(data.state.street);
  document.getElementById("hint-panel").style.display = "none";
}

async function nextHand() {
  hideHint();
  document.getElementById("btn-next").style.display = "none";
  closeModal();

  const res = await fetch("/api/game/next-hand", { method: "POST" });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  gameState = data.state;
  validActions = data.valid_actions;
  renderTable(data.state);
  if (isHumanTurn(data.state)) {
    updateActions(data.valid_actions);
  } else {
    setActionButtonsEnabled(false);
  }
  logStreet(data.state.street);
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function sendAction(action, amount = 0) {
  // Disable buttons immediately to prevent double-click
  setActionButtonsEnabled(false);

  const res = await fetch("/api/game/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, amount }),
  });
  const data = await res.json();
  if (data.error) {
    alert(data.error);
    // Re-enable if still human's turn
    if (gameState && isHumanTurn(gameState)) updateActions(validActions);
    return;
  }

  addLog("你", action, amount);

  if (data.run_it_twice_pending) {
    // Waiting for run-it-twice socket event — keep buttons disabled
    gameState = data.state;
    renderTable(data.state);
    addLogRaw(`<span class="log-street">── 等待发牌决策… ──</span>`);
    return;
  }

  if (data.result?.hand_over) {
    // hand_result will arrive via socket
  } else {
    gameState = data.state;
    validActions = data.valid_actions;
    renderTable(data.state);
    if (isHumanTurn(data.state)) {
      updateActions(data.valid_actions);
    }
    // else: buttons stay disabled, AI will emit state_update when done
    if (data.result?.street_changed) logStreet(data.state.street);
  }
}

function sendRaise() {
  const amount = parseInt(document.getElementById("raise-input").value, 10) || raiseMin;
  sendAction("raise", amount);
}

async function requestHint() {
  // On mobile (sidebar is a fixed popup), auto-open it so the hint is visible
  const sidePanel = document.querySelector(".side-panel");
  if (getComputedStyle(sidePanel).position === "fixed") {
    sidePanel.classList.add("open");
    document.getElementById("sidebar-overlay").classList.add("open");
  }

  const panel = document.getElementById("hint-panel");
  const content = document.getElementById("hint-content");
  panel.style.display = "block";
  content.innerHTML = "<em style='color:#94a3b8'>正在获取建议…</em>";

  const res = await fetch("/api/game/hint", { method: "POST" });
  const hint = await res.json();
  if (hint.error) { content.textContent = hint.error; return; }

  const confidenceColor = hint.confidence === "高" ? "#55efc4"
    : hint.confidence === "中" ? "#fdcb6e" : "#b2bec3";

  const actionLabels = {
    fold: "弃牌", check: "让牌", call: "跟注",
    raise: `加注至 ${hint.raise_to ?? ""}`, all_in: "全押",
  };

  content.innerHTML = `
    <div class="hint-action">${actionLabels[hint.action] || hint.action}
      <small style="color:${confidenceColor};font-size:.85em;margin-left:6px">
        置信度：${hint.confidence}
      </small>
    </div>
    <div class="hint-row"><span class="hint-label">建议：</span>${hint.explanation}</div>
    <div class="hint-row"><span class="hint-label">手牌强度：</span>${hint.hand_strength_desc}</div>
    <div class="hint-row"><span class="hint-label">底池赔率：</span>${hint.pot_odds_note}</div>
  `;
}

function hideHint() {
  document.getElementById("hint-panel").style.display = "none";
}

// ---------------------------------------------------------------------------
// Render table
// ---------------------------------------------------------------------------
const STREET_CN = {
  preflop: "翻牌前", flop: "翻牌", turn: "转牌", river: "河牌", showdown: "摊牌"
};
const STYLE_CN = {
  tight_aggressive: "紧凶", loose_aggressive: "松凶",
  tight_passive: "紧弱", loose_passive: "松弱", balanced: "均衡",
};

function renderTable(state, revealAll = false) {
  document.getElementById("hand-number").textContent = `第 ${state.hand_number} 手`;
  document.getElementById("pot-amount").textContent = state.pot;
  document.getElementById("street-label").textContent = STREET_CN[state.street] || state.street;

  renderCommunityCards(state.community_cards);

  const human = state.players.find(p => p.is_human);
  const humanTurn = human && state.current_player_idx === human.idx && state.street !== "showdown";

  const seat = document.getElementById("player-seat");
  seat.classList.toggle("current-turn", !!humanTurn);
  if (human) {
    const isDealer = state.dealer_idx === human.idx;
    const dealerMark = isDealer ? `<span class="dealer-btn">D</span>` : "";
    const actionBadge = human.last_action && !human.is_folded && !humanTurn
      ? `<span class="seat-action-badge">${actionCN(human.last_action, human.last_action_amount)}</span>`
      : "";
    const betInfo = human.current_bet > 0
      ? `<span class="seat-bet"> | 下注: ${human.current_bet}</span>` : "";
    const actionLine = actionBadge || human.is_folded
      ? `${actionBadge}${human.is_folded ? `<span class="folded-label">已弃牌</span>` : ""}`
      : "";
    seat.innerHTML = `
      <div class="seat-info">
        <div>
          <span class="seat-name${humanTurn ? " current-turn-name" : ""}">
            ${getDisplayName(human)}${dealerMark}
          </span>
        </div>
        <div><span class="seat-chips">筹码: ${human.chips}</span>${betInfo}</div>
      </div>
      <div class="card-row">${renderCardRow(human.hole_cards, true, human.is_folded)}</div>
      <div class="seat-action-row">${actionLine}</div>
    `;
  }

  renderOpponents(state.players, state.dealer_idx, state.current_player_idx, revealAll);
}

function renderCommunityCards(cards) {
  const el = document.getElementById("community-cards");
  el.innerHTML = "";
  (cards || []).forEach(c => { el.innerHTML += cardHtml(c, true); });
  for (let i = (cards || []).length; i < 5; i++) {
    el.innerHTML += `<div class="card-placeholder"></div>`;
  }
}

function renderCardRow(cards, visible, folded = false) {
  if (!cards) return "";
  return cards.map(c => cardHtml(c, visible, folded)).join("");
}

function cardHtml(c, visible, folded = false) {
  if (!c || !visible) return `<div class="card-back"></div>`;
  const overlay = folded ? `<div class="card-fold-overlay"></div>` : "";
  return `<div class="card ${c.color}">
    <span class="card-rank">${c.rank === "T" ? "10" : c.rank}</span>
    <span class="card-suit">${suitUnicode(c.suit)}</span>
    ${overlay}
  </div>`;
}

function suitUnicode(suit) {
  return { s: "♠", h: "♥", d: "♦", c: "♣" }[suit] || suit;
}

function getDisplayName(p) {
  const adj = p.chip_adjustment || 0;
  if (adj < 0) return `${p.name} (${adj})`;
  if (adj > 0) return `${p.name} (+${adj})`;
  return p.name;
}

function opponentCardBackHtml(isActive) {
  const cls = isActive ? "card-back active" : "card-back";
  return `<div class="${cls}"></div>`;
}

function renderOpponents(players, dealerIdx, currentPlayerIdx, revealAll) {
  const arc = document.getElementById("opponents-arc");
  arc.innerHTML = "";
  const opponents = players.filter(p => !p.is_human);
  const positions = SEAT_POSITIONS[opponents.length] || [];
  opponents.forEach((p, i) => {
    const [left, top] = positions[i] || [50, 20];
    const isDealer  = p.idx === dealerIdx;
    const isCurrent = p.idx === currentPlayerIdx;
    const dealerMark = isDealer ? `<span class="dealer-btn">D</span>` : "";
    const actionBadge = p.last_action && !p.is_folded && !isCurrent
      ? `<span class="seat-action-badge">${actionCN(p.last_action, p.last_action_amount)}</span>`
      : "";
    const betInfo = p.current_bet > 0
      ? `<span class="seat-bet"> | 下注: ${p.current_bet}</span>` : "";

    const stylePart = SHOW_STYLES
      ? `<span style="font-size:.8em;color:#74b9ff"> [${STYLE_CN[p.style] || p.style}]</span>`
      : "";

    const cardHtmlStr = revealAll
      ? renderCardRow(p.hole_cards, true)
      : (p.hole_cards || []).map(() => opponentCardBackHtml(!p.is_folded)).join("");

    const actionLine = actionBadge || p.is_folded
      ? `${actionBadge}${p.is_folded ? `<span class="folded-label">已弃牌</span>` : ""}`
      : "";

    arc.innerHTML += `
      <div class="seat opponent-seat${isCurrent ? " current-turn" : ""}" style="left:${left}%;top:${top}%">
        <div class="seat-info">
          <div>
            <span class="seat-name${isCurrent ? " current-turn-name" : ""}">
              ${getDisplayName(p)}${dealerMark}
            </span>${stylePart}
          </div>
          <div><span class="seat-chips">筹码: ${p.chips}</span>${betInfo}</div>
        </div>
        <div class="card-row">${cardHtmlStr}</div>
        <div class="seat-action-row">${actionLine}</div>
      </div>`;
  });
}

function actionCN(action, amount) {
  const map = {
    fold: "弃牌", check: "让牌", call: "跟注",
    raise: `加注 ${amount}`, all_in: "全押",
    small_blind: `小盲 ${amount}`, big_blind: `大盲 ${amount}`, ante: `Ante ${amount}`,
  };
  return map[action] || action;
}

// ---------------------------------------------------------------------------
// Update action buttons
// ---------------------------------------------------------------------------
function updateActions(actions) {
  const byAction = {};
  (actions || []).forEach(a => { byAction[a.action] = a; });

  const setBtn = (id, enabled) => { document.getElementById(id).disabled = !enabled; };

  setBtn("btn-fold",  !!byAction.fold);
  setBtn("btn-check", !!byAction.check);
  setBtn("btn-allin", !!byAction.all_in);
  setBtn("btn-hint",  true);  // hint always enabled during human turn

  if (byAction.call) {
    setBtn("btn-call", true);
    const ca = byAction.call.call_amount;
    document.getElementById("call-amount").textContent = ca > 0 ? `(${ca})` : "";
  } else {
    setBtn("btn-call", false);
    document.getElementById("call-amount").textContent = "";
  }

  if (byAction.raise) {
    setBtn("btn-raise", true);
    raiseMin = byAction.raise.min_amount;
    raiseMax = byAction.raise.max_amount;
    const slider = document.getElementById("raise-slider");
    slider.min = raiseMin; slider.max = raiseMax; slider.value = raiseMin;
    document.getElementById("raise-input").value = raiseMin;
  } else {
    setBtn("btn-raise", false);
  }
}

function onSliderChange(val) {
  document.getElementById("raise-input").value = val;
}
function onRaiseInputChange(val) {
  const v = Math.min(Math.max(parseInt(val) || raiseMin, raiseMin), raiseMax);
  document.getElementById("raise-slider").value = v;
}

// ---------------------------------------------------------------------------
// Run-it-twice dialog
// ---------------------------------------------------------------------------
function showRunItTwiceDialog(aiRunTwice, aiReasoning) {
  const choiceEl = document.getElementById("rit-ai-choice");
  const reasonEl = document.getElementById("rit-ai-reason");
  const hintContent = document.getElementById("rit-hint-content");

  choiceEl.textContent = aiRunTwice ? "✅ 同意发两次" : "❌ 选择发一次";
  choiceEl.style.color = aiRunTwice ? "#55efc4" : "#fdcb6e";
  reasonEl.textContent = aiReasoning || "";
  hintContent.style.display = "none";
  hintContent.innerHTML = "";

  document.getElementById("rit-modal").style.display = "flex";
}

async function requestRunItTwiceHint() {
  const btn = document.getElementById("rit-hint-btn");
  const content = document.getElementById("rit-hint-content");
  btn.disabled = true;
  btn.textContent = "正在获取建议…";
  content.style.display = "block";
  content.innerHTML = "<em style='color:#94a3b8'>AI 思考中…</em>";

  try {
    const res = await fetch("/api/game/run-it-twice-hint", { method: "POST" });
    const data = await res.json();
    if (data.error) {
      content.textContent = data.error;
    } else {
      const rec = data.run_twice ? "✅ 建议发两次" : "❌ 建议发一次";
      const color = data.run_twice ? "#55efc4" : "#fdcb6e";
      content.innerHTML = `
        <div style="color:${color};font-weight:bold">${rec}</div>
        <div style="color:#94a3b8;margin-top:4px">${data.reasoning}</div>
      `;
    }
  } catch (e) {
    content.textContent = `获取失败: ${e}`;
  }

  btn.disabled = false;
  btn.textContent = "💡 咨询 AI 建议";
}

async function submitRunItTwice(runTwice) {
  document.getElementById("rit-modal").style.display = "none";
  addLogRaw(`<span class="log-street">── ${runTwice ? "发两次" : "发一次"} ──</span>`);

  await fetch("/api/game/run-it-twice", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_twice: runTwice }),
  });
  // Result will arrive via hand_result socket event
}

// ---------------------------------------------------------------------------
// Result modal
// ---------------------------------------------------------------------------

function showHandResult(result, state) {
  const pots = result.side_pot_results || [];
  const winners = pots.length > 0 ? pots[0].winners.join("、") : "未知";
  const handName = pots[0]?.hand_name || "";

  let bodyHtml = "";

  if (result.run_twice) {
    // Run-twice: show two sets of community cards
    bodyHtml += _buildRunTwiceResultHtml(result);
  } else {
    // Normal: show single community cards
    const communityCards = state.community_cards || [];
    if (communityCards.length > 0) {
      bodyHtml += `<div class="result-community">
        <div class="result-community-label">公共牌</div>
        <div class="card-row">${renderCardRow(communityCards, true)}</div>
       </div>`;
    }
    bodyHtml += `<div class="result-winner">🏆 赢家：${winners}${handName ? "（" + handName + "）" : ""}</div>`;
  }

  // Hole card reveal
  let revealHtml = "";
  const reveal = result.reveal || {};
  Object.entries(reveal).forEach(([name, cards]) => {
    revealHtml += `
      <div class="result-reveal-row">
        <span class="result-reveal-name">${name}</span>
        <div class="result-reveal-cards">${renderCardRow(cards, true)}</div>
      </div>`;
  });

  document.getElementById("result-body").innerHTML = bodyHtml;
  document.getElementById("result-reveal").innerHTML = revealHtml || "";

  document.getElementById("analysis-body").innerHTML = `
    <hr style="border-color:#2d3f55;margin:12px 0"/>
    <div id="analysis-trigger">
      <button class="btn btn-secondary" onclick="requestAnalysis()" style="width:100%">🤖 AI 分析本局</button>
    </div>
    <div id="analysis-loading" style="display:none;color:#94a3b8;font-size:0.9rem">⏳ AI 正在分析本局…</div>
  `;

  document.getElementById("result-modal").style.display = "flex";
}

async function requestAnalysis() {
  const trigger = document.getElementById("analysis-trigger");
  const loading = document.getElementById("analysis-loading");
  if (trigger) trigger.style.display = "none";
  if (loading) loading.style.display = "block";
  await fetch("/api/game/analyze", { method: "POST" });
  // Result arrives via hand_analysis socket event → appendAnalysisToModal
}

function _buildRunTwiceResultHtml(result) {
  const r1 = result.run_1_results || [];
  const r2 = result.run_2_results || [];
  const comm1 = result.run_1_community || [];
  const comm2 = result.run_2_community || [];

  const potSummary = (runResults) => runResults.map(p =>
    `${p.winners.join("、")}${p.hand_name ? "（" + p.hand_name + "）" : ""} 赢 ${p.pot_amount}`
  ).join("；");

  const cardRowFromDicts = (cards) => (cards || []).map(c => cardHtml(c, true)).join("");

  let html =`<div style="font-size:0.85rem;color:#74b9ff;margin-bottom:8px">🃏 本局发了两次</div>`;

  html += `<div class="result-community">
    <div class="result-community-label">第一次公共牌</div>
    <div class="card-row">${cardRowFromDicts(comm1)}</div>
    <div style="font-size:0.85rem;color:#55efc4;margin-top:4px">${potSummary(r1)}</div>
  </div>`;

  html += `<div class="result-community" style="margin-top:8px">
    <div class="result-community-label">第二次公共牌</div>
    <div class="card-row">${cardRowFromDicts(comm2)}</div>
    <div style="font-size:0.85rem;color:#55efc4;margin-top:4px">${potSummary(r2)}</div>
  </div>`;

  // Combined winners
  const pots = result.side_pot_results || [];
  const totalWinners = pots.length > 0 ? pots[0].winners.join("、") : "未知";
  html += `<div class="result-winner" style="margin-top:12px">🏆 综合结果：${totalWinners}</div>`;

  return html;
}

function appendAnalysisToModal(analysis) {
  const container = document.getElementById("analysis-body");
  if (!container) return;

  const scoreColor = analysis.overall_score >= 70 ? "#55efc4"
    : analysis.overall_score >= 40 ? "#fdcb6e" : "#ff7675";

  const evalsHtml = (analysis.key_decision_evals || []).map(ev => `
    <div class="decision-eval ${ev.is_optimal ? "good" : "bad"}">
      <strong>${ev.street}</strong>：${ev.player_action}
      ${!ev.is_optimal ? `→ 建议 <em>${ev.suggested_action}</em>` : " ✓"}
      <br><small>${ev.reason}</small>
    </div>
  `).join("");

  const tipsHtml = (analysis.tips || []).map(t => `<li>${t}</li>`).join("");

  container.innerHTML = `
    <hr style="border-color:#2d3f55;margin:12px 0"/>
    <div>评分: <span class="analysis-score" style="color:${scoreColor}">${analysis.overall_score}</span>/100</div>
    <div class="analysis-summary">${analysis.summary}</div>
    ${evalsHtml}
    <div class="analysis-lesson">💡 本局要点：${analysis.main_lesson}</div>
    <ul class="analysis-tips">${tipsHtml}</ul>
  `;
}

function closeModal() {
  document.getElementById("result-modal").style.display = "none";
}

function showNextHandButton() {
  document.getElementById("btn-next").style.display = "inline-block";
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------
async function refreshStats() {
  const res = await fetch("/api/stats");
  const stats = await res.json();
  if (stats.error) return;

  const panel = document.getElementById("stats-panel");
  panel.style.display = "block";

  const pct = v => `${(v * 100).toFixed(1)}%`;
  let html = `
    <div class="stat-row"><span class="stat-label">胜率</span><span class="stat-value">${pct(stats.win_rate)}</span></div>
    <div class="stat-row"><span class="stat-label">已玩手数</span><span class="stat-value">${stats.hands_played}</span></div>
    <div class="stat-row"><span class="stat-label">VPIP</span><span class="stat-value">${pct(stats.vpip)}</span></div>
    <div class="stat-row"><span class="stat-label">PFR</span><span class="stat-value">${pct(stats.pfr)}</span></div>
  `;
  document.getElementById("stats-content").innerHTML = html;
}

// ---------------------------------------------------------------------------
// Action log
// ---------------------------------------------------------------------------
function addLog(player, action, amount) {
  const ul = document.getElementById("action-log");
  const li = document.createElement("li");
  const actionClass = action === "raise" || action === "all_in" ? "log-action-raise"
    : action === "fold" ? "log-action-fold" : "";
  li.innerHTML = `<span class="log-player">${player}</span>
    <span class="${actionClass}"> ${actionCN(action, amount)}</span>`;
  ul.prepend(li);
}

function addLogRaw(html) {
  const ul = document.getElementById("action-log");
  const li = document.createElement("li");
  li.innerHTML = html;
  ul.prepend(li);
}

function logHandEnd(result, state) {
  if (state?.players) {
    [...state.players].reverse().forEach(p => {
      const before = p.chips_before_hand ?? p.chips;
      const net = p.chips - before;
      const sign = net >= 0 ? "+" : "";
      addLogRaw(`<span class="log-chips">${getDisplayName(p)}: ${sign}${net} (余${p.chips})</span>`);
    });
  }
  // Aggregate multiple side-pots by winner set → one log entry per winner group
  const pots = result.side_pot_results || [];
  const winMap = new Map();
  pots.forEach(pot => {
    const key = [...pot.winners].sort().join(",");
    if (!winMap.has(key)) winMap.set(key, { winners: pot.winners, total: 0, hand_name: "" });
    const e = winMap.get(key);
    e.total += pot.pot_amount;
    if (pot.hand_name) e.hand_name = pot.hand_name;
  });
  [...winMap.values()].reverse().forEach(e => {
    const hn = e.hand_name ? `（${e.hand_name}）` : "";
    addLogRaw(`<span class="log-result">🏆 ${e.winners.join("、")} 赢 ${e.total}${hn}</span>`);
  });
  addLogRaw(`<span class="log-street">── 本局结束 ──</span>`);
}

function logStreet(street) {
  const ul = document.getElementById("action-log");
  const li = document.createElement("li");
  li.innerHTML = `<span class="log-street">── ${STREET_CN[street] || street} ──</span>`;
  ul.prepend(li);
}

function clearLog() {
  document.getElementById("action-log").innerHTML = "";
}

// ---------------------------------------------------------------------------
// Sidebar popup (mobile / portrait mode)
// ---------------------------------------------------------------------------
function toggleSidebar() {
  const panel = document.querySelector(".side-panel");
  const overlay = document.getElementById("sidebar-overlay");
  const opening = !panel.classList.contains("open");
  panel.classList.toggle("open", opening);
  overlay.classList.toggle("open", opening);
}

function closeSidebar() {
  document.querySelector(".side-panel").classList.remove("open");
  document.getElementById("sidebar-overlay").classList.remove("open");
}
