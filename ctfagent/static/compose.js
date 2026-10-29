/* The home page: one box, like every other chat.
   Enter sends (Shift+Enter breaks the line), files can be picked or dropped
   anywhere on the page, and the first message opens a run. Files are staged in
   the browser (a DataTransfer keeps the real File objects) so the operator can
   drop several times without the picker resetting. */

(function () {
  "use strict";

  var form = document.getElementById("compose-form");
  if (!form) return;

  var input = document.getElementById("files");
  var chosen = document.getElementById("chosen");
  var state = document.getElementById("compose-state");
  var submit = document.getElementById("compose-submit");
  var textarea = form.querySelector("textarea[name=description]");
  var dropZone = document.getElementById("drop-veil");
  var scroll = document.getElementById("home-scroll");

  var staged = [];
  var transfer = typeof DataTransfer === "function" ? new DataTransfer() : null;

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function same(a, b) {
    return a.name === b.name && a.size === b.size && a.lastModified === b.lastModified;
  }

  function sync() {
    if (transfer) {
      transfer.items.clear();
      staged.forEach(function (file) { transfer.items.add(file); });
      input.files = transfer.files;
    }
    if (!chosen) return;
    chosen.textContent = "";
    staged.forEach(function (file, index) {
      var chip = document.createElement("span");
      chip.className = "chip-file";
      chip.title = file.name + " · " + humanSize(file.size);
      var name = document.createElement("span");
      name.className = "n";
      name.textContent = file.name;
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "x";
      remove.setAttribute("aria-label", "移除 " + file.name);
      remove.textContent = "×";
      remove.addEventListener("click", function () {
        staged.splice(index, 1);
        sync();
      });
      chip.appendChild(name);
      chip.appendChild(remove);
      chosen.appendChild(chip);
    });
  }

  function stage(files) {
    Array.prototype.forEach.call(files || [], function (file) {
      if (!staged.some(function (other) { return same(other, file); })) staged.push(file);
    });
    sync();
  }

  // ---- picking -------------------------------------------------------------

  if (input) {
    input.addEventListener("change", function () { stage(input.files); });
  }

  // ---- dropping anywhere on the page ---------------------------------------

  var depth = 0;

  function hasFiles(ev) {
    var types = ev.dataTransfer && ev.dataTransfer.types;
    return types && Array.prototype.indexOf.call(types, "Files") !== -1;
  }

  document.addEventListener("dragenter", function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    depth += 1;
    if (dropZone) dropZone.hidden = false;
  });

  document.addEventListener("dragover", function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
  });

  document.addEventListener("dragleave", function (ev) {
    if (!hasFiles(ev)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) {
      if (dropZone) dropZone.hidden = true;
    }
  });

  document.addEventListener("drop", function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    depth = 0;
    if (dropZone) dropZone.hidden = true;
    stage(ev.dataTransfer.files);
    if (textarea) textarea.focus();
  });

  // ---- example prompts -----------------------------------------------------

  document.querySelectorAll(".example[data-fill]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!textarea) return;
      textarea.value = button.getAttribute("data-fill");
      textarea.focus();
      if (scroll) scroll.scrollTop = scroll.scrollHeight;
    });
  });

  // ---- sending -------------------------------------------------------------

  function send() {
    var text = textarea ? textarea.value.trim() : "";
    if (!text) {
      if (state) state.textContent = "先写点什么再发送";
      if (textarea) textarea.focus();
      return;
    }
    var data = new FormData();
    data.append("description", text);
    // The folder picker is a <select> in this same form, so the plain POST would
    // send it — but this path builds the body by hand, and a field missing from
    // this list is silently dropped. A run filed into a competition only lands
    // there because it is named here.
    ["title", "category", "remote", "flag_format", "objective", "folder_id"].forEach(function (name) {
      var field = form.querySelector("[name=" + name + "]");
      if (field && field.value.trim()) data.append(name, field.value.trim());
    });
    staged.forEach(function (file) { data.append("files", file, file.name); });

    submit.disabled = true;
    if (state) state.textContent = "正在开工作台…";
    fetch(form.getAttribute("action"), { method: "POST", body: data, redirect: "follow" })
      .then(function (response) {
        // The endpoint answers 303 to /runs/<id>; fetch follows it, and the
        // response is the workspace HTML. Navigate there for real.
        if (!response.ok) throw new Error("HTTP " + response.status);
        if (response.redirected && response.url) {
          window.location.href = response.url;
        } else {
          window.location.href = "/";
        }
      })
      .catch(function (err) {
        submit.disabled = false;
        if (state) state.textContent = "失败：" + err.message;
      });
  }

  form.addEventListener("submit", function (ev) {
    if (!window.fetch) return;   // let the plain POST work
    ev.preventDefault();
    send();
  });

  if (textarea) {
    textarea.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        send();
      }
    });
    textarea.addEventListener("input", function () {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 220) + "px";
    });
  }

  sync();
})();
