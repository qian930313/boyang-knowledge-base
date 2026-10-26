/*
 * 管理后台表单提交：带真实上传进度的就地刷新。
 *
 * 背景：培训视频常有 1GB 以上，即使在本地服务器上，一次上传也要几十秒到一两分钟。
 * 如果期间界面上只有一句静态的「保存中…」，用户会以为页面卡死而刷新，
 * 刷新会直接中断上传（服务端记为 400 Bad Request），最后只看到一句「保存失败」。
 * 所以这里必须给出真实百分比 + 上传期间阻止离开页面。
 */
(function () {
  var MAX_MB = window.KB_MAX_MB || 4096;
  var pending = 0;

  function toast(msg, kind) {
    var t = document.createElement('div');
    t.className = 'panel-toast' + (kind ? ' panel-toast-' + kind : '');
    t.textContent = msg;
    // 多条提示按出现先后依次下移：上传失败时往往连续弹出多条（重试、最终失败），
    // 若都堆在同一位置会互相遮挡，用户最后只看到一团看不清的重影。
    var n = document.querySelectorAll('.panel-toast').length;
    if (n) t.style.top = (24 + n * 52) + 'px';
    document.body.appendChild(t);
    // 错误提示停留更久（8s），成功提示 2.5s；避免一闪而过被忽略
    var life = kind === 'error' ? 8000 : (kind === 'ok' ? 2600 : 2000);
    setTimeout(function () { t.classList.add('out'); }, life - 500);
    setTimeout(function () { t.parentNode && t.parentNode.removeChild(t); }, life);
  }

  function fmtSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(bytes >= 10485760 ? 0 : 1) + ' MB';
    if (bytes >= 1024) return Math.round(bytes / 1024) + ' KB';
    return bytes + ' B';
  }

  // 上传中禁止离开/刷新页面（大文件传一半刷新等于全部重来）
  function guard(on) {
    pending += on ? 1 : -1;
    if (pending < 0) pending = 0;
    window.onbeforeunload = pending > 0
      ? function () { return '文件正在上传，离开页面会中断上传。'; }
      : null;
  }

  /* 必填项（文件除外）全部填写后才允许点击保存 */
  function bindRequired(root) {
    root.querySelectorAll('form.js-all-required').forEach(function (f) {
      var btn = f.querySelector('button[type=submit]');
      if (!btn || btn.dataset.kbBound) return;
      btn.dataset.kbBound = '1';
      var need = Array.prototype.filter.call(f.querySelectorAll('[required]'),
        function (el) { return el.type !== 'file'; });
      function chk() {
        btn.disabled = !need.every(function (el) { return String(el.value).trim() !== ''; });
      }
      f.addEventListener('input', chk);
      f.addEventListener('change', chk);
      f.kbCheck = chk;
      chk();
    });
  }

  /*
   * 提交表单。返回 Promise：
   *   成功 -> resolve(响应 HTML)，失败 -> reject(Error)
   * opts: { url: 缺省提交地址, submitter: 被点击的提交按钮, onSuccess: fn(html) }
   * submitter 用于「逐个保存」这类同一表单里有多个提交按钮的场景：
   * 只有把被点的按钮传进 FormData，服务端才知道用户点的是哪一行。
   */
  function submit(form, opts) {
    opts = opts || {};
    if (form.dataset.kbBusy === '1') {
      toast('正在保存，请稍候…');
      return Promise.reject(new Error('busy'));
    }
    // 有多个提交按钮时（如「逐个保存」的行内按钮），状态要显示在被点的那个上
    var btn = opts.submitter || form.querySelector('button[type=submit],button:not([type])');
    // 必须在改动按钮状态之前构造 FormData：
    // 被禁用的控件会被浏览器排除在表单数据之外，若此刻才构造，
    // 被点的那个提交按钮就会丢失，服务端认不出改的是哪一行。
    var fd;
    try { fd = new FormData(form, opts.submitter || undefined); }
    catch (e) { fd = new FormData(form); }
    var files = [];
    Array.prototype.forEach.call(form.querySelectorAll('input[type=file]'), function (inp) {
      Array.prototype.forEach.call(inp.files || [], function (f) { files.push(f); });
    });

    // 本地先校验体积：超限立刻提示，不必等整包传完再被服务器拒掉
    for (var i = 0; i < files.length; i++) {
      if (files[i].size > MAX_MB * 1048576) {
        toast('✕ ' + files[i].name + '（' + fmtSize(files[i].size) + '）超过 ' + MAX_MB + ' MB 上限');
        return Promise.reject(new Error('too_large'));
      }
    }

    var total = files.reduce(function (n, f) { return n + f.size; }, 0);
    var wrap = null, bar = null;
    if (files.length) {
      wrap = document.createElement('div');
      wrap.className = 'up-wrap';
      wrap.innerHTML = '<div class="up-bar"><i></i></div><div class="up-tip"></div>';
      wrap.querySelector('.up-tip').textContent =
        '共 ' + files.length + ' 个文件 / ' + fmtSize(total) + '，上传期间请勿刷新或关闭页面';
      (btn ? btn.parentNode : form).insertBefore(wrap, btn ? btn.nextSibling : null);
      bar = wrap.querySelector('i');
    }
    // 注意：这里刻意不用 btn.disabled —— 一旦请求中途异常，
    // 禁用状态会永久留在按钮上，用户就会感觉「保存点了没反应」。
    // 改用 is-busy 标记 + 提交中的文案，重复点击由上面的 kbBusy 拦掉。
    if (btn) {
      btn.dataset.txt = btn.textContent;
      btn.classList.add('is-busy');
      btn.textContent = files.length ? '上传中 0%' : (form.dataset.busy || '保存中…');
    }
    form.dataset.kbBusy = '1';
    function setBtn(txt) { if (btn) btn.textContent = txt; }
    guard(true);

    return new Promise(function (resolve, reject) {
      var done = false, timer = null;
      var xhr = new XMLHttpRequest();
      function cleanup() {
        if (done) return;
        done = true;
        if (timer) clearTimeout(timer);
        guard(false);
        delete form.dataset.kbBusy;
        if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
        if (btn) {
          btn.classList.remove('is-busy');
          btn.disabled = false;                       // 兜底：确保永远是「可点」的
          btn.textContent = btn.dataset.txt || '保存';
        }
        if (form.kbCheck) form.kbCheck();   // 交还给「必填项」校验决定是否可点
      }
      // 看门狗：请求卡住（服务器重启/连接挂死）时也要把界面恢复可操作
      timer = setTimeout(function () {
        if (done) return;
        try { xhr.abort(); } catch (e) { /* 忽略 */ }
        cleanup();
        toast('✕ 保存超时，请重试');
        reject(new Error('timeout'));
      }, files.length ? 1800000 : 30000);
      xhr.open('POST', form.getAttribute('action') || opts.url || location.href, true);
      xhr.setRequestHeader('X-Requested-With', 'fetch');
      if (xhr.upload) {
        xhr.upload.onprogress = function (e) {
          if (!e.lengthComputable) { setBtn('上传中…'); return; }
          var pct = Math.round(e.loaded / e.total * 100);
          setBtn('上传中 ' + pct + '%');
          if (bar) bar.style.width = pct + '%';
        };
      }
      xhr.onload = function () {
        cleanup();
        if (xhr.status >= 200 && xhr.status < 400) { resolve(xhr.responseText); return; }
        var msg = xhr.status === 413 ? '✕ 文件太大，超出 ' + MAX_MB + ' MB 上限'
                : xhr.status === 400 ? '✕ 上传被中断（页面被刷新或连接断开），请重试'
                : (xhr.status === 401 || xhr.status === 403) ? '✕ 登录状态已失效，请重新登录后台'
                : '✕ 保存失败（HTTP ' + xhr.status + '），请重试';
        toast(msg);
        reject(new Error('HTTP ' + xhr.status));
      };
      xhr.onerror = function () { cleanup(); toast('✕ 网络错误，上传失败，请重试'); reject(new Error('network')); };
      xhr.onabort = function () { cleanup(); toast('✕ 上传已中断'); reject(new Error('abort')); };
      try {
        xhr.send(fd);
      } catch (err) {
        // 请求根本没发出去：退回浏览器原生提交，绝不留下「点了没反应」
        cleanup();
        toast('✕ 提交未发出，已改用浏览器直接提交');
        HTMLFormElement.prototype.submit.call(form);
        reject(err);
      }
    });
  }

  /* -------------------------------------------------------------------------
   * 浏览器「分片直传 R2」。
   * 历史教训（两次失败的方案，勿回退）：
   *   1) 整文件单 PUT 直传 —— 本网络对 r2.cloudflarestorage.com 的大块单请求会
   *      中途断流：进度条走完但对象从未落进 R2，且常常连错误提示都没有。
   *   2) 服务器中转整文件 —— Render 上 gunicorn worker 读大请求体超过 30s
   *      即被主进程杀掉（SIGKILL），必现 500。
   *   现方案：文件切成 8MB 小片，每片独立向服务器要预签名 URL 后直传 R2，
   *   单片失败自动重签重试 3 次；服务器只签名与合并，不经手文件本体。
   * ---------------------------------------------------------------------- */
  function val(form, name) {
    var el = form.querySelector('[name="' + name + '"]');
    return el ? el.value : '';
  }

  /*
   * 所有 XHR 都必须设 timeout。
   * 血的教训：XHR 默认 timeout=0（永不超时）。一旦连接中途挂死（既收不到响应、
   * 也不断开），onload / onerror 都不会触发，Promise 永远悬着 —— 界面上就是
   * 进度条停在某个百分比、之后再无任何提示，用户只能以为「传完了但没结果」。
   * 设了 timeout 后，挂死的连接会被 ontimeout 捕获，从而进入重试或报错流程。
   */
  var XHR_TIMEOUT_MS = 180000;   // 3 分钟：8MB 单片在极差网络下也够用
  var MAX_ATTEMPTS = 4;          // 单片最多尝试 4 次（首次 + 3 次退避重试）

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function jsonPost(url, obj) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.setRequestHeader('X-Requested-With', 'fetch');
      xhr.timeout = 60000;
      xhr.onload = function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 400 && !data.error) resolve(data);
        else reject(new Error(data.error || ('HTTP ' + xhr.status)));
      };
      xhr.onerror = function () { reject(new Error('网络错误')); };
      xhr.ontimeout = function () { reject(new Error('服务器响应超时')); };
      xhr.send(JSON.stringify(obj || {}));
    });
  }

  function putOnce(url, blob, contentType, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('PUT', url, true);
      xhr.timeout = XHR_TIMEOUT_MS;
      if (contentType) xhr.setRequestHeader('Content-Type', contentType);
      if (xhr.upload) {
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
        };
      }
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 400) resolve(xhr.getResponseHeader('ETag') || '');
        else reject(new Error('HTTP ' + xhr.status));
      };
      xhr.onerror = function () { reject(new Error('网络错误')); };
      xhr.ontimeout = function () { reject(new Error('连接超时')); };
      xhr.onabort = function () { reject(new Error('已中断')); };
      xhr.send(blob);
    });
  }

  /* 上传单个文件（分片直传 + 逐片重试）。返回 Promise，resolve 在数据库登记完成后。 */
  function uploadOne(cid, batch, meta, file, onProgress) {
    var sig = null;
    return jsonPost('/admin/course/' + cid + '/mpu-init', { name: file.name, size: file.size })
      .then(function (s) { sig = s; })
      .then(function () {
        var partSize = sig.partSize || 8 * 1024 * 1024;
        var total = Math.max(1, Math.ceil(file.size / partSize));
        var parts = [];
        var sentBefore = 0;   // 已完成分片的累计字节数
        var curPart = 0;      // 当前正在传第几片（用于界面上给出「还在动」的明确信号）
        function report(partLoaded) {
          if (onProgress) onProgress(Math.min(sentBefore + partLoaded, file.size), file.size,
                                     curPart, total);
        }
        var pno = 0;
        function nextPart() {
          if (pno >= total) return Promise.resolve(parts);
          var n = ++pno;
          curPart = n;
          var start = (n - 1) * partSize;
          var blob = file.slice(start, Math.min(start + partSize, file.size));
          var attempts = 0;
          function tryPut() {
            attempts += 1;
            // 每次尝试都重新签名单片 URL（旧 URL 可能已过期或已被 R2 拒绝）
            return jsonPost('/admin/course/' + cid + '/mpu-sign-part',
                            { key: sig.key, uploadId: sig.uploadId, partNumber: n })
              .then(function (u) {
                return putOnce(u.url, blob, sig.contentType, function (loaded) { report(loaded); })
                  .then(function (etag) {
                    if (!etag) throw new Error('分片响应缺少 ETag');
                    sentBefore += blob.size;
                    report(0);
                    parts.push({ ETag: etag.replace(/"/g, ''), PartNumber: n });
                  });
              })
              .catch(function (e) {
                if (attempts < MAX_ATTEMPTS) {
                  toast('第 ' + n + '/' + total + ' 片上传失败（' + (e.message || '') +
                        '），正在重试 ' + attempts + '/' + (MAX_ATTEMPTS - 1) + '…', 'warn');
                  // 退避重试：网络抖动往往持续数秒，立刻重发大概率再失败
                  return wait(attempts * 2000).then(tryPut);
                }
                throw new Error('分片 ' + n + '/' + total + ' 连续 ' + attempts +
                                ' 次上传失败：' + (e.message || ''));
              });
          }
          return tryPut().then(nextPart);
        }
        return nextPart();
      })
      .then(function (parts) {
        // 全部分片传完：合并 + 登记数据库
        return jsonPost('/admin/course/' + cid + '/mpu-complete', {
          key: sig.key, uploadId: sig.uploadId, parts: parts,
          batch: batch, meta: meta,
          title: sig.title || file.name.replace(/\.[^.]+$/, '')
        }).catch(function (e) {
          throw new Error('「' + file.name + '」最后一步处理失败：' + (e.message || ''));
        });
      })
      .catch(function (e) {
        // 统一清理：任何一步失败（分片连续重试仍失败、合并失败、登记失败）都要
        // 放弃该文件的分片任务。否则已传出去的分片会作为孤儿留在 R2 里占着存储额度，
        // 用户会看到「已用容量」上涨、桶里却找不到文件，自己也无从清理。
        if (sig && sig.key && sig.uploadId) {
          jsonPost('/admin/course/' + cid + '/mpu-abort',
                   { key: sig.key, uploadId: sig.uploadId }).catch(function () {});
        }
        throw e;
      });
  }

  function kbUploadDirect(form) {
    var cid = form.getAttribute('data-cid');
    if (!cid) return Promise.reject(new Error('缺少课程 id'));
    var files = [];
    Array.prototype.forEach.call(form.querySelectorAll('input[type=file]'), function (inp) {
      Array.prototype.forEach.call(inp.files || [], function (f) { files.push(f); });
    });
    if (!files.length) return Promise.reject(new Error('no_file'));
    for (var i = 0; i < files.length; i++) {
      if (files[i].size > MAX_MB * 1048576) {
        toast('✕ ' + files[i].name + '（' + fmtSize(files[i].size) + '）超过 ' + MAX_MB + ' MB 上限');
        return Promise.reject(new Error('too_large'));
      }
    }
    var meta = {
      course_title: val(form, 'course_title'),
      lecturer: val(form, 'lecturer'),
      record_time: val(form, 'record_time'),
      sort: val(form, 'sort')
    };
    var batch = 'b' + Date.now() + Math.random().toString(36).slice(2);
    var total = files.reduce(function (n, f) { return n + f.size; }, 0);
    var wrap = document.createElement('div');
    wrap.className = 'up-wrap';
    wrap.innerHTML = '<div class="up-bar"><i></i></div><div class="up-tip"></div>';
    wrap.querySelector('.up-tip').textContent =
      '共 ' + files.length + ' 个文件 / ' + fmtSize(total) + '，分片直传到云存储，请勿刷新或关闭页面';
    var btn = form.querySelector('button[type=submit], button:not([type])');
    if (btn) { btn.disabled = true; btn.dataset.txt = btn.textContent; btn.textContent = '上传中 0%'; }
    (btn ? btn.parentNode : form).insertBefore(wrap, btn ? btn.nextSibling : null);
    var bar = wrap.querySelector('i');
    guard(true);

    var sentTotal = 0;  // 已彻底完成的字节数（跨文件累计）
    var tip = wrap.querySelector('.up-tip');
    // 大视频要传十几分钟。除了百分比，还必须给出「正在传第几片、已传多少」
    // 这类持续变化的细节，否则用户看到百分比长时间不动就会以为卡死而去刷新页面。
    function setOverall(bytes, fileIdx, part, parts) {
      var pct = total ? Math.round(bytes / total * 100) : 100;
      if (bar) bar.style.width = pct + '%';
      if (btn) btn.textContent = '上传中 ' + pct + '%';
      if (tip) {
        var seg = [];
        if (files.length > 1) seg.push('文件 ' + (fileIdx || 1) + '/' + files.length);
        if (parts > 1) seg.push('第 ' + (part || 1) + '/' + parts + ' 片');
        seg.push(fmtSize(bytes) + ' / ' + fmtSize(total));
        tip.textContent = seg.join(' · ') + '，请勿刷新或关闭页面';
      }
    }
    function cleanup() {
      guard(false);
      if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.txt || '保存'; }
    }
    function fail(msg) { cleanup(); toast('✕ ' + msg, 'error'); }

    function step(idx) {
      if (idx >= files.length) {
        cleanup();
        toast('✓ 上传成功，正在刷新页面…', 'ok');
        return Promise.resolve();
      }
      var f = files[idx];
      return uploadOne(cid, batch, meta, f, function (loaded, size, part, parts) {
        setOverall(sentTotal + loaded, idx + 1, part, parts);
      }).then(function () {
        sentTotal += f.size;
        setOverall(sentTotal, idx + 1);
        return step(idx + 1);
      }).catch(function (e) { fail(e.message || '上传失败'); throw e; });
    }
    return step(0);
  }

  window.kbToast = toast;
  window.kbSubmitForm = submit;
  window.kbBindRequired = bindRequired;
  window.kbUploadDirect = kbUploadDirect;
})();
