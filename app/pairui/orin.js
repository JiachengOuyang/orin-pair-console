(function () {
  'use strict';

  const U = window.PairUI;
  const $ = U.$;
  const METHOD_LABEL = { usb: 'USB device', mdns: 'mDNS', manual: '静态 IP' };

  let S = null;
  let gateCode = sessionStorage.getItem('orinPairCode') || '';
  let busy = false;
  let initialized = false;
  let restarting = false;
  let setupLog = '';

  function authHeaders() {
    return gateCode ? { 'X-Pair-Code': gateCode } : {};
  }

  function chip(text, kind) {
    return '<span class="chip ' + (kind || '') + '">' + U.esc(text) + '</span>';
  }

  function currentMethod() {
    const radio = document.querySelector('input[name="method"]:checked');
    return radio ? radio.value : 'usb';
  }

  function updateTiles() {
    U.$$('.tile').forEach(function (tile) {
      const input = tile.querySelector('input');
      tile.classList.toggle('selected', input.checked);
    });
    $('#manualFields').hidden = currentMethod() !== 'manual';
  }

  async function refresh(force) {
    if (busy && !force) return;
    if (restarting) return;
    const data = await U.api('GET', '/api/status', undefined, authHeaders());
    if (data._status === 401) {
      showGate();
      return;
    }
    if (!data.ok) {
      U.toast(data.error || '读取状态失败', 'bad');
      return;
    }
    S = data;
    render();
  }

  function showGate() {
    const dialog = $('#gate');
    if (!dialog.open) {
      $('#gateCode').value = gateCode;
      dialog.showModal();
    }
  }

  function showAuth(url) {
    const box = $('#tsAuth');
    if (!url) {
      box.hidden = true;
      box.textContent = '';
      return;
    }
    box.hidden = false;
    box.innerHTML = '登录授权链接：<a href="' + U.esc(url) +
      '" target="_blank" rel="noreferrer">' + U.esc(url) + '</a>';
  }

  function render() {
    if (!S) return;
    const local = S.local !== false;
    const canNet = local && (S.is_root || S.demo);

    // 顶栏状态
    const chips = [];
    if (S.demo) chips.push(chip('演示模式', 'info'));
    else chips.push(chip(S.is_root ? 'root' : '普通用户', S.is_root ? 'ok' : 'warn'));
    chips.push(chip(S.sshd ? 'sshd 运行中' : 'sshd 未运行', S.sshd ? 'ok' : 'bad'));
    chips.push(chip(S.avahi ? 'mDNS 可用' : 'mDNS 未启用', S.avahi ? 'ok' : 'warn'));
    const ufwActive = /(^|\s)active(\s|$)/i.test(S.ufw || '') && !/inactive/i.test(S.ufw || '');
    chips.push(chip('ufw ' + (ufwActive ? '已启用' : '未启用'), ufwActive ? 'warn' : ''));
    if (S.tailscale && S.tailscale.up) chips.push(chip('Tailscale', 'info'));
    $('#chips').innerHTML = chips.join('');

    $('#rootBanner').hidden = S.demo || S.is_root || !local;
    $('#rootSwitch').hidden = !(local && !S.is_root && !S.demo);

    // 环境准备
    const envRows = [
      ['sshd', S.sshd ? '运行中' : (S.sshd_installed ? '未运行' : '未安装')],
      ['tmux', S.tmux ? '已安装' : '未安装'],
      ['avahi', S.avahi ? '运行中' : (S.avahi_installed ? '未运行' : '未安装')],
      ['ssh 目录', S.ssh_dir_ok ? '700/600 已就绪' : '未就绪'],
      ['dialout', S.dialout ? '已在组里' : '未加入']
    ];
    $('#envList').innerHTML = envRows.map(function (row) {
      return '<span class="k">' + U.esc(row[0]) + '</span><span class="v">' +
        U.esc(row[1]) + '</span>';
    }).join('');
    const envOk = S.sshd && S.tmux && S.avahi && S.ssh_dir_ok;
    $('#envHint').textContent = envOk ? '已就绪' : '有缺失';
    const canSetup = local && (S.is_root || S.demo);
    $('#setupBtn').disabled = !canSetup || busy;
    if (setupLog) {
      $('#envNote').textContent = setupLog;
    } else {
      $('#envNote').textContent = !local
        ? '环境准备只能在本机页面上执行；远程页面只读。'
        : (canSetup
          ? '会安装或修复 openssh-server、tmux、avahi，启用 ssh 和 avahi，准备 ~/.ssh（700/600），把目标账号加入 dialout。可以重复点，已装的会跳过。'
          : '需要 root：在下面输入密码切换后再点“安装 / 修复”。');
    }

    // 连接方式
    if (!initialized) {
      const method = (S.network && S.network.method) || 'usb';
      const radio = document.querySelector('input[name="method"][value="' + method + '"]');
      if (radio) radio.checked = true;
      initialized = true;
    }
    updateTiles();

    const connections = S.connections || [];
    const signature = connections.map(function (c) { return c.name + '|' + (c.device || ''); }).join(';');
    const select = $('#connSelect');
    if (select.dataset.signature !== signature) {
      const previous = select.value;
      if (connections.length) {
        select.innerHTML = connections.map(function (c) {
          const label = c.name + (c.device ? ' · ' + c.device : '');
          return '<option value="' + U.esc(c.name) + '">' + U.esc(label) + '</option>';
        }).join('');
      } else {
        select.innerHTML = '<option value="">没有可用的 nmcli 连接</option>';
      }
      select.dataset.signature = signature;
      if (previous && connections.some(function (c) { return c.name === previous; })) {
        select.value = previous;
      }
    }

    const note = $('#netNote');
    if (!local) {
      note.textContent = '网络配置只能在 Orin 本机的页面上操作；远程页面只读，可查看状态和解除配对。';
    } else if (!S.is_root && !S.demo) {
      note.textContent = '网络设置需要 root；在「环境准备」里输入密码切换后即可操作。';
    } else {
      note.textContent = '应用静态 IP 后，如果你正通过这条连接访问本页，页面可能断开——这是正常的，改完用新地址重开。';
    }
    ['applyBtn', 'revertBtn', 'fwBtn'].forEach(function (id) {
      $('#' + id).disabled = !canNet;
    });

    const method = S.network && S.network.method;
    $('#netHint').textContent = method
      ? (METHOD_LABEL[method] || method) + (S.network.applied_at ? ' · ' + S.network.applied_at : '')
      : '还没应用';

    // 地址
    const addresses = S.addresses || [];
    let addrHtml = addresses.map(function (a) {
      return '<div class="addr"><span class="if">' + U.esc(a.ifname) + '</span>' +
        '<span>' + U.esc(a.cidr) + '</span>' +
        '<button class="btn ghost small" data-copy="' + U.esc(a.cidr) + '">复制</button></div>';
    }).join('');
    if (S.mdns_name) {
      addrHtml += '<div class="addr"><span class="if">mDNS</span><span>' + U.esc(S.mdns_name) +
        '</span><button class="btn ghost small" data-copy="' + U.esc(S.mdns_name) + '">复制</button></div>';
    }
    $('#addrList').innerHTML = addrHtml || '<div class="empty">还没有非回环地址。检查网线或 USB 连接。</div>';

    // 本机信息
    const rows = [
      ['主机名', S.hostname],
      ['目标账号', S.target_user + (S.user_exists ? '' : '（不存在，先跑 setup.sh）')],
      ['家目录', S.target_home],
      ['SSH 服务', S.sshd ? '运行中' : '未运行'],
      ['mDNS', S.avahi ? (S.mdns_name || '可用') : '未启用'],
      ['ufw', S.ufw],
      ['连接方式', method ? (METHOD_LABEL[method] || method) : '未应用']
    ];
    $('#machineInfo').innerHTML = rows.map(function (row) {
      return '<span class="k">' + U.esc(row[0]) + '</span><span class="v">' +
        U.esc(row[1] || '—') + '</span>';
    }).join('');

    // 远程组网（Tailscale）
    const ts = S.tailscale || {};
    const tsPeers = ts.peers || [];
    const tsHealth = (ts.health || []).join(' ');
    const tsStuck = !!ts.available && !ts.up &&
      (String(ts.state || '') === 'NoState' || /starting|no state/i.test(tsHealth));
    const tsRows = [
      ['客户端', ts.available ? '已安装' : '未安装'],
      ['状态', ts.up ? '已连接' : (ts.available ? (ts.state === 'NoState' ? '未就绪' : (ts.state || '未连接')) : '—')],
      ['地址', ts.ip || '—'],
      ['在线对端', tsPeers.length
        ? tsPeers.map(function (p) { return p.hostname; }).join('、')
        : '—']
    ];
    $('#tsList').innerHTML = tsRows.map(function (row) {
      return '<span class="k">' + U.esc(row[0]) + '</span><span class="v mono">' +
        U.esc(row[1]) + '</span>';
    }).join('');
    $('#tsHint').textContent = ts.up ? '两端同一账号' : (tsStuck ? '服务未就绪' : (ts.available ? '待登录' : '可选'));
    const canTs = S.is_root || S.demo;
    const tsInstall = $('#tsInstall');
    const tsLogin = $('#tsLogin');
    const tsRestart = $('#tsRestart');
    const tsCopy = $('#tsCopy');
    tsInstall.hidden = ts.available;
    tsLogin.hidden = !ts.available || !!ts.up;
    tsRestart.hidden = !(canTs && tsStuck);
    tsCopy.hidden = !ts.up;
    tsInstall.disabled = !canTs || busy;
    tsLogin.disabled = !canTs || busy || tsStuck;
    tsRestart.disabled = !canTs || busy;
    const tsError = String(ts.error || '');
    const tsPerm = /permission|denied|access|root/i.test(tsError);
    if (ts.up) {
      $('#tsNote').textContent = '两端登录同一 Tailscale 账号即可，不用改桥接或热点；控制端扫描会自动列出在线对端。';
    } else if (!canTs) {
      $('#tsNote').textContent = ts.available
        ? '登录 Tailscale 需要 root：先在「环境准备」里切换为 root。'
        : '安装 Tailscale 需要 root：先在「环境准备」里切换为 root。';
    } else if (!ts.available) {
      $('#tsNote').textContent = '跨网段、NAT 或校园网隔离时用：点上方「安装 Tailscale」，装完再点「登录」；两台机器登录同一账号，各得一个 100.x 地址。';
    } else if (tsPerm) {
      $('#tsNote').textContent = '读不到 Tailscale 状态（权限不足）：切换为 root 后再试，或执行 sudo tailscale set --operator=$USER。';
    } else if (tsStuck) {
      $('#tsNote').textContent = 'Tailscale 服务没有就绪（' + (ts.state || 'NoState') +
        '）：点「重启 Tailscale 服务」，以 root 重启后再点「登录」。';
    } else {
      $('#tsNote').textContent = '已安装但没登录：点上方「登录」，按提示在浏览器完成授权。' +
        (tsError ? '（' + tsError + '）' : '');
    }
    showAuth(ts.up ? '' : ts.auth_url);

    // 配对码
    const codeBox = $('#codeBox');
    if (S.code) {
      codeBox.innerHTML = String(S.code).split('').map(function (digit) {
        return '<span class="digit">' + U.esc(digit) + '</span>';
      }).join('');
    } else {
      codeBox.innerHTML = '<span class="hint">远程访问时不显示配对码，请在 Orin 本机查看。</span>';
    }
    $('#copyCode').disabled = !S.code;
    $('#refreshCode').disabled = !local;

    // 已配对设备
    const clients = S.clients || [];
    $('#clientCount').textContent = clients.length ? clients.length + ' 台' : '';
    $('#clientList').innerHTML = clients.length
      ? clients.map(function (client) {
          return '<div class="client"><div>' +
            '<strong>' + U.esc(client.name || '未命名') + '</strong>' +
            '<span class="fp">' + U.esc(client.fingerprint || '') + '</span>' +
            '<span class="meta">配对时间 ' + U.esc(client.paired_at || '—') + '</span>' +
            '</div>' +
            '<button class="btn ghost small" data-unpair="' + U.esc(client.id) + '">解除</button></div>';
        }).join('')
      : '<div class="empty">还没有设备配对。Windows 端输入上面的配对码即可。</div>';

    // 日志
    $('#logBox').textContent = (S.logs || []).map(function (entry) {
      return '[' + entry.t + '] ' + entry.m;
    }).join('\n') || '暂无日志';
    const logFile = $('#logFile');
    logFile.hidden = !S.log_path;
    logFile.textContent = S.log_path ? '同时写入：' + S.log_path : '';
    $('#hostName').textContent = S.hostname || '';
  }

  // ---- 事件 ----
  U.$$('input[name="method"]').forEach(function (radio) {
    radio.addEventListener('change', updateTiles);
  });

  $('#applyBtn').addEventListener('click', async function () {
    const method = currentMethod();
    const payload = { method: method };
    let summary = '';
    if (method === 'manual') {
      payload.connection = $('#connSelect').value;
      payload.address = $('#addrInput').value.trim();
      payload.gateway = $('#gwInput').value.trim();
      if (!payload.connection) {
        U.toast('没有可用的连接，先用 nmcli 建一个', 'bad');
        return;
      }
      if (!payload.address) {
        U.toast('填一个地址，例如 192.168.1.50/24', 'bad');
        return;
      }
      summary = '<p>将修改连接 <code>' + U.esc(payload.connection) + '</code> 的 IPv4：</p>' +
        '<p class="mono">' + U.esc(payload.address) +
        (payload.gateway ? ' · 网关 ' + U.esc(payload.gateway) : '') + '</p>';
    } else if (method === 'mdns') {
      summary = '<p>将确认 avahi 正在运行，并解析 <code>' +
        U.esc((S && S.mdns_name) || 'orin.local') + '</code>。不改 IP。</p>';
    } else {
      summary = '<p>只检测 <code>192.168.55.1</code> 是否已经存在，不改任何配置。</p>';
    }
    const ok = await U.confirm({ title: '应用连接方式', html: summary, okText: '应用' });
    if (!ok) return;

    busy = true;
    try {
      const data = await U.api('POST', '/api/network/apply', payload, authHeaders());
      U.toast(data.ok ? (data.message || '已应用') : (data.error || '应用失败'), data.ok ? 'ok' : 'bad');
    } finally {
      busy = false;
    }
    await refresh(true);
  });

  $('#revertBtn').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '还原网络',
      html: '<p>把上次应用静态 IP 的连接恢复成原配置。USB / mDNS 模式没有改 IP，不需要还原。</p>',
      okText: '还原',
      danger: true
    });
    if (!ok) return;
    busy = true;
    try {
      const data = await U.api('POST', '/api/network/revert', {}, authHeaders());
      U.toast(data.ok ? (data.message || '已还原') : (data.error || '还原失败'), data.ok ? 'ok' : 'bad');
    } finally {
      busy = false;
    }
    await refresh(true);
  });

  $('#fwBtn').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '放行 22 端口',
      html: '<p>如果 ufw 已启用，按本机当前地址推断网段，只放行这个网段访问 22/tcp。</p>',
      okText: '放行'
    });
    if (!ok) return;
    const data = await U.api('POST', '/api/firewall/allow22', {}, authHeaders());
    U.toast(data.ok ? (data.message || '已处理') : (data.error || '失败'), data.ok ? 'ok' : 'bad');
    await refresh(true);
  });

  $('#setupBtn').addEventListener('click', async function () {
    if (busy || restarting) return;
    const ok = await U.confirm({
      title: '安装 / 修复环境',
      html: '<p>会安装或修复 <code>openssh-server</code>、<code>tmux</code>、' +
        '<code>avahi-daemon</code> / <code>avahi-utils</code>；启用 ssh 与 avahi；' +
        '准备 <code>~/.ssh</code>（700/600）；把目标账号加入 dialout。</p>' +
        '<p>第一次需要联网下载软件包，可能要几分钟。</p>',
      okText: '开始'
    });
    if (!ok) return;
    busy = true;
    setupLog = '';
    $('#setupBtn').disabled = true;
    $('#envNote').textContent = '正在准备环境，安装软件包可能需要几分钟…';
    U.toast('正在准备环境，安装软件包可能需要几分钟…', 'ok');
    let data;
    try {
      data = await U.api('POST', '/api/setup', {}, authHeaders());
    } finally {
      busy = false;
    }
    const lines = (data.steps || []).map(function (step) {
      return (step.ok ? '✓ ' : '✗ ') + step.name + (step.message ? '：' + step.message : '');
    });
    setupLog = lines.join('\n') || (data.error || '没有返回任何步骤');
    U.toast(data.ok ? '环境已就绪' : (data.error || '有步骤失败，见环境准备里的说明'),
      data.ok ? 'ok' : 'bad');
    await refresh(true);
  });

  $('#rootBtn').addEventListener('click', async function () {
    if (restarting) return;
    const password = $('#rootPw').value;
    const button = $('#rootBtn');
    button.disabled = true;
    restarting = true;
    const data = await U.api('POST', '/api/relaunch-root', { password: password }, authHeaders());
    $('#rootPw').value = '';
    if (!data.ok) {
      restarting = false;
      button.disabled = false;
      U.toast(data.error || '切换失败', 'bad');
      return;
    }
    U.toast(data.message || '正在以 root 重启…', 'ok');
    // 等服务重新起来：先等它断掉，再等新实例响应
    const started = Date.now();
    const timer = setInterval(async function () {
      if (Date.now() - started < 2500) return;
      const ping = await U.api('GET', '/api/ping');
      if (ping && ping.ok) {
        clearInterval(timer);
        location.reload();
      } else if (Date.now() - started > 90000) {
        clearInterval(timer);
        restarting = false;
        button.disabled = false;
        U.toast('等待超时：如果程序已退出，请重新运行 ./start.sh', 'bad');
      }
    }, 1200);
  });

  $('#refreshCode').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '更换配对码',
      html: '<p>旧配对码立刻失效；已经配对的设备不受影响。</p>',
      okText: '更换'
    });
    if (!ok) return;
    const data = await U.api('POST', '/api/code/refresh', {}, authHeaders());
    if (data.ok) {
      if (S) S.code = data.code;
      U.toast('已更换配对码', 'ok');
      render();
    } else {
      U.toast(data.error || '更换失败', 'bad');
    }
  });

  $('#copyCode').addEventListener('click', async function () {
    if (!S || !S.code) return;
    const ok = await U.copy(S.code);
    U.toast(ok ? '已复制配对码' : '复制失败', ok ? 'ok' : 'bad');
  });

  $('#tsInstall').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '安装 Tailscale',
      html: '<p>会以 root 执行 Tailscale 官方安装脚本：从 <code>pkgs.tailscale.com</code> ' +
        '下载并安装 <code>tailscale</code> 包，然后启用 <code>tailscaled</code> 服务。</p>' +
        '<p>需要能访问外网，通常要一两分钟，期间页面会停在“安装中”。</p>',
      okText: '安装'
    });
    if (!ok) return;
    const button = $('#tsInstall');
    button.disabled = true;
    button.textContent = '安装中…';
    try {
      const data = await U.api('POST', '/api/tailscale/install', {}, authHeaders());
      if (data.ok) U.toast(data.already ? 'Tailscale 已经安装' : '安装完成，点「登录」继续', 'ok');
      else U.toast(data.error || '安装失败', 'bad');
    } finally {
      await refresh();
      button.textContent = '安装 Tailscale';
    }
  });

  $('#tsLogin').addEventListener('click', async function () {
    const button = $('#tsLogin');
    button.disabled = true;
    button.textContent = '等待授权…';
    try {
      const data = await U.api('POST', '/api/tailscale/up', {}, authHeaders());
      if (!data.ok) {
        U.toast(data.error || '登录失败', 'bad');
      } else if (data.url) {
        showAuth(data.url);
        U.toast('已生成授权链接，在浏览器打开并登录', 'ok');
      } else if (data.already) {
        U.toast('Tailscale 已经登录', 'ok');
      } else {
        U.toast(data.note || '已拉起登录，稍等几秒刷新', 'warn');
      }
    } finally {
      await refresh();
      button.textContent = '登录';
    }
  });

  $('#tsRestart').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '重启 Tailscale 服务',
      html: '<p>会以 root 执行 <code>systemctl restart tailscaled</code>。</p>' +
        '<p>用于 Tailscale 卡在 <code>NoState</code> / “starting” 的情况；重启后需要再点「登录」。</p>',
      okText: '重启'
    });
    if (!ok) return;
    const button = $('#tsRestart');
    button.disabled = true;
    button.textContent = '重启中…';
    try {
      const data = await U.api('POST', '/api/tailscale/restart', {}, authHeaders());
      if (!data.ok) U.toast(data.error || '重启失败', 'bad');
      else U.toast(data.message || data.note || '已请求重启，稍等几秒刷新', data.started ? 'warn' : 'ok');
    } finally {
      await refresh();
      button.textContent = '重启 Tailscale 服务';
      button.disabled = false;
    }
  });

  $('#tsCopy').addEventListener('click', async function () {
    const ts = (S && S.tailscale) || {};
    if (!ts.ip) return;
    const ok = await U.copy(ts.ip);
    U.toast(ok ? '已复制：' + ts.ip : '复制失败', ok ? 'ok' : 'bad');
  });

  $('#addrList').addEventListener('click', async function (event) {
    const button = event.target.closest('[data-copy]');
    if (!button) return;
    const ok = await U.copy(button.dataset.copy);
    U.toast(ok ? '已复制' : '复制失败', ok ? 'ok' : 'bad');
  });

  $('#clientList').addEventListener('click', async function (event) {
    const button = event.target.closest('[data-unpair]');
    if (!button) return;
    const client = (S && S.clients || []).find(function (item) {
      return item.id === button.dataset.unpair;
    });
    const ok = await U.confirm({
      title: '解除配对',
      html: '<p>删除 <strong>' + U.esc(client ? client.name : '这个设备') +
        '</strong> 在 <code>authorized_keys</code> 里的公钥。该设备下次连接会失败。</p>',
      okText: '解除',
      danger: true
    });
    if (!ok) return;
    const data = await U.api('POST', '/api/unpair', { client_id: button.dataset.unpair }, authHeaders());
    U.toast(data.ok ? '已解除配对' : (data.error || '解除失败'), data.ok ? 'ok' : 'bad');
    await refresh(true);
  });

  $('#gateForm').addEventListener('submit', async function (event) {
    event.preventDefault();
    const code = $('#gateCode').value.replace(/\D/g, '').slice(0, 6);
    if (code.length !== 6) {
      U.toast('配对码是 6 位数字', 'bad');
      return;
    }
    gateCode = code;
    const data = await U.api('GET', '/api/status', undefined, authHeaders());
    if (data._status === 401) {
      gateCode = '';
      U.toast('配对码不正确', 'bad');
      return;
    }
    sessionStorage.setItem('orinPairCode', code);
    $('#gate').close();
    if (data.ok) {
      S = data;
      render();
    }
  });

  setInterval(function () {
    if (document.querySelector('dialog[open]')) return;
    const active = document.activeElement;
    if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) return;
    refresh();
  }, 5000);

  refresh();
})();
