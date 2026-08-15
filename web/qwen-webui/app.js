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
  artifactButton: document.querySelector("#artifact-button"),
  artifactDialog: document.querySelector("#artifact-dialog"),
  artifactClose: document.querySelector("#artifact-close"),
  artifactList: document.querySelector("#artifact-list"),
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

async function copyText(text, button) {
  const originalLabel = button.textContent;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const temporary = document.createElement("textarea");
      temporary.value = text;
      temporary.setAttribute("readonly", "");
      temporary.style.position = "fixed";
      temporary.style.opacity = "0";
      document.body.appendChild(temporary);
      temporary.select();
      const copied = document.execCommand("copy");
      temporary.remove();
      if (!copied) throw new Error("copy command was rejected");
    }
    button.textContent = "コピー済み";
    button.classList.add("copied");
  } catch (error) {
    showError(`応答をコピーできませんでした: ${error.message}`);
    button.textContent = "失敗";
  }
  window.setTimeout(() => {
    button.textContent = originalLabel;
    button.classList.remove("copied");
  }, 1600);
}

function appendMessage(role, text, attachments = []) {
  const article = document.createElement("article");
  article.className = `message ${role}`;

  const header = document.createElement("div");
  header.className = "message-header";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "You" : "Qwen";
  header.appendChild(label);
  if (role === "assistant") {
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-button";
    copy.textContent = "コピー";
    copy.setAttribute("aria-label", "Qwenの応答をコピー");
    copy.addEventListener("click", () => copyText(text, copy));
    header.appendChild(copy);
  }
  article.appendChild(header);

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
  return article;
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

function addPendingAttachment(attachment) {
  if (!configuration) throw new Error("サーバー情報の取得を待っています。");
  if (attachment.kind === "image") {
    if (pending.some((item) => item.kind === "image")) {
      throw new Error("1回の発言に添付できる画像は1枚です。");
    }
    if (attachment.size > configuration.max_image_bytes) {
      throw new Error(`画像が大きすぎます（上限 ${formatBytes(configuration.max_image_bytes)}）。`);
    }
  } else {
    const textBytes = pending
      .filter((item) => item.kind === "text")
      .reduce((total, item) => total + item.size, 0);
    if (textBytes + attachment.size > configuration.max_text_attachment_bytes) {
      throw new Error(`文書添付の合計が上限 ${formatBytes(configuration.max_text_attachment_bytes)} を超えます。`);
    }
  }
  pending.push(attachment);
  renderPending();
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
        addPendingAttachment({kind: "image", name: file.name, size: file.size, data});
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
        addPendingAttachment({kind: "text", name: file.name, size: file.size, data});
      }
    } catch (error) {
      showError(`${file.name}: ${error.message}`);
    }
  }
  elements.fileInput.value = "";
  renderPending();
}

async function loadServerAttachments(documentId, paths) {
  clearError();
  const previousPending = pending.slice();
  try {
    for (const path of paths) {
      const query = new URLSearchParams({document: documentId, path});
      const response = await fetch(`/api/attachment?${query}`);
      const attachment = await response.json();
      if (!response.ok) throw new Error(attachment.error || `HTTP ${response.status}`);
      addPendingAttachment(attachment);
    }
    elements.artifactDialog.close();
    elements.prompt.focus();
  } catch (error) {
    pending = previousPending;
    renderPending();
    elements.artifactDialog.close();
    showError(`処理済み資料を添付できませんでした: ${error.message}`);
  }
}

async function summarizeArtifact(artifact) {
  if (busy) return;
  clearError();
  elements.artifactDialog.close();
  busy = true;
  elements.send.disabled = true;
  elements.prompt.disabled = true;
  elements.clear.disabled = true;
  elements.artifactButton.disabled = true;
  elements.fileInput.disabled = true;
  const working = appendMessage(
    "assistant",
    `「${artifact.title}」をページ単位で解析し、階層的要約を作成しています。\n` +
      `対象: ${artifact.pages.length}ページ（各ページの代表画像を最大1枚解析）\n` +
      "数分かかることがあります。この画面とSSH接続を開いたままお待ちください。",
  );

  try {
    const response = await fetch("/api/summarize", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document: artifact.id, include_images: true}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);

    messages.splice(0);
    pending = [];
    renderPending();
    document.querySelectorAll(".message").forEach((node) => node.remove());
    const contextMessage = {
      role: "user",
      content:
        `以下は論文「${result.title}」をページ別に解析して圧縮した統合ノートです。\n\n` +
        `${result.context_notes}\n\n` +
        "以降の質問では、この統合ノートと階層的要約を根拠として回答してください。",
    };
    messages.push(contextMessage, {role: "assistant", content: result.answer});
    appendMessage(
      "user",
      `「${result.title}」の階層的要約を作成してください。`,
      [{kind: "text", name: `統合ノート · ${result.page_count}ページ`}],
    );
    appendMessage(
      "assistant",
      `${result.answer}\n\n${result.cached ? "（保存済み要約を再利用）" : "（ページ別解析から新規作成）"}`,
    );
  } catch (error) {
    working.remove();
    showError(`階層的要約を作成できませんでした: ${error.message}`);
  } finally {
    busy = false;
    elements.send.disabled = false;
    elements.prompt.disabled = false;
    elements.clear.disabled = false;
    elements.artifactButton.disabled = false;
    elements.fileInput.disabled = false;
    elements.prompt.focus();
  }
}

function loadServerAttachment(documentId, path) {
  return loadServerAttachments(documentId, [path]);
}

