// 字幕ビューア: /captions から暫定 / 確定テキストを受け取って表示する
"use strict";

const scroller = document.getElementById("scroller");
const finals = document.getElementById("finals");
const partials = document.getElementById("partials");
const statusEl = document.getElementById("status");
const emptyEl = document.getElementById("empty");
const autoScrollEl = document.getElementById("autoscroll");
const sizeEl = document.getElementById("size");
const smallerEl = document.getElementById("smaller");
const largerEl = document.getElementById("larger");
const jumpEl = document.getElementById("jump");
const pauseEl = document.getElementById("pause");
const settingsEl = document.getElementById("settings");
const formEl = document.getElementById("settings-form");
const errorEl = document.getElementById("settings-error");

const RECONNECT_MS = 1500;
const FONT_SIZES = [16, 18, 20, 22, 26, 30, 36, 44, 52, 64]; // px
const DEFAULT_FONT_SIZE = 22;
const partialBySource = new Map(); // 入力ごとの暫定行

// 表示の好みはこのブラウザだけに保存する（保存できない環境でも動くようにする）
const prefs = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`viewer.${key}`);
      return v === null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`viewer.${key}`, JSON.stringify(value));
    } catch {
      // 保存できなくても表示には影響しない
    }
  },
};

// --- 文字サイズ ---
let fontSize = FONT_SIZES.includes(prefs.get("fontSize")) ? prefs.get("fontSize") : DEFAULT_FONT_SIZE;

function applyFontSize() {
  document.documentElement.style.setProperty("--caption-size", `${fontSize}px`);
  sizeEl.textContent = fontSize;
  smallerEl.disabled = fontSize === FONT_SIZES[0];
  largerEl.disabled = fontSize === FONT_SIZES.at(-1);
  prefs.set("fontSize", fontSize);
}

function stepFontSize(delta) {
  const follow = autoScrollEl.checked && nearBottom();
  const i = FONT_SIZES.indexOf(fontSize) + delta;
  fontSize = FONT_SIZES[Math.min(Math.max(i, 0), FONT_SIZES.length - 1)];
  applyFontSize();
  if (follow) scrollToBottom();
}

smallerEl.addEventListener("click", () => stepFontSize(-1));
largerEl.addEventListener("click", () => stepFontSize(+1));
applyFontSize();

// --- 自動スクロール ---
autoScrollEl.checked = prefs.get("autoScroll", true);
autoScrollEl.addEventListener("change", () => {
  prefs.set("autoScroll", autoScrollEl.checked);
  if (autoScrollEl.checked) scrollToBottom();
});

function nearBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
}

function scrollToBottom() {
  scroller.scrollTop = scroller.scrollHeight;
  updateJump();
}

function updateJump() {
  jumpEl.hidden = nearBottom();
}

scroller.addEventListener("scroll", updateJump);
jumpEl.addEventListener("click", scrollToBottom);

// 自動スクロールが ON で、一番下を見ているときだけ追従する（読み返し中はスクロールを奪わない）
function withAutoScroll(update) {
  const follow = autoScrollEl.checked && nearBottom();
  update();
  emptyEl.hidden = finals.children.length + partials.children.length > 0;
  if (follow) scrollToBottom();
  else updateJump();
}

function line(text, at) {
  const li = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = at ?? "";
  const span = document.createElement("span");
  span.textContent = text;
  li.append(time, span);
  return li;
}

function clearPartial(source) {
  partialBySource.get(source)?.remove();
  partialBySource.delete(source);
}

function setStatus(state, text) {
  statusEl.dataset.state = state;
  statusEl.textContent = text;
  statusEl.title = text;
}

let lastStatus = {};

