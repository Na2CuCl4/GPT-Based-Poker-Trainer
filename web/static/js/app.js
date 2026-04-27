/* ============================================================
   Poker Trainer — Frontend Logic
   ============================================================ */
"use strict";

// ---------------------------------------------------------------------------
// Client identity (persisted in localStorage)
// ---------------------------------------------------------------------------
function _getOrCreateClientId() {
  let id = localStorage.getItem("pokerClientId");
  if (!id) { id = crypto.randomUUID(); localStorage.setItem("pokerClientId", id); }
  return id;
}
const CLIENT_ID = _getOrCreateClientId();

// ---------------------------------------------------------------------------
// Fetch wrapper — injects X-Client-Id and Content-Type on every API call
// ---------------------------------------------------------------------------
function apiFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Client-Id": CLIENT_ID,
      ...(options.headers || {}),
    },
  });
}

let socket;

// Seat positions [left%, top%] distributed around the 2:1 oval table.
// Calculated at 75% oval radius (r=38%) from center, clockwise from bottom-left.
const SEAT_POSITIONS = {
  1: [[50, 12]],
  2: [[23, 23], [77, 23]],
  3: [[23, 23], [50, 12], [77, 23]],
  4: [[12, 50], [23, 23], [77, 23], [88, 50]],
  5: [[14, 76], [14, 22], [50, 12], [86, 22], [86, 76]],
};

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
let gameState = null;
let validActions = [];
let raiseMin = 0, raiseMax = 1000;
let _currentHandActions = [];
let _aiRetryPlayerIdx = null;

// ---------------------------------------------------------------------------
// localStorage helpers
// ---------------------------------------------------------------------------
const LS_HISTORY = "pokerHandHistory";
const LS_CHIPS   = "pokerChips";
const LS_CONFIG  = "pokerGameConfig";

function getHandHistory()         { return JSON.parse(localStorage.getItem(LS_HISTORY) || "[]"); }
function getSavedChips()          { return parseInt(localStorage.getItem(LS_CHIPS) || "0", 10) || null; }
function saveChips(chips)         { localStorage.setItem(LS_CHIPS, String(chips)); }
function appendHandHistory(entry) {
  const h = getHandHistory();
  h.push(entry);
  localStorage.setItem(LS_HISTORY, JSON.stringify(h));
}

function getGameConfig() {
  const stored = localStorage.getItem(LS_CONFIG);
  if (stored) { try { return JSON.parse(stored); } catch {} }
  const ac = window.APP_CONFIG || {};
  return {
    game_mode:            ac.game_mode            ?? "cash",
    num_opponents:        ac.num_opponents         ?? 5,
    starting_chips:       ac.starting_chips        ?? 2000,
    small_blind:          ac.small_blind           ?? 10,
    big_blind:            ac.big_blind             ?? 20,
    ante:                 ac.ante                  ?? 0,
    hint_enabled:         ac.hint_enabled          ?? true,
    post_hand_analysis:   ac.post_hand_analysis    ?? true,
    show_opponent_styles: ac.show_opponent_styles  ?? true,
    opponent_styles:      ac.opponent_styles       ?? ["random"],
    run_it_twice:         ac.run_it_twice_enabled  ?? false,
    four_color_deck:      ac.four_color_deck       ?? true,
    max_chips:            ac.max_chips             ?? null,
  };
}
function saveGameConfig(cfg) { localStorage.setItem(LS_CONFIG, JSON.stringify(cfg)); }

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
function showToast(message, type = "error", duration = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ---------------------------------------------------------------------------
// Seat badge helper
// ---------------------------------------------------------------------------
function updateSeatBadge(playerName, text) {
  document.querySelectorAll(".seat").forEach(seat => {
    const nameEl = seat.querySelector(".seat-name");
    if (nameEl && nameEl.textContent.includes(playerName)) {
      const row = seat.querySelector(".seat-action-row");
      if (row) row.innerHTML = `<span class="folded-label">${text}</span>`;
    }
  });
}

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
  ["btn-fold", "btn-check", "btn-call", "btn-raise", "btn-allin"].forEach(id => {
    document.getElementById(id).disabled = !enabled;
  });
  document.getElementById("btn-hint").disabled = !enabled || !getGameConfig().hint_enabled;
  if (!enabled) {
    document.getElementById("call-amount").textContent = "";
  }
}

