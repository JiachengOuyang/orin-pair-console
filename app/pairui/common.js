(function () {
  'use strict';

  const U = {};

  U.$ = function (selector, root) {
    return (root || document).querySelector(selector);
  };

  U.$$ = function (selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  };

  U.esc = function (value) {
    return String(value === undefined || value === null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  let toastTimer = null;
  U.toast = function (message, kind) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.className = 'toast show ' + (kind || 'ok');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.className = 'toast';
    }, 3600);
  };

  U.api = async function (method, path, body, extraHeaders) {
    const headers = Object.assign({ Accept: 'application/json' }, extraHeaders || {});
    const options = { method: method, headers: headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      headers['X-Orin-Pair'] = '1';
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options);
      let data;
      try {
        data = await response.json();
      } catch (err) {
        data = { ok: false, error: '响应不是 JSON' };
      }
      data._status = response.status;
      return data;
    } catch (err) {
      return { ok: false, error: '连不上本地服务：' + err.message, _status: 0 };
    }
  };

  U.confirm = function (options) {
    const opts = options || {};
    return new Promise(function (resolve) {
      const dialog = document.getElementById('modal');
      if (!dialog) {
        resolve(window.confirm(opts.title || '确认？'));
        return;
      }
      document.getElementById('modalTitle').textContent = opts.title || '确认';
      document.getElementById('modalBody').innerHTML = opts.html || '';
      const okButton = document.getElementById('modalOk');
      okButton.textContent = opts.okText || '确认';
      okButton.className = 'btn ' + (opts.danger ? 'danger' : 'primary');

      function onClose() {
        dialog.removeEventListener('close', onClose);
        resolve(dialog.returnValue === 'ok');
      }
      dialog.addEventListener('close', onClose);
      dialog.returnValue = '';
      dialog.showModal();
    });
  };

  U.copy = async function (text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (e) {
        ok = false;
      }
      area.remove();
      return ok;
    }
  };

  window.PairUI = U;
})();
