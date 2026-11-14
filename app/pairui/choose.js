(function () {
  'use strict';

  const U = window.PairUI;
  const $ = U.$;
  let S = null;
  let busy = false;

  function chip(text, kind) {
    return '<span class="chip ' + (kind || '') + '">' + U.esc(text) + '</span>';
  }

  function setRole(id, option, extraDisabled, extraReason) {
    const badge = $('#' + id + 'Badge');
    const note = $('#' + id + 'Note');
    const button = $('#' + id + 'Btn');
    const card = document.querySelector('.role-card[data-role="' + id + '"]');
    const enabled = !!option && option.enabled && !extraDisabled;
    const reason = extraReason || (option && option.reason) || '';
    const noteText = enabled ? ((option && option.note) || '') : reason;

    card.classList.toggle('disabled', !enabled);
    badge.textContent = enabled ? '可用' : '不可用';
    badge.className = 'role-badge ' + (enabled ? 'ok' : 'bad');
    button.disabled = !enabled;
    note.textContent = noteText;
    note.hidden = !noteText;
  }

  function render() {
    $('#' + 'chips').innerHTML = [
      chip('自动检测', 'ok'),
      chip(S.os.label, 'info'),
      chip(S.hostname, ''),
      chip(S.is_root ? 'root' : '普通用户', S.is_root ? 'warn' : ''),
    ].join('');

    $('#' + 'localHint').textContent = S.local ? '本机页面' : '远程页面';
    $('#' + 'machineInfo').innerHTML = [
      ['系统', S.os.label],
      ['详情', S.os.detail],
      ['主机名', S.hostname],
      ['当前身份', S.is_root ? 'root' : '普通用户'],
      ['控制台', '端口 ' + S.port],
    ].map(function (row) {
      return '<span class="k">' + U.esc(row[0]) + '</span><span class="v">' +
        U.esc(row[1]) + '</span>';
    }).join('');

    const addrBox = $('#' + 'addrList');
    if (S.addresses && S.addresses.length) {
      addrBox.innerHTML = S.addresses.map(function (ip) {
        return '<div class="addr"><span class="mono">' + U.esc(ip) + '</span></div>';
      }).join('');
    } else {
      addrBox.innerHTML = '<div class="addr"><span class="hint">没有检测到非回环 IPv4 地址</span></div>';
    }

    const options = {};
    (S.roles || []).forEach(function (item) { options[item.id] = item; });

    setRole(
      'control',
      options.controller,
      !S.local,
      !S.local ? '控制端只能在本机打开：请在你要操作的那台机器上运行控制台' : ''
    );
    setRole('controlled', options.controlled, false, '');
  }

  async function choose(role) {
    if (busy) return;
    busy = true;
    const overlay = $('#' + 'switching');
    const text = $('#' + 'switchText');
    overlay.hidden = false;
    text.textContent = role === 'controller' ? '准备控制端…' : '准备被控端…';

    const data = await U.api('POST', '/api/choose', { role: role });
    if (!data.ok) {
      overlay.hidden = true;
      busy = false;
      U.toast(data.error || '切换失败', 'bad');
      return;
    }

    let host = location.hostname || '127.0.0.1';
    if (host.indexOf(':') !== -1 && host.charAt(0) !== '[') host = '[' + host + ']';
    const url = 'http://' + host + ':' + data.port + '/';
    text.textContent = '正在打开 ' + url;
    setTimeout(function () { location.href = url; }, 1500);
  }

  async function load() {
    const data = await U.api('GET', '/api/status');
    if (!data.ok) {
      U.toast(data.error || '读取本机信息失败', 'bad');
      return;
    }
    S = data;
    render();
  }

  $('#controlBtn').addEventListener('click', function () { choose('controller'); });
  $('#controlledBtn').addEventListener('click', function () { choose('controlled'); });

  load();
})();