function renderArtifacts(artifacts) {
  elements.artifactList.replaceChildren();
  if (!artifacts.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "完成済みの処理結果はありません。";
    elements.artifactList.appendChild(empty);
    return;
  }

  for (const artifact of artifacts) {
    const card = document.createElement("section");
    card.className = "artifact-card";
    const heading = document.createElement("h3");
    heading.textContent = artifact.title;
    const meta = document.createElement("p");
    meta.className = "artifact-meta";
    const pages = Number.isInteger(artifact.page_count) ? `${artifact.page_count}ページ` : "ページ数不明";
    meta.textContent = `${pages} · 画像 ${artifact.images.length}枚 · ${artifact.id.slice(0, 12)}`;
    card.append(heading, meta);

    const actions = document.createElement("div");
    actions.className = "artifact-actions";
    const summaryButton = document.createElement("button");
    summaryButton.type = "button";
    summaryButton.className = "library-button summary-action";
    summaryButton.textContent = "論文全体を階層的に要約";
    summaryButton.addEventListener("click", () => summarizeArtifact(artifact));
    actions.appendChild(summaryButton);
    const summaryNote = document.createElement("p");
    summaryNote.className = "summary-note";
    summaryNote.textContent = "ページ別ノート → 中間統合 → 全体要約。各ページの代表画像を最大1枚解析します。";
    actions.appendChild(summaryNote);
    for (const markdown of artifact.markdown) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "library-button";
      const tooLarge = markdown.size > configuration.max_text_attachment_bytes;
      button.disabled = tooLarge;
      button.textContent = tooLarge
        ? `全文Markdownは上限超過 (${formatBytes(markdown.size)})`
        : `全文Markdownを添付 (${formatBytes(markdown.size)})`;
      button.addEventListener("click", () => loadServerAttachment(artifact.id, markdown.path));
      actions.appendChild(button);
    }

    if (artifact.images.length) {
      const imageRow = document.createElement("div");
      imageRow.className = "image-picker";
      const select = document.createElement("select");
      select.setAttribute("aria-label", `${artifact.title}の画像`);
      for (const image of artifact.images) {
        const option = document.createElement("option");
        option.value = image.path;
        option.textContent = `${image.name} (${formatBytes(image.size)})`;
        select.appendChild(option);
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "library-button secondary";
      button.textContent = "選択画像を添付";
      button.addEventListener("click", () => loadServerAttachment(artifact.id, select.value));
      imageRow.append(select, button);
      actions.appendChild(imageRow);
    }

    if (artifact.pages.length) {
      const pagePicker = document.createElement("div");
      pagePicker.className = "page-picker";
      const pageHeading = document.createElement("div");
      pageHeading.className = "picker-heading";
      pageHeading.textContent = "ページ単位で添付";
      const pageSelect = document.createElement("select");
      pageSelect.setAttribute("aria-label", `${artifact.title}のページ`);
      artifact.pages.forEach((page, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        const markdownSize = page.markdown[0] ? formatBytes(page.markdown[0].size) : "MDなし";
        option.textContent = `Page ${page.number} · ${markdownSize} · 画像 ${page.images.length}枚`;
        pageSelect.appendChild(option);
      });

      const pageImageSelect = document.createElement("select");
      pageImageSelect.setAttribute("aria-label", `${artifact.title}のページ内画像`);
      const markdownButton = document.createElement("button");
      markdownButton.type = "button";
      markdownButton.className = "library-button secondary";
      markdownButton.textContent = "ページMarkdownを添付";
      const combinedButton = document.createElement("button");
      combinedButton.type = "button";
      combinedButton.className = "library-button";
      combinedButton.textContent = "ページMD＋選択画像を添付";

      function selectedPage() {
        return artifact.pages[Number(pageSelect.value)];
      }

      function updatePageControls() {
        const page = selectedPage();
        const markdown = page.markdown[0];
        const markdownTooLarge = markdown && markdown.size > configuration.max_text_attachment_bytes;
        markdownButton.disabled = !markdown || markdownTooLarge;
        markdownButton.textContent = markdownTooLarge
          ? `ページMarkdownは上限超過 (${formatBytes(markdown.size)})`
          : "ページMarkdownを添付";
        pageImageSelect.replaceChildren();
        for (const image of page.images) {
          const option = document.createElement("option");
          option.value = image.path;
          option.textContent = `${image.name} (${formatBytes(image.size)})`;
          pageImageSelect.appendChild(option);
        }
        pageImageSelect.disabled = page.images.length === 0;
        combinedButton.disabled = !markdown || markdownTooLarge || page.images.length === 0;
      }

      pageSelect.addEventListener("change", updatePageControls);
      markdownButton.addEventListener("click", () => {
        const markdown = selectedPage().markdown[0];
        if (markdown) loadServerAttachment(artifact.id, markdown.path);
      });
      combinedButton.addEventListener("click", () => {
        const markdown = selectedPage().markdown[0];
        if (markdown && pageImageSelect.value) {
          loadServerAttachments(artifact.id, [markdown.path, pageImageSelect.value]);
        }
      });
      updatePageControls();
      pagePicker.append(
        pageHeading,
        pageSelect,
        pageImageSelect,
        markdownButton,
        combinedButton,
      );
      actions.appendChild(pagePicker);
    }
    card.appendChild(actions);
    elements.artifactList.appendChild(card);
  }
}

async function openArtifactLibrary() {
  clearError();
  elements.artifactList.innerHTML = '<p class="muted">読み込み中…</p>';
  elements.artifactDialog.showModal();
  try {
    const response = await fetch("/api/artifacts");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    renderArtifacts(result.artifacts);
  } catch (error) {
    elements.artifactDialog.close();
    showError(`処理済み資料を取得できませんでした: ${error.message}`);
  }
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
elements.artifactButton.addEventListener("click", openArtifactLibrary);
elements.artifactClose.addEventListener("click", () => elements.artifactDialog.close());
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
