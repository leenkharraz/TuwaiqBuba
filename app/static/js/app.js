/* Bupa Discovery reimbursement prototype — lightweight client script (no framework). */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var S = {};
  try { S = JSON.parse(($("#ui-strings") || {}).textContent || "{}"); } catch (e) { S = {}; }
  var MAX_BYTES = parseInt(document.body.getAttribute("data-max-bytes") || "5242880", 10);
  var OK_TYPES = ["image/jpeg", "image/png", "application/pdf"];
  var OK_EXT = /\.(jpe?g|png|pdf)$/i;
  var FETCH_HEADERS = { "X-Requested-With": "fetch" };

  function fmt(str, params) {
    return String(str || "").replace(/\{(\w+)\}/g, function (_, k) { return params && k in params ? params[k] : "{" + k + "}"; });
  }

  // ------------------------------------------------------------- toasts
  function toast(message, opts) {
    if (!message) return;
    opts = opts || {};
    var region = $("#toasts");
    if (!region) return;
    var el = document.createElement("div");
    el.className = "toast" + (opts.error ? " err" : "");
    el.setAttribute("role", opts.error ? "alert" : "status");
    var span = document.createElement("span");
    span.textContent = message;
    el.appendChild(span);
    if (opts.link) {
      var a = document.createElement("a");
      a.href = opts.link.href;
      a.textContent = opts.link.label;
      el.appendChild(a);
    }
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, opts.link ? 9000 : 4500);
  }

  function claimId() {
    var ws = $("#workspace");
    return ws ? ws.getAttribute("data-claim") : null;
  }

  function feedbackBox() {
    var boxes = $$("[data-upload-feedback]");
    return boxes[0] || null;
  }

  function showFeedback(lines) {
    var box = feedbackBox();
    if (!box) { lines.forEach(function (l) { toast(l.text, { error: !l.ok }); }); return; }
    box.innerHTML = "";
    lines.forEach(function (l) {
      var p = document.createElement("p");
      if (l.ok) p.className = "ok";
      p.textContent = l.text;
      box.appendChild(p);
    });
  }

  // ------------------------------------------------------------- image preparation
  function loadBitmap(file) {
    if (window.createImageBitmap) {
      return createImageBitmap(file).catch(function () { return loadViaImg(file); });
    }
    return loadViaImg(file);
  }
  function loadViaImg(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("decode")); };
      img.src = url;
    });
  }
  function toJpeg(source, maxEdge, quality, name) {
    var w = source.width, h = source.height;
    var scale = Math.min(1, maxEdge / Math.max(w, h));
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
    return new Promise(function (resolve) {
      canvas.toBlob(function (blob) {
        resolve(blob ? new File([blob], name, { type: "image/jpeg" }) : null);
      }, "image/jpeg", quality);
    });
  }
  // Convert HEIC/WebP photos to JPEG and shrink oversized photos so they fit the prototype limit.
  function prepareFile(file) {
    var isImage = /^image\//.test(file.type) || /\.(heic|heif|webp|jpe?g|png)$/i.test(file.name);
    var supported = OK_TYPES.indexOf(file.type) !== -1 || OK_EXT.test(file.name);
    if (!isImage || (supported && file.size <= MAX_BYTES)) return Promise.resolve(file);
    var base = (file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg";
    return loadBitmap(file).then(function (bmp) {
      return toJpeg(bmp, 2600, 0.88, base).then(function (out) {
        if (out && out.size > MAX_BYTES) return toJpeg(bmp, 2000, 0.8, base);
        return out;
      });
    }).then(function (out) { return out || file; }).catch(function () { return file; });
  }

  // ------------------------------------------------------------- uploads
  function uploadFiles(fileList, opts) {
    opts = opts || {};
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    Promise.all(files.map(prepareFile)).then(function (prepared) {
      var errors = [], valid = [];
      prepared.forEach(function (f) {
        var typeOk = OK_TYPES.indexOf(f.type) !== -1 || OK_EXT.test(f.name);
        if (!typeOk) errors.push({ text: fmt(S.rejected, { filename: f.name, reason: S.unsupported }) });
        else if (f.size > MAX_BYTES) errors.push({ text: fmt(S.rejected, { filename: f.name, reason: S.tooLarge }) });
        else valid.push(f);
      });
      if (!valid.length) { showFeedback(errors); return; }
      send(valid, opts, errors);
    });
  }

  function send(files, opts, clientErrors) {
    var fd = new FormData();
    files.forEach(function (f) { fd.append(opts.replaceDoc ? "file" : "files", f, f.name); });
    fd.append("source", opts.source || "upload");
    var id = claimId();
    var url = opts.replaceDoc ? "/documents/" + opts.replaceDoc + "/replace" : (id ? "/claims/" + id + "/documents" : "/claims/start");
    var box = feedbackBox();
    var bar = null;
    if (box) {
      box.innerHTML = "";
      var label = document.createElement("p");
      label.className = "ok";
      label.textContent = S.uploading;
      var prog = document.createElement("div");
      prog.className = "upload-progress";
      bar = document.createElement("span");
      prog.appendChild(bar);
      box.appendChild(label);
      box.appendChild(prog);
    }
    $$("[data-act=camera],[data-act=pick],.file-label").forEach(function (b) { b.setAttribute("aria-busy", "true"); });
    var xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-Requested-With", "fetch");
    xhr.upload.onprogress = function (e) {
      if (bar && e.lengthComputable) bar.style.width = Math.round((e.loaded / e.total) * 100) + "%";
    };
    xhr.onload = function () {
      $$("[aria-busy]").forEach(function (b) { b.removeAttribute("aria-busy"); });
      var data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = {}; }
      var lines = (clientErrors || []).slice();
      (data.rejected || []).forEach(function (r) { lines.push({ text: r.message }); });
      if (data.ok === false && !(data.rejected || []).length && data.message) lines.push({ text: data.message });
      if (!id && data.redirect && data.ok !== false) { window.location.assign(data.redirect); return; }
      if (data.ok && data.message) lines.unshift({ text: data.message, ok: true });
      if (xhr.status >= 500) lines.push({ text: S.error });
      refresh().then(function () { showFeedback(lines); });
    };
    xhr.onerror = function () {
      $$("[aria-busy]").forEach(function (b) { b.removeAttribute("aria-busy"); });
      showFeedback([{ text: S.network }]);
    };
    xhr.send(fd);
  }

  // ------------------------------------------------------------- workspace refresh + polling
  var pollTimer = null;
  var refreshing = null;

  function refresh() {
    var ws = $("#workspace");
    var id = claimId();
    if (!ws || !id) return Promise.resolve();
    if (refreshing) return refreshing;
    var openIds = $$("details[open]", ws).map(function (d) { return d.id; }).filter(Boolean);
    var activeId = document.activeElement && document.activeElement.id;
    refreshing = fetch("/claims/" + id + "/workspace", { headers: FETCH_HEADERS, credentials: "same-origin" })
      .then(function (r) {
        if (r.redirected && /\/submitted$/.test(r.url)) { window.location.assign(r.url); return null; }
        return r.ok ? r.text() : null;
      })
      .then(function (html) {
        if (html == null) return;
        ws.innerHTML = html;
        openIds.forEach(function (oid) { var d = document.getElementById(oid); if (d) d.open = true; });
        if (activeId) { var a = document.getElementById(activeId); if (a) a.focus({ preventScroll: true }); }
      })
      .catch(function () { toast(S.network, { error: true }); })
      .then(function () { refreshing = null; schedulePoll(); });
    return refreshing;
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    var inner = $("#workspace .ws");
    if (!inner || inner.getAttribute("data-processing") !== "true") return;
    pollTimer = setTimeout(function () {
      fetch("/claims/" + claimId() + "/state", { headers: FETCH_HEADERS, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.version !== inner.getAttribute("data-version") || !s.processing) return refresh();
          schedulePoll();
        })
        .catch(function () { schedulePoll(); });
    }, 1800);
  }

  // ------------------------------------------------------------- async forms
  document.addEventListener("submit", function (e) {
    var form = e.target.closest ? e.target.closest("form[data-async]") : null;
    if (!form) return;
    e.preventDefault();
    var confirmText = form.getAttribute("data-confirm");
    if (confirmText && !window.confirm(confirmText)) return;
    var buttons = $$("button", form);
    buttons.forEach(function (b) { b.disabled = true; });
    fetch(form.action, { method: "POST", body: new FormData(form), headers: FETCH_HEADERS, credentials: "same-origin" })
      .then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }).then(function (d) { d._status = r.status; return d; }); })
      .then(function (data) {
        if (data.ok === false || data._status >= 400) {
          toast(data.message || S.error, { error: true });
          buttons.forEach(function (b) { b.disabled = false; });
          return;
        }
        if (data.redirect && !claimId()) { window.location.assign(data.redirect); return; }
        if (data.message) toast(data.message, { link: data.new_claim ? { href: data.new_claim, label: data.new_claim_label } : null });
        return refresh();
      })
      .catch(function () {
        toast(S.network, { error: true });
        buttons.forEach(function (b) { b.disabled = false; });
      });
  });

  // ------------------------------------------------------------- click actions
  function focusUploadZone(docType) {
    var zone = $("#upload-zone");
    if (!zone) return;
    zone.classList.add("is-target");
    zone.scrollIntoView({ behavior: "smooth", block: "start" });
    var btn = $("[data-act=camera]", zone);
    if (btn) setTimeout(function () { btn.focus({ preventScroll: true }); }, 350);
    setTimeout(function () { zone.classList.remove("is-target"); }, 2600);
  }

  function openField(slug) {
    var det = document.getElementById("edit-" + slug);
    var row = document.getElementById("field-" + slug);
    if (det) {
      det.open = true;
      det.scrollIntoView({ behavior: "smooth", block: "center" });
      var input = $("input:not([type=hidden]), select", det);
      if (input) setTimeout(function () { input.focus({ preventScroll: true }); }, 300);
    } else if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function openDoc(docId, focusType) {
    var det = document.getElementById("docdetails-" + docId);
    var card = document.getElementById("doc-" + docId);
    if (det) det.open = true;
    if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    if (focusType) {
      var sel = document.getElementById("type-" + docId);
      if (sel) setTimeout(function () { sel.focus({ preventScroll: true }); }, 300);
    }
  }

  function pickReplace(docId) {
    var inp = $("#pick-replace");
    if (!inp) return;
    inp.onchange = function () { uploadFiles(inp.files, { replaceDoc: docId }); inp.value = ""; };
    inp.click();
  }

  function openSource(btn) {
    var dlg = $("#source-dialog");
    if (!dlg) return;
    var doc = btn.getAttribute("data-source-doc");
    var page = btn.getAttribute("data-source-page") || "1";
    var excerpt = btn.getAttribute("data-source-excerpt") || "";
    $("[data-source-title]", dlg).textContent = btn.getAttribute("data-source-name") || "";
    $("[data-source-excerpt]", dlg).textContent = excerpt;
    $("[data-source-excerpt-wrap]", dlg).hidden = !excerpt;
    var img = $("[data-source-img]", dlg);
    img.src = "/documents/" + doc + "/page/" + page;
    img.alt = btn.getAttribute("data-source-name") || "";
    $("[data-source-open]", dlg).href = "/documents/" + doc + "/file#page=" + page;
    dlg.showModal();
  }

  document.addEventListener("click", function (e) {
    var src = e.target.closest ? e.target.closest("[data-source-doc]") : null;
    if (src) { e.preventDefault(); openSource(src); return; }
    var closer = e.target.closest ? e.target.closest("[data-close-dialog]") : null;
    if (closer) { var d = closer.closest("dialog"); if (d) d.close(); return; }
    var el = e.target.closest ? e.target.closest("[data-act]") : null;
    if (!el) return;
    var act = el.getAttribute("data-act");
    if (act === "camera") { e.preventDefault(); openCamera({}); }
    else if (act === "pick") { e.preventDefault(); var p = $("#pick-files"); if (p) p.click(); }
    else if (act === "add") { e.preventDefault(); focusUploadZone(el.getAttribute("data-doc-type")); }
    else if (act === "retake") { e.preventDefault(); openCamera({ replaceDoc: el.getAttribute("data-doc") }); }
    else if (act === "replace") { e.preventDefault(); pickReplace(el.getAttribute("data-doc")); }
    else if (act === "field") { e.preventDefault(); openField(el.getAttribute("data-field")); }
    else if (act === "doc") { e.preventDefault(); openDoc(el.getAttribute("data-doc"), el.getAttribute("data-focus") === "type"); }
    else if (act === "show-issues") {
      e.preventDefault();
      var st = $("#claim-status");
      if (st) { st.scrollIntoView({ behavior: "smooth", block: "start" }); var first = $(".sev-blocking button, .sev-blocking select", st); if (first) setTimeout(function () { first.focus({ preventScroll: true }); }, 400); }
    }
  });

  // light-dismiss for dialogs
  $$("dialog").forEach(function (dlg) {
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
  });

  // ------------------------------------------------------------- camera
  var cam = { stream: null, shots: [], replaceDoc: null };
  var camDlg = $("#camera-dialog");
  var video = $("#camera-video");

  function camText(key) { return camDlg ? camDlg.getAttribute("data-txt-" + key) || "" : ""; }

  function stopCamera() {
    if (cam.stream) cam.stream.getTracks().forEach(function (tr) { tr.stop(); });
    cam.stream = null;
    if (video) video.srcObject = null;
  }

  function nativeCamera(replaceDoc) {
    var inp = $("#native-camera-input");
    if (!inp) { var p = $("#pick-files") || $("[data-upload-input]"); if (p) p.click(); return; }
    inp.multiple = !replaceDoc;
    inp.onchange = function () { uploadFiles(inp.files, { source: "camera", replaceDoc: replaceDoc }); inp.value = ""; };
    inp.click();
  }

  function renderShots() {
    var list = $("[data-camera-shots]", camDlg);
    if (!list) return;
    list.innerHTML = "";
    cam.shots.forEach(function (shot, idx) {
      var li = document.createElement("li");
      var img = document.createElement("img");
      img.src = shot.url;
      img.alt = "";
      var del = document.createElement("button");
      del.type = "button";
      del.setAttribute("aria-label", camText("remove"));
      del.innerHTML = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-x"/></svg>';
      del.addEventListener("click", function () {
        URL.revokeObjectURL(shot.url);
        cam.shots.splice(idx, 1);
        renderShots();
      });
      li.appendChild(img);
      li.appendChild(del);
      list.appendChild(li);
    });
    var use = $("[data-camera-use]", camDlg);
    use.disabled = cam.shots.length === 0;
    $("[data-camera-use-label]", camDlg).textContent = fmt(camText("use"), { n: cam.shots.length });
  }

  function showCamError(text) {
    var box = $("[data-camera-error]", camDlg);
    $("[data-camera-error-text]", camDlg).textContent = text;
    box.hidden = false;
    $("[data-camera-status]", camDlg).textContent = "";
    $("[data-camera-shoot]", camDlg).disabled = true;
  }

  function openCamera(opts) {
    var canUse = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
    if (!camDlg || !canUse) {
      if (camDlg && !window.isSecureContext) toast(camText("unavailable"));
      nativeCamera(opts.replaceDoc || null);
      return;
    }
    cam.replaceDoc = opts.replaceDoc || null;
    cam.shots = [];
    renderShots();
    $("[data-camera-error]", camDlg).hidden = true;
    $("[data-camera-shoot]", camDlg).disabled = false;
    $("[data-camera-status]", camDlg).textContent = S.cameraStarting || "";
    camDlg.showModal();
    navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 2560 }, height: { ideal: 1920 } },
      audio: false
    }).then(function (stream) {
      if (!camDlg.open) { stream.getTracks().forEach(function (tr) { tr.stop(); }); return; }
      cam.stream = stream;
      video.srcObject = stream;
      return video.play().then(function () { $("[data-camera-status]", camDlg).textContent = ""; });
    }).catch(function (err) {
      stopCamera();
      var denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
      showCamError(denied ? camText("denied") : camText("unavailable"));
    });
  }

  if (camDlg) {
    camDlg.addEventListener("close", function () {
      stopCamera();
      cam.shots.forEach(function (s) { URL.revokeObjectURL(s.url); });
    });
    $("[data-camera-close]", camDlg).addEventListener("click", function () { camDlg.close(); });
    $("[data-camera-shoot]", camDlg).addEventListener("click", function () {
      if (!cam.stream || !video.videoWidth) return;
      var canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      canvas.toBlob(function (blob) {
        if (!blob) return;
        var file = new File([blob], "camera-" + new Date().toISOString().replace(/[:.]/g, "-") + ".jpg", { type: "image/jpeg" });
        if (cam.replaceDoc) { cam.shots.forEach(function (s) { URL.revokeObjectURL(s.url); }); cam.shots = []; }
        cam.shots.push({ file: file, url: URL.createObjectURL(blob) });
        renderShots();
      }, "image/jpeg", 0.9);
    });
    $("[data-camera-use]", camDlg).addEventListener("click", function () {
      var files = cam.shots.map(function (s) { return s.file; });
      var replaceDoc = cam.replaceDoc;
      camDlg.close();
      if (files.length) uploadFiles(files, { source: "camera", replaceDoc: replaceDoc });
    });
    $("[data-camera-native]", camDlg).addEventListener("click", function () {
      var replaceDoc = cam.replaceDoc;
      camDlg.close();
      nativeCamera(replaceDoc);
    });
    // Never leave the camera running in the background.
    document.addEventListener("visibilitychange", function () { if (document.hidden && camDlg.open) camDlg.close(); });
    window.addEventListener("pagehide", stopCamera);
  }

  // ------------------------------------------------------------- file inputs
  var pick = $("#pick-files");
  if (pick) pick.addEventListener("change", function () { uploadFiles(pick.files, {}); pick.value = ""; });
  $$("[data-upload-input]").forEach(function (inp) {
    inp.addEventListener("change", function () { uploadFiles(inp.files, {}); inp.value = ""; });
  });

  // ------------------------------------------------------------- review page
  var submitForm = $("form[data-submit-once]");
  if (submitForm) {
    var box = $("input[name=reviewed]", submitForm);
    var btn = $("button[type=submit]", submitForm);
    var sync = function () { if (btn && !btn.hasAttribute("data-blocked")) btn.disabled = !box.checked; };
    if (box) { box.addEventListener("change", sync); sync(); }
    submitForm.addEventListener("submit", function () { setTimeout(function () { if (btn) btn.disabled = true; }, 0); });
  }
  var printBtn = $("[data-print]");
  if (printBtn) printBtn.addEventListener("click", function () { window.print(); });

  schedulePoll();
})();
