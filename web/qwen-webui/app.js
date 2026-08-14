"use strict";

const messages = [];
let pending = [];
let configuration = null;
let busy = false;

const elements = {
  messages: document.querySelector("#messages"),
  prompt: document.querySelector("#prompt"),
  send: document.querySelector("#send"),
  clear: document.querySelector("#clear"),
  fileInput: document.querySelector("#file-input"),
  attachmentList: document.querySelector("#attachment-list"),
  error: document.querySelector("#error"),
  serverLabel: document.querySelector("#server-label"),
};

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function showError(message) {
  elements.error.textContent = message;
  elements.error.hidden = false;
}

function clearError() {
  elements.error.hidden = true;
  elements.error.textContent = "";
}

function scrollToBottom() {
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function appendMessage(role, text, attachments = []) {
  const article = document.createElement("article");
  article.className = `message ${role}`;

  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "You" : "Qwen";
  article.appendChild(label);

  if (attachments.length) {
    const files = document.createElement("div");
    files.className = "message-files";
    for (const attachment of attachments) {
      const chip = document.createElement("span");
      chip.textContent = `${attachment.kind === "image" ? "画像" : "文書"} · ${attachment.name}`;
      files.appendChild(chip);
    }
    article.appendChild(files);
  }

  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = text;
  article.appendChild(content);
  elements.messages.appendChild(article);
  scrollToBottom();
}

function renderPending() {
  elements.attachmentList.replaceChildren();
  elements.attachmentList.hidden = pending.length === 0;
  pending.forEach((attachment, index) => {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    const text = document.createElement("span");
    text.textContent = `${attachment.kind === "image" ? "画像" : "文書"} · ${attachment.name} · ${formatBytes(attachment.size)}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `${attachment.name}を外す`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      pending.splice(index, 1);
      renderPending();
    });
    chip.append(text, remove);
    elements.attachmentList.appendChild(chip);
  });
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(reader.result));
    reader.addEventListener("error", () => reject(reader.error));
    reader.readAsDataURL(file);
  });
}

function imageMimeType(file) {
  const supported = ["image/jpeg", "image/png", "image/webp", "image/gif"];
  if (supported.includes(file.type)) return file.type;
  const extension = file.name.toLowerCase().split(".").pop();
  return {
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    png: "image/png",
    webp: "image/webp",
    gif: "image/gif",
  }[extension] || null;
}

async function readUtf8(file) {
  const buffer = await file.arrayBuffer();
  return new TextDecoder("utf-8", {fatal: true}).decode(buffer);
}

async function queueFiles(files) {
  clearError();
  for (const file of files) {
    try {
      if (!configuration) throw new Error("サーバー情報の取得を待っています。");
      const imageMime = imageMimeType(file);
      if (imageMime) {
        if (pending.some((item) => item.kind === "image")) {
          throw new Error("1回の発言に添付できる画像は1枚です。");
        }
        if (file.size > configuration.max_image_bytes) {
          throw new Error(`画像が大きすぎます（上限 ${formatBytes(configuration.max_image_bytes)}）。`);
        }
        const rawDataUrl = await readAsDataURL(file);
        const data = rawDataUrl.replace(/^data:[^;]*;/, `data:${imageMime};`);
        pending.push({kind: "image", name: file.name, size: file.size, data});
      } else {
        const textBytes = pending
          .filter((item) => item.kind === "text")
          .reduce((total, item) => total + item.size, 0);
        if (textBytes + file.size > configuration.max_text_attachment_bytes) {
          throw new Error(`文書添付の合計が上限 ${formatBytes(configuration.max_text_attachment_bytes)} を超えます。`);
        }
        const data = await readUtf8(file);
        if (data.includes("\0")) {
          throw new Error(`${file.name} はテキストファイルとして読み込めません。`);
        }
        pending.push({kind: "text", name: file.name, size: file.size, data});
      }
    } catch (error) {
      showError(`${file.name}: ${error.message}`);
    }
  }
  elements.fileInput.value = "";
  renderPending();
}

function stripImages(history) {
  for (const message of history) {
    if (!Array.isArray(message.content)) continue;
    message.content = message.content.filter((part) => part.type !== "image_url");
  }
}

function buildUserContent(prompt, attachments) {
  if (!attachments.length) return prompt;
  const content = [];
  for (const attachment of attachments) {
    if (attachment.kind === "image") {
      content.push({type: "image_url", image_url: {url: attachment.data}});
    } else {
      content.push({
        type: "text",
        text: `--- 添付文書: ${attachment.name} ---\n${attachment.data}\n--- 添付文書ここまで ---`,
      });
    }
  }
  content.push({type: "text", text: prompt});
  return content;
}

async function sendMessage() {
  const prompt = elements.prompt.value.trim();
  if (!prompt || busy) return;
  clearError();
  busy = true;
  elements.send.disabled = true;
  elements.prompt.disabled = true;

  const attachments = pending.slice();
  const hasNewImage = attachments.some((item) => item.kind === "image");
  const requestHistory = JSON.parse(JSON.stringify(messages));
  if (hasNewImage) stripImages(requestHistory);
  const userMessage = {role: "user", content: buildUserContent(prompt, attachments)};

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({messages: [...requestHistory, userMessage]}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (hasNewImage) stripImages(messages);
    messages.push(userMessage, {role: "assistant", content: result.answer});
    appendMessage("user", prompt, attachments);
    appendMessage("assistant", result.answer);
    pending = [];
    renderPending();
    elements.prompt.value = "";
    elements.prompt.style.height = "auto";
  } catch (error) {
    showError(`応答を取得できませんでした: ${error.message}`);
  } finally {
    busy = false;
    elements.send.disabled = false;
    elements.prompt.disabled = false;
    elements.prompt.focus();
  }
}

async function initialize() {
  try {
    const response = await fetch("/api/status");
    configuration = await response.json();
    if (!response.ok) throw new Error(configuration.error || `HTTP ${response.status}`);
    elements.serverLabel.textContent = `${configuration.model} · Job ${configuration.pbs_job_id}`;
    elements.send.disabled = false;
  } catch (error) {
    showError(`サーバー情報を取得できませんでした: ${error.message}`);
    elements.serverLabel.textContent = "接続エラー";
    elements.send.disabled = true;
  }
}

elements.send.addEventListener("click", sendMessage);
elements.fileInput.addEventListener("change", (event) => queueFiles(event.target.files));
elements.clear.addEventListener("click", () => {
  messages.splice(0);
  pending = [];
  renderPending();
  document.querySelectorAll(".message").forEach((node) => node.remove());
  clearError();
});
elements.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    sendMessage();
  }
});
elements.prompt.addEventListener("input", () => {
  elements.prompt.style.height = "auto";
  elements.prompt.style.height = `${Math.min(elements.prompt.scrollHeight, 180)}px`;
});

initialize();
