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

  function toast(msg) {
    var t = document.createElement('div');
    t.className = 'panel-toast';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add('out'); }, 1800);
    setTimeout(function () { t.parentNode && t.parentNode.removeChild(t); }, 2500);
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

  window.kbToast = toast;
  window.kbSubmitForm = submit;
  window.kbBindRequired = bindRequired;
})();