// ---------------------------------------------------------------------------
// Socket init (called after auth)
// ---------------------------------------------------------------------------
function initSocket() {
  socket = io();

  socket.on("connect", () => socket.emit("join_session", { client_id: CLIENT_ID }));

  socket.on("state_update", ({ state, valid_actions, street_changed }) => {
    _aiRetryPlayerIdx = null;
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

  socket.on("ai_action", ({ player_name, action, amount, reasoning, run_twice }) => {
    _aiRetryPlayerIdx = null;
    if (action === "run_twice_decision") {
      const choice = run_twice ? "✅ 同意发两次" : "❌ 选择发一次";
      addLogRaw(`<span class="log-player">${player_name}</span> <span>${choice}</span>` +
        (reasoning ? ` <small style="color:#94a3b8">${reasoning}</small>` : ""));
    } else {
      addLog(player_name, action, amount);
    }
    updateSeatBadge(player_name, actionCN(action, amount));
  });

  socket.on("hand_result", ({ state, result }) => {
    _aiRetryPlayerIdx = null;
    gameState = state;
    renderTable(state, true);
    setActionButtonsEnabled(false);
    logHandEnd(result, state);
    showHandResult(result, state);
    showNextHandButton();

    const human = state.players.find(p => p.is_human);
    if (human) {
      saveChips(human.chips);
      const won = (result.side_pot_results || []).some(p =>
        (p.winners || []).includes(human.name));
      appendHandHistory({
        handNumber: state.hand_number,
        won,
        chipsAfter: human.chips,
        myActions: [..._currentHandActions],
      });
      _currentHandActions = [];
    }
    refreshStats();
  });

  socket.on("ai_thinking", ({ player_name }) => {
    updateSeatBadge(player_name, "思考中…");
  });

  socket.on("ai_action_failed", ({ player_name, player_idx, retry_type }) => {
    addLogRaw(`<span class="log-player">${player_name}</span> <span style="color:#ff7675">决策失败</span>`);
    if (retry_type !== "rit") {
      _aiRetryPlayerIdx = player_idx;
      renderTable(gameState);
    } else {
      updateSeatBadge(player_name, "决策失败");
    }
  });

  socket.on("hand_analysis", ({ analysis }) => {
    appendAnalysisToModal(analysis);
  });

  socket.on("hand_analysis_failed", () => {
    const ab = document.getElementById("analysis-body");
    if (ab) ab.innerHTML = `<div style="color:#ff7675;font-size:0.9rem;margin-top:8px">AI 分析失败，请重试</div>`;
    const loading = document.getElementById("analysis-loading");
    if (loading) loading.style.display = "none";
    const btn = document.getElementById("btn-analysis");
    if (btn) { btn.textContent = "🔄 重新分析"; btn.disabled = false; }
  });

  socket.on("run_it_twice_prompt", (data) => {
    showRunItTwiceDialog(data);
  });

  socket.on("run_it_twice_ai_result", ({ ai_run_twice, ai_reasoning }) => {
    if (document.getElementById("rit-modal").style.display === "none") return;
    const choiceEl = document.getElementById("rit-ai-choice");
    choiceEl.textContent = ai_run_twice ? "✅ 同意发两次" : "❌ 选择发一次";
    choiceEl.style.color = ai_run_twice ? "#55efc4" : "#fdcb6e";
    document.getElementById("rit-ai-reason").textContent = ai_reasoning || "";
    document.getElementById("rit-ai-retry-btn").style.display = "none";
  });

  socket.on("run_it_twice_ai_failed", () => {
    if (document.getElementById("rit-modal").style.display === "none") return;
    const choiceEl = document.getElementById("rit-ai-choice");
    choiceEl.textContent = "⚠️ 决策失败";
    choiceEl.style.color = "#ff7675";
    document.getElementById("rit-ai-retry-btn").style.display = "block";
  });
}

// ---------------------------------------------------------------------------
// Session management
// ---------------------------------------------------------------------------
async function startSession() {
  hideHint();
  clearLog();
  _currentHandActions = [];
  document.getElementById("btn-next").style.display = "none";
  document.getElementById("result-modal").style.display = "none";
  document.getElementById("rit-modal").style.display = "none";

  const cfg = getGameConfig();
  const savedChips = getSavedChips();
  const res = await apiFetch("/api/session/start", {
    method: "POST",
    body: JSON.stringify({ ...cfg, starting_chips: savedChips ?? cfg.starting_chips }),
  });
  const data = await res.json();
  if (data.error) { showToast(data.error); return; }

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
  _currentHandActions = [];
  document.getElementById("btn-next").style.display = "none";
  closeModal();

  const res = await apiFetch("/api/game/next-hand", { method: "POST" });
  const data = await res.json();
  if (data.error) { showToast(data.error); return; }

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

  let res, data;
  try {
    res = await apiFetch("/api/game/action", {
      method: "POST",
      body: JSON.stringify({ action, amount }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    showToast("操作发送失败，请重试");
    if (gameState && isHumanTurn(gameState)) updateActions(validActions);
    return;
  }
  if (data.error) {
    showToast(data.error);
    if (gameState && isHumanTurn(gameState)) updateActions(validActions);
    return;
  }

  addLog("你", action, amount);
  _currentHandActions.push({ street: gameState?.street, action, amount });

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

  const res = await apiFetch("/api/game/hint", { method: "POST" });
  const hint = await res.json();
  if (hint.error) { content.textContent = hint.error; return; }

  const confidenceColor = hint.confidence === "高" ? "#55efc4"
    : hint.confidence === "中" ? "#fdcb6e" : "#b2bec3";

  const actionLabels = {
    fold: "弃牌", check: "过牌", call: "跟注",
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
  random: "随机",
  tight_aggressive: "紧凶", loose_aggressive: "松凶",
  tight_passive: "紧弱", loose_passive: "松弱", balanced: "均衡",
};

function renderTable(state, revealAll = false) {
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
    let actionBadge = "";
    if (!human.is_folded && state.street !== "showdown") {
      if (humanTurn) {
        actionBadge = `<span class="folded-label">思考中…</span>`;
      } else if (!human.last_action) {
        actionBadge = `<span class="folded-label">等待操作</span>`;
      } else {
        actionBadge = `<span class="seat-action-badge">${actionCN(human.last_action, human.last_action_amount)}</span>`;
      }
    }
    const betInfo = human.current_bet > 0
      ? `<span class="seat-bet"> | 注: ${human.current_bet}</span>` : "";
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
        <div><span class="seat-chips">码: ${human.chips}</span>${betInfo}</div>
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
  return `<div class="card ${cardColor(c)}">
    <span class="card-rank">${c.rank === "T" ? "10" : c.rank}</span>
    <span class="card-suit">${suitUnicode(c.suit)}</span>
    ${overlay}
  </div>`;
}

function suitUnicode(suit) {
  return { s: "♠", h: "♥", d: "♦", c: "♣" }[suit] || suit;
}

function cardColor(c) {
  if (getGameConfig().four_color_deck !== false) return c.color;
  if (c.suit === "d") return "red";
  if (c.suit === "c") return "black";
  return c.color;
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
    let actionBadge = "";
    if (p.idx === _aiRetryPlayerIdx) {
      actionBadge = `<button class="retry-btn-seat" onclick="retryAiAction()">🔄 重试</button>`;
    } else {
      if (p.is_folded || gameState?.street === "showdown") {
        actionBadge = "";
      } else if (isCurrent) {
        actionBadge = `<span class="folded-label">思考中…</span>`;
      } else if (!p.last_action) {
        actionBadge = `<span class="folded-label">等待操作</span>`;
      } else {
        actionBadge = `<span class="seat-action-badge">${actionCN(p.last_action, p.last_action_amount)}</span>`;
      }
    }
    const betInfo = p.current_bet > 0
      ? `<span class="seat-bet"> | 注: ${p.current_bet}</span>` : "";

    const stylePart = getGameConfig().show_opponent_styles
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
          <div><span class="seat-chips">码: ${p.chips}</span>${betInfo}</div>
        </div>
        <div class="card-row">${cardHtmlStr}</div>
        <div class="seat-action-row">${actionLine}</div>
      </div>`;
  });
}

function actionCN(action, amount) {
  const map = {
    fold: "弃牌", check: "过牌", call: "跟注",
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
  setBtn("btn-hint",  getGameConfig().hint_enabled !== false);

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
function showRunItTwiceDialog({ ai_pending, ai_run_twice, ai_reasoning } = {}) {
  const choiceEl = document.getElementById("rit-ai-choice");
  const reasonEl = document.getElementById("rit-ai-reason");
  const retryBtn = document.getElementById("rit-ai-retry-btn");
  const hintContent = document.getElementById("rit-hint-content");

  if (ai_pending) {
    choiceEl.textContent = "AI 决定中…";
    choiceEl.style.color = "#94a3b8";
    if (retryBtn) retryBtn.style.display = "none";
  } else {
    choiceEl.textContent = ai_run_twice ? "✅ 同意发两次" : "❌ 选择发一次";
    choiceEl.style.color = ai_run_twice ? "#55efc4" : "#fdcb6e";
    if (retryBtn) retryBtn.style.display = "none";
  }
  reasonEl.textContent = ai_reasoning || "";
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
    const res = await apiFetch("/api/game/run-it-twice-hint", { method: "POST" });
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

  await apiFetch("/api/game/run-it-twice", {
    method: "POST",
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

  // Clear analysis area; permanent analysis button is in the HTML button row
  document.getElementById("analysis-body").innerHTML = "";
  document.getElementById("analysis-loading").style.display = "none";
  const btnAnalysis = document.getElementById("btn-analysis");
  if (btnAnalysis) {
    if (getGameConfig().post_hand_analysis) {
      btnAnalysis.style.display = "";
      btnAnalysis.disabled = false;
      btnAnalysis.textContent = "🤖 AI 分析";
    } else {
      btnAnalysis.style.display = "none";
    }
  }

  document.getElementById("result-modal").style.display = "flex";
}

async function requestAnalysis() {
  const btn = document.getElementById("btn-analysis");
  const loading = document.getElementById("analysis-loading");
  const ab = document.getElementById("analysis-body");
  if (btn) { btn.disabled = true; btn.textContent = "分析中…"; }
  if (loading) loading.style.display = "block";
  if (ab) ab.innerHTML = "";
  await apiFetch("/api/game/analyze", { method: "POST" });
  // Result arrives via hand_analysis / hand_analysis_failed socket event
}

function _buildRunTwiceResultHtml(result) {
  const r1 = result.run_1_results || [];
  const r2 = result.run_2_results || [];
  const comm1 = result.run_1_community || [];
  const comm2 = result.run_2_community || [];

  const potSummary = (runResults) => {
    const winMap = new Map();
    runResults.forEach(p => {
      const key = [...p.winners].sort().join(",");
      if (!winMap.has(key)) winMap.set(key, { winners: p.winners, total: 0, hand_name: p.hand_name || "" });
      const e = winMap.get(key);
      e.total += p.pot_amount;
      if (p.hand_name && !e.hand_name) e.hand_name = p.hand_name;
    });
    return [...winMap.values()]
      .map(e => `${e.winners.join("、")}${e.hand_name ? "（" + e.hand_name + "）" : ""} 赢 ${e.total}`)
      .join("；");
  };

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
  const totalWinners = pots.length > 0 ? [...new Set(pots.flatMap(p => p.winners))].join("、") : "未知";
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
  const loading = document.getElementById("analysis-loading");
  if (loading) loading.style.display = "none";
  const btn = document.getElementById("btn-analysis");
  if (btn) btn.style.display = "none";
}

function closeModal() {
  document.getElementById("result-modal").style.display = "none";
}

function showNextHandButton() {
  document.getElementById("btn-next").style.display = "inline-block";
}

async function retryAiAction() {
  if (_aiRetryPlayerIdx !== null && gameState) {
    const player = gameState.players.find(p => p.idx === _aiRetryPlayerIdx);
    _aiRetryPlayerIdx = null;
    if (player) updateSeatBadge(player.name, "思考中…");
  }
  await apiFetch("/api/game/retry-ai", { method: "POST" });
}

async function retryRitAi() {
  const retryBtn = document.getElementById("rit-ai-retry-btn");
  const choiceEl = document.getElementById("rit-ai-choice");
  if (retryBtn) retryBtn.style.display = "none";
  if (choiceEl) { choiceEl.textContent = "AI 决定中…"; choiceEl.style.color = "#94a3b8"; }
  await apiFetch("/api/game/retry-rit-ai", { method: "POST" });
}

// ---------------------------------------------------------------------------
// Stats (computed client-side from localStorage)
// ---------------------------------------------------------------------------
function refreshStats() {
  const history = getHandHistory();
  const panel = document.getElementById("stats-panel");
  if (!history.length) { panel.style.display = "none"; return; }

  const n = history.length;
  const won = history.filter(h => h.won).length;
  const vpip = history.filter(h =>
    h.myActions?.some(a => a.street === "preflop" && ["call", "raise", "all_in"].includes(a.action))
  ).length;
  const pfr = history.filter(h =>
    h.myActions?.some(a => a.street === "preflop" && ["raise", "all_in"].includes(a.action))
  ).length;
  const pct = v => `${(v * 100).toFixed(1)}%`;

  document.getElementById("stats-content").innerHTML = `
    <div class="stat-row"><span class="stat-label">胜率</span><span class="stat-value">${pct(won / n)}</span></div>
    <div class="stat-row"><span class="stat-label">已玩手数</span><span class="stat-value">${n}</span></div>
    <div class="stat-row"><span class="stat-label">VPIP</span><span class="stat-value">${pct(vpip / n)}</span></div>
    <div class="stat-row"><span class="stat-label">PFR</span><span class="stat-value">${pct(pfr / n)}</span></div>
  `;
  panel.style.display = "block";
}

function clearGameData() {
  localStorage.removeItem(LS_HISTORY);
  localStorage.removeItem(LS_CHIPS);
  _currentHandActions = [];
  refreshStats();
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
    const chipsEnd = result.chips_end || {};
    [...state.players].reverse().forEach(p => {
      const before = p.chips_before_hand ?? p.chips;
      const afterHand = chipsEnd[p.name] ?? p.chips;
      const net = afterHand - before;
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

// ---------------------------------------------------------------------------
// Config modal
// ---------------------------------------------------------------------------
const _STYLE_OPTIONS = ["random", "tight_aggressive", "loose_aggressive", "tight_passive", "loose_passive", "balanced"];

function openConfigModal() {
  const cfg = getGameConfig();
  const modeEl = document.querySelector(`input[name="cfg-mode"][value="${cfg.game_mode}"]`);
  if (modeEl) modeEl.checked = true;
  document.getElementById("cfg-num-opponents").value      = cfg.num_opponents;
  document.getElementById("cfg-starting-chips").value    = cfg.starting_chips;
  document.getElementById("cfg-small-blind").value       = cfg.small_blind;
  document.getElementById("cfg-big-blind").value         = cfg.big_blind;
  document.getElementById("cfg-ante").value              = cfg.ante;
  document.getElementById("cfg-hint-enabled").checked    = cfg.hint_enabled;
  document.getElementById("cfg-analysis-enabled").checked = cfg.post_hand_analysis;
  document.getElementById("cfg-show-styles").checked     = cfg.show_opponent_styles;
  document.getElementById("cfg-run-it-twice").checked    = cfg.run_it_twice;
  document.getElementById("cfg-four-color").checked      = cfg.four_color_deck;
  document.getElementById("cfg-max-chips").value         = cfg.max_chips ?? "";
  const playerChips = gameState?.players?.find(p => p.is_human)?.chips
    ?? getSavedChips() ?? cfg.starting_chips;
  const aiPlayers = gameState?.players?.filter(p => !p.is_human) ?? [];
  const opponentChips = aiPlayers.length ? aiPlayers.map(p => p.chips) : null;
  const opponentNames = aiPlayers.length ? aiPlayers.map(p => p.name) : null;
  _renderPlayersList(cfg.num_opponents, cfg.opponent_styles, playerChips, opponentChips, opponentNames);
  document.getElementById("config-modal").style.display = "flex";
}

function closeConfigModal() {
  document.getElementById("config-modal").style.display = "none";
}

function onNumOpponentsChange(val) {
  const n = Math.min(5, Math.max(2, parseInt(val) || 2));
  const styles = Array.from({ length: n }, (_, i) =>
    document.getElementById(`cfg-style-${i}`)?.value || "random"
  );
  const chips = Array.from({ length: n }, (_, i) =>
    parseInt(document.getElementById(`cfg-chips-${i}`)?.value) || null
  );
  const playerChips = parseInt(document.getElementById("cfg-chips-player")?.value)
    || gameState?.players?.find(p => p.is_human)?.chips
    || getSavedChips() || getGameConfig().starting_chips;
  const aiPlayers = gameState?.players?.filter(p => !p.is_human) ?? [];
  const names = aiPlayers.length ? aiPlayers.map(p => p.name) : null;
  _renderPlayersList(n, styles, playerChips, chips, names);
}

const _AI_NAMES = ["Alice", "Bob", "Charlie", "Diana", "Eve"];

function _renderPlayersList(n, styles, playerChips, opponentChips, opponentNames) {
  const container = document.getElementById("cfg-styles-list");
  container.innerHTML = "";

  const pr = document.createElement("div");
  pr.className = "config-style-row";
  pr.innerHTML = `<span>你 (玩家)</span>
    <input type="number" id="cfg-chips-player" class="config-input config-chips-input"
           min="1" step="100" value="${playerChips}" />`;
  container.appendChild(pr);

  for (let i = 0; i < n; i++) {
    const style = (styles || [])[i] || "random";
    const chips = (opponentChips || [])[i] ?? playerChips;
    const name = (opponentNames || [])[i] || _AI_NAMES[i] || `对手 ${i + 1}`;
    const row = document.createElement("div");
    row.className = "config-style-row";
    row.innerHTML = `<span>${name}</span>
      <select id="cfg-style-${i}">
        ${_STYLE_OPTIONS.map(s =>
          `<option value="${s}"${s === style ? " selected" : ""}>${STYLE_CN[s] || s}</option>`
        ).join("")}
      </select>
      <input type="number" id="cfg-chips-${i}" class="config-input config-chips-input"
             min="1" step="100" value="${chips}" />`;
    container.appendChild(row);
  }
}

function setAllStyles(style) {
  const n = parseInt(document.getElementById("cfg-num-opponents").value) || 2;
  for (let i = 0; i < n; i++) {
    const el = document.getElementById(`cfg-style-${i}`);
    if (el) el.value = style;
  }
}

async function saveConfig() {
  const cfg = getGameConfig();
  const n = Math.min(5, Math.max(2, parseInt(document.getElementById("cfg-num-opponents").value) || 2));
  const styles = Array.from({ length: n }, (_, i) =>
    document.getElementById(`cfg-style-${i}`)?.value || "random"
  );
  const playerChips = Math.max(1,
    parseInt(document.getElementById("cfg-chips-player").value) || cfg.starting_chips);
  const opponentChips = Array.from({ length: n }, (_, i) =>
    Math.max(1, parseInt(document.getElementById(`cfg-chips-${i}`)?.value) || cfg.starting_chips));
  const maxChipsRaw = parseInt(document.getElementById("cfg-max-chips").value);
  const maxChips = maxChipsRaw > 0 ? maxChipsRaw : null;

  saveGameConfig({
    game_mode:            document.querySelector('input[name="cfg-mode"]:checked')?.value || "cash",
    num_opponents:        n,
    starting_chips:       Math.max(100,  parseInt(document.getElementById("cfg-starting-chips").value) || 2000),
    small_blind:          Math.max(1,    parseInt(document.getElementById("cfg-small-blind").value)    || 10),
    big_blind:            Math.max(1,    parseInt(document.getElementById("cfg-big-blind").value)      || 20),
    ante:                 Math.max(0,    parseInt(document.getElementById("cfg-ante").value)           || 0),
    hint_enabled:         document.getElementById("cfg-hint-enabled").checked,
    post_hand_analysis:   document.getElementById("cfg-analysis-enabled").checked,
    show_opponent_styles: document.getElementById("cfg-show-styles").checked,
    opponent_styles:      styles,
    run_it_twice:         document.getElementById("cfg-run-it-twice").checked,
    four_color_deck:      document.getElementById("cfg-four-color").checked,
    max_chips:            maxChips,
  });

  if (gameState) {
    const res = await apiFetch("/api/session/chips", {
      method: "POST",
      body: JSON.stringify({ player_chips: playerChips, opponent_chips: opponentChips }),
    });
    const data = await res.json();
    if (data.state) { gameState = data.state; renderTable(gameState); }
    saveChips(playerChips);
  } else {
    saveChips(playerChips);
  }

  closeConfigModal();
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function checkAuth() {
  const res = await fetch("/api/auth/status");
  const data = await res.json();
  if (data.authenticated) {
    document.getElementById("auth-overlay").style.display = "none";
    refreshStats();
    initSocket();
  } else {
    document.getElementById("auth-input").focus();
  }
}

async function submitAuth() {
  const pwd = document.getElementById("auth-input").value;
  const errEl = document.getElementById("auth-error");
  errEl.style.display = "none";
  const res = await fetch("/api/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pwd }),
  });
  if (res.ok) {
    document.getElementById("auth-overlay").style.display = "none";
    refreshStats();
    initSocket();
  } else {
    errEl.style.display = "block";
    document.getElementById("auth-input").value = "";
    document.getElementById("auth-input").focus();
  }
}

checkAuth();