function showStatus(s) {
  lastStatus = s;
  if (s.model_state === "loading") setStatus("loading", "モデルを読み込み中…");
  else if (s.model_state === "error") setStatus("error", `モデルの読み込みに失敗しました: ${s.error ?? ""}`);
  else if (s.paused) setStatus("paused", "一時停止中（届いた音声は捨てています）");
  else if (s.model_state === "switching") setStatus("loading", "モデルを切り替え中…（終わるまで今のモデルで認識）");
  else if (s.model_error) setStatus("error", `モデルを切り替えられませんでした（今のモデルで認識を続けています）: ${s.model_error}`);
  else if (s.ingest_clients > 0) setStatus("receiving", "音声を受信中");
  else setStatus("idle", "音声の入力を待っています");

  pauseEl.disabled = false;
  pauseEl.setAttribute("aria-pressed", String(Boolean(s.paused)));
  pauseEl.textContent = s.paused ? "▶ 再開" : "⏸ 一時停止";
}

// --- 一時停止 / 再開 ---
pauseEl.addEventListener("click", async () => {
  pauseEl.disabled = true;
  try {
    await fetch(lastStatus.paused ? "/api/resume" : "/api/pause", { method: "POST" });
  } finally {
    pauseEl.disabled = false; // 表示は status で更新される
  }
});

// --- 設定 ---
const CLI_FLAGS = { model: "--model", final_model: "--final-model", speed: "--speed", glossary: "--glossary" };

function openSettings() {
  const c = lastStatus.config ?? {};
  formEl.speed.value = c.speed ?? 1;
  formEl.glossary.value = (c.glossary ?? []).join("\n");
  formEl.model.value = c.model ?? "";
  formEl.final_model.value = c.final_model ?? "";
  for (const el of formEl.querySelectorAll(".override")) {
    const key = el.dataset.key;
    el.hidden = !(lastStatus.cli_overrides ?? []).includes(key);
    el.textContent = `起動時の引数 ${CLI_FLAGS[key]} で指定されています。ここで変えても、次回の起動では引数が優先されます。`;
  }
  errorEl.hidden = true;
  settingsEl.showModal();
}

function readForm() {
  const c = lastStatus.config ?? {};
  const value = {
    speed: Number(formEl.speed.value),
    glossary: formEl.glossary.value.split(/[\n,、]/).map((w) => w.trim()).filter(Boolean),
    model: formEl.model.value.trim(),
    final_model: formEl.final_model.value.trim(),
  };
  // 変わった項目だけ送る（config.toml にも変えた項目だけが書かれる）
  return Object.fromEntries(Object.entries(value).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(c[k])));
}

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const changes = readForm();
  if (Object.keys(changes).length === 0) {
    settingsEl.close();
    return;
  }
  const submit = formEl.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    const r = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error ?? `保存できませんでした (${r.status})`);
    settingsEl.close();
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

for (const b of formEl.querySelectorAll("[data-speed]")) {
  b.addEventListener("click", () => (formEl.speed.value = b.dataset.speed));
}
document.getElementById("open-settings").addEventListener("click", openSettings);
document.getElementById("cancel-settings").addEventListener("click", () => settingsEl.close());

function handle(msg) {
  switch (msg.type) {
    case "history":
      withAutoScroll(() => {
        finals.replaceChildren(...msg.captions.map((c) => line(c.text, c.at)));
        partials.replaceChildren();
        partialBySource.clear();
      });
      break;
    case "partial":
      withAutoScroll(() => {
        let li = partialBySource.get(msg.source);
        if (!li) {
          li = line("", "");
          partialBySource.set(msg.source, li);
          partials.append(li);
        }
        li.lastChild.textContent = msg.text;
      });
      break;
    case "final":
      withAutoScroll(() => {
        clearPartial(msg.source);
        finals.append(line(msg.text, msg.at));
      });
      break;
    case "clear":
      withAutoScroll(() => clearPartial(msg.source));
      break;
    case "status":
      showStatus(msg);
      break;
  }
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/captions`);
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => {
    setStatus("offline", "サーバーに接続できません。再接続しています…");
    pauseEl.disabled = true;
    setTimeout(connect, RECONNECT_MS);
  };
}

connect();
