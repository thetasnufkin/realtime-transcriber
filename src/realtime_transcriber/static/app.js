// 字幕ビューア: /captions から暫定 / 確定テキストを受け取って表示する
"use strict";

const scroller = document.getElementById("scroller");
const finals = document.getElementById("finals");
const partials = document.getElementById("partials");
const statusEl = document.getElementById("status");
const emptyEl = document.getElementById("empty");

const RECONNECT_MS = 1500;
const partialBySource = new Map(); // 入力ごとの暫定行

function nearBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
}

// 一番下を見ているときだけ追従する（読み返し中はスクロールを奪わない）
function withAutoScroll(update) {
  const follow = nearBottom();
  update();
  emptyEl.hidden = finals.children.length + partials.children.length > 0;
  if (follow) scroller.scrollTop = scroller.scrollHeight;
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
}

function showStatus(s) {
  if (s.model_state === "loading") setStatus("loading", "モデルを読み込み中…");
  else if (s.model_state === "error") setStatus("error", `モデルの読み込みに失敗しました: ${s.error ?? ""}`);
  else if (s.ingest_clients > 0) setStatus("receiving", "音声を受信中");
  else setStatus("idle", "音声の入力を待っています");
}

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
    setTimeout(connect, RECONNECT_MS);
  };
}

connect();
