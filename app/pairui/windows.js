(function () {
  'use strict';

  const U = window.PairUI;
  const $ = U.$;
  const SOURCE_LABEL = { tailscale: 'Tailscale', usb: 'USB', mdns: 'mDNS', manual: '手动', demo: '演示' };

  let S = null;
  let results = [];
  let selected = null;
  let busy = false;

  function chip(text, kind) {
    return '<span class="chip ' + (kind || '') + '">' + U.esc(text) + '</span>';
  }

  function updatePairBtn() {
    const code = $('#codeInput').value.replace(/\D/g, '');
    $('#pairBtn').disabled = !selected || code.length !== 6 || busy;
  }

  function renderResults() {
    const box = $('#scanResults');
    if (!results.length) {
      box.innerHTML = '<div class="empty">没找到 Orin。检查 USB 线、局域网，或在上面的输入框手填地址再扫描；' +
        '两端装了 Tailscale 的话，扫描会自动列出在线对端。</div>';
      updatePairBtn();
      return;
    }
    box.innerHTML = results.map(function (item) {
      const key = item.host + ':' + item.port;
      const via = item.source ? ' · ' + (SOURCE_LABEL[item.source] || item.source) : '';
      return '<label class="result ' + (selected === key ? 'selected' : '') + '" data-key="' + U.esc(key) + '">' +
        '<input type="radio" name="orin" value="' + U.esc(key) + '"' + (selected === key ? ' checked' : '') + '>' +
        '<div><strong>' + U.esc(item.host) + ':' + item.port + '</strong>' +
        '<span>' + U.esc(item.hostname || 'Orin') + ' · ' + item.latency_ms + ' ms · v' +
        U.esc(item.version || '') + U.esc(via) + '</span></div></label>';
    }).join('');
    updatePairBtn();
  }

  function showTest(ok, output) {
    const box = $('#testResult');
    box.hidden = false;
    box.className = 'test-result ' + (ok ? 'ok' : 'bad');
    box.textContent = (ok ? '连接测试通过' : '连接测试失败') + (output ? '\n' + output : '');
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

  function render(wasPaired) {
    if (!S) return;

    const chips = [];
    if (S.demo) chips.push(chip('演示模式', 'info'));
    chips.push(chip(S.paired ? '已配对' : '未配对', S.paired ? 'ok' : ''));
    if (S.paired) {
      if (S.remote_state === 'valid') chips.push(chip('Orin 可达', 'ok'));
      else if (S.remote_state === 'unreachable') chips.push(chip('Orin 不可达', 'warn'));
      else if (S.remote_state === 'invalid') chips.push(chip('远端已解除', 'bad'));
    }
    $('#chips').innerHTML = chips.join('');

    $('#pairView').hidden = S.paired;
    $('#pairedView').hidden = !S.paired;
    $('#remoteBanner').hidden = !(S.paired && S.remote_state === 'invalid');

    const envRows = [
      ['SSH 工具', S.ssh_found ? S.ssh_path : '未找到（点下面按钮安装，或装 Git for Windows）'],
      ['私钥', S.key_path + (S.key_exists ? '' : '（不存在，配对时生成）')],
      ['SSH 配置', S.ssh_config + (S.managed_block ? '（已管理）' : '')],
      ['Tailscale', S.tailscale && S.tailscale.available
        ? (S.tailscale.up ? '已连接 · ' + (S.tailscale.ip || '') : '已安装，未连接')
        : '未安装（可选）'],
      ['版本', 'v' + S.version]
    ];
    $('#envInfo').innerHTML = envRows.map(function (row) {
      return '<span class="k">' + U.esc(row[0]) + '</span><span class="v mono">' +
        U.esc(row[1] || '—') + '</span>';
    }).join('');
    $('#envHint').textContent = S.demo ? '不改真实系统' : '';

    const tsState = S.tailscale || {};
    const tsInstall = $('#tsInstall');
    const tsLogin = $('#tsLogin');
    const tsRestart = $('#tsRestart');
    const tsNote = $('#tsInstallNote');
    const tsHealth = (tsState.health || []).join(' ');
    const tsStuck = !!tsState.available && !tsState.up &&
      (String(tsState.state || '') === 'NoState' || /starting|no state/i.test(tsHealth));
    const sshInstall = $('#sshInstall');
    const sshNote = $('#sshNote');
    sshInstall.hidden = !!S.ssh_found;
    sshNote.hidden = !!S.ssh_found;
    sshNote.textContent = '没找到 ssh.exe：点「安装 SSH 客户端」会用 Windows 可选功能装 OpenSSH Client，' +
      '需要在 UAC 窗口点“是”。系统里没有这个功能时，也可以自己装 Git for Windows，程序会自动用它自带的 ssh。';
    tsInstall.hidden = !!(tsState.up || tsState.available);
    tsLogin.hidden = !tsState.available || !!tsState.up;
    tsLogin.disabled = tsStuck;
    tsRestart.hidden = !tsStuck;
    tsNote.hidden = !!tsState.up;
    if (tsStuck) {
      tsNote.textContent = 'Tailscale 服务没有就绪（' + (tsState.state || 'NoState') +
        '）：点「重启 Tailscale 服务」，在 UAC 里确认后再点「登录」。';
    } else if (!tsState.up) {
      tsNote.textContent = tsState.available
        ? '已安装但没登录：点上方「登录」，会打开浏览器让你登录同一 Tailscale 账号。'
        : '点「安装 Tailscale」会调用 winget 并弹出 UAC；装完后页面刷新会显示登录按钮，扫描也能自动列出 100.x 对端。';
    }
    showAuth(tsState.up ? '' : tsState.auth_url);

    if (S.paired) {
      const info = [
        ['Orin', S.host + ':' + S.port],
        ['SSH 用户', S.user],
        ['别名', 'orin'],
        ['指纹', S.fingerprint || '—'],
        ['配对时间', S.paired_at || '—'],
        ['密钥', S.key_path],
        ['SSH 配置', S.ssh_config],
        ['最近测试', S.last_test ? (S.last_test.ok ? '通过' : '失败') + ' · ' + S.last_test.at : '还没测过']
      ];
      $('#pairInfo').innerHTML = info.map(function (row) {
        const mono = row[0] === '指纹' || row[0] === '密钥' || row[0] === 'SSH 配置';
        return '<span class="k">' + U.esc(row[0]) + '</span><span class="v' +
          (mono ? ' mono' : '') + '">' + U.esc(row[1] || '—') + '</span>';
      }).join('');

      const pairChip = $('#pairChip');
      if (S.remote_state === 'invalid') {
        pairChip.textContent = '远端已解除';
        pairChip.className = 'tag bad';
      } else if (S.remote_state === 'unreachable') {
        pairChip.textContent = 'Orin 不可达';
        pairChip.className = 'tag warn';
      } else {
        pairChip.textContent = '已配对';
        pairChip.className = 'tag ok';
      }

      $('#sshPreview').textContent = S.ssh_block || '（没有找到 orin-pair 管理的配置段）';
      if (S.last_test) showTest(S.last_test.ok, S.last_test.output);
      else $('#testResult').hidden = true;
    } else {
      const ts = S.tailscale || {};
      $('#scanHint').textContent = S.demo
        ? '演示模式会尝试 127.0.0.1'
        : (ts.available
          ? (ts.up ? 'USB 直连、局域网或 Tailscale' : 'Tailscale 已安装但未连接')
          : 'USB 直连或同一局域网；装 Tailscale 可自动发现远程');
      $('#keyHint').textContent = S.key_path;
      $('#configHint').textContent = S.ssh_config;
      if (wasPaired) {
        results = [];
        selected = null;
        renderResults();
      }
    }

    $('#logBox').textContent = (S.logs || []).map(function (entry) {
      return '[' + entry.t + '] ' + entry.m;
    }).join('\n') || '暂无日志';
    const logFile = $('#logFile');
    logFile.hidden = !S.log_path;
    logFile.textContent = S.log_path ? '同时写入：' + S.log_path : '';
  }

  async function refresh() {
    const data = await U.api('GET', '/api/status');
    if (!data.ok) {
      U.toast(data.error || '读取状态失败', 'bad');
      return;
    }
    const wasPaired = S ? S.paired : false;
    S = data;
    render(wasPaired);
  }

  async function remoteCheck() {
    if (!S || !S.paired) return;
    if (document.querySelector('dialog[open]')) return;
    const data = await U.api('POST', '/api/remote_check', {});
    if (!data.ok) return;
    S.remote_state = data.state;
    render(false);
  }

  async function scan() {
    if (busy) return;
    busy = true;
    const button = $('#scanBtn');
    const label = button.textContent;
    button.disabled = true;
    button.textContent = '扫描中…';
    try {
      const data = await U.api('POST', '/api/scan', { host: $('#manualHost').value.trim() });
      if (!data.ok) {
        U.toast(data.error || '扫描失败', 'bad');
        return;
      }
      results = data.results || [];
      selected = results.length ? results[0].host + ':' + results[0].port : null;
      renderResults();
      if (results.length) U.toast('找到 ' + results.length + ' 个 Orin', 'ok');
      else U.toast('没找到 Orin。USB 直连看 192.168.55.1，局域网可手填地址。', 'warn');
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = label;
      updatePairBtn();
    }
  }

  async function pair() {
    const code = $('#codeInput').value.replace(/\D/g, '');
    if (!selected) {
      U.toast('先选择一个 Orin', 'bad');
      return;
    }
    if (code.length !== 6) {
      U.toast('配对码是 6 位数字', 'bad');
      return;
    }
    busy = true;
    const button = $('#pairBtn');
    button.disabled = true;
    button.textContent = '配对中…';
    try {
      const data = await U.api('POST', '/api/pair', { host: selected, code: code });
      if (!data.ok) {
        U.toast(data.error || '配对失败', 'bad');
        return;
      }
      U.toast(data.test_ok ? '配对成功，SSH 已通' : '已配对，但 SSH 测试未通过', data.test_ok ? 'ok' : 'warn');
      await refresh();
    } finally {
      busy = false;
      button.textContent = '完成配对';
      updatePairBtn();
    }
  }

  // ---- 事件 ----
  $('#scanBtn').addEventListener('click', scan);
  $('#manualHost').addEventListener('keydown', function (event) {
    if (event.key === 'Enter') scan();
  });

  $('#scanResults').addEventListener('click', function (event) {
    const item = event.target.closest('.result');
    if (!item) return;
    selected = item.dataset.key;
    U.$$('.result').forEach(function (node) {
      node.classList.toggle('selected', node === item);
    });
    const radio = item.querySelector('input');
    if (radio) radio.checked = true;
    updatePairBtn();
  });

  $('#codeInput').addEventListener('input', function (event) {
    event.target.value = event.target.value.replace(/\D/g, '').slice(0, 6);
    updatePairBtn();
  });
  $('#codeInput').addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !$('#pairBtn').disabled) pair();
  });

  $('#pairBtn').addEventListener('click', pair);

  $('#testBtn').addEventListener('click', async function () {
    const button = $('#testBtn');
    button.disabled = true;
    button.textContent = '测试中…';
    try {
      const data = await U.api('POST', '/api/test', {});
      if (data.ok) {
        showTest(data.test_ok, data.output);
        U.toast(data.test_ok ? 'SSH 连接正常' : 'SSH 连接失败', data.test_ok ? 'ok' : 'bad');
      } else {
        U.toast(data.error || '测试失败', 'bad');
      }
      await refresh();
    } finally {
      button.disabled = false;
      button.textContent = '测试连接';
    }
  });

  $('#hostkeyBtn').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '更新主机指纹',
      html: '<p>会删掉本地 <code>known_hosts</code> 里记录的 <code>orin</code> 主机指纹，' +
        '再连一次让 ssh 重新记录 Orin 当前的指纹。</p>' +
        '<p>用于 Orin 重装系统或换机器后提示“主机指纹变了”。如果现在连不上 Orin，' +
        '旧指纹也会被清掉，等 Orin 恢复后再点一次即可。</p>',
      okText: '更新'
    });
    if (!ok) return;
    const button = $('#hostkeyBtn');
    button.disabled = true;
    try {
      const data = await U.api('POST', '/api/hostkey/refresh', {});
      if (data.ok) {
        U.toast(data.fingerprint
          ? '已更新指纹：' + data.fingerprint
          : (data.message || '已更新指纹'), 'ok');
      } else {
        U.toast(data.error || '更新失败', 'bad');
      }
      await refresh();
    } finally {
      button.disabled = false;
    }
  });

  $('#unpairBtn').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '解除配对',
      html: '<p>会做三件事：</p><ul>' +
        '<li>通知 Orin 删除这对公钥</li>' +
        '<li>从 <code>' + U.esc(S.ssh_config) + '</code> 移除 <code>Host orin</code> 段</li>' +
        '<li>清空本地配对状态</li></ul>' +
        '<p>私钥文件会保留。</p>',
      okText: '解除',
      danger: true
    });
    if (!ok) return;
    const button = $('#unpairBtn');
    button.disabled = true;
    button.textContent = '解除中…';
    try {
      const data = await U.api('POST', '/api/unpair', {});
      if (data.ok) {
        U.toast(data.remote_ok ? '已解除配对' : '本地已清理，远端没有响应', data.remote_ok ? 'ok' : 'warn');
        await refresh();
      } else {
        U.toast(data.error || '解除失败', 'bad');
      }
    } finally {
      button.disabled = false;
      button.textContent = '解除配对';
    }
  });

  $('#cleanupBtn').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '清理本地配置',
      html: '<p>远端已经解除了这条配对。删除本地 <code>Host orin</code> 段和状态文件？私钥会保留。</p>',
      okText: '清理',
      danger: true
    });
    if (!ok) return;
    const data = await U.api('POST', '/api/cleanup', {});
    U.toast(data.ok ? '已清理本地配置' : (data.error || '清理失败'), data.ok ? 'ok' : 'bad');
    await refresh();
  });

  $('#copySsh').addEventListener('click', async function () {
    const cmd = (S && S.ssh_config) ? ('ssh -F "' + S.ssh_config + '" orin') : 'ssh orin';
    const ok = await U.copy(cmd);
    U.toast(ok ? '已复制：' + cmd : '复制失败', ok ? 'ok' : 'bad');
  });

  $('#sshInstall').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '安装 SSH 客户端',
      html: '<p>会通过 Windows 可选功能安装 <code>OpenSSH.Client</code>，并弹出 UAC 窗口，需要你点“是”。</p>' +
        '<p>装好后「运行环境」会显示 <code>ssh.exe</code> 的路径；如果系统里没有这个可选功能，改用 Git for Windows 也可以。</p>',
      okText: '安装'
    });
    if (!ok) return;
    const button = $('#sshInstall');
    button.disabled = true;
    button.textContent = '安装中…';
    try {
      const data = await U.api('POST', '/api/ssh/install', {});
      if (data.ok) {
        U.toast(data.already ? 'SSH 客户端已经可用'
          : (data.output ? 'SSH 客户端已安装' : (data.note || '安装已启动')), 'ok');
      } else {
        U.toast(data.error || '安装失败', 'bad');
      }
    } finally {
      await refresh();
      button.textContent = '安装 SSH 客户端';
      button.disabled = false;
    }
  });

  $('#tsInstall').addEventListener('click', async function () {
    const ok = await U.confirm({
      title: '安装 Tailscale',
      html: '<p>会用 winget 安装 Tailscale，并弹出 UAC 窗口，需要你点“是”。</p>' +
        '<p>安装完成后，页面刷新就会出现「登录」按钮；没有 winget 时会提示改用安装包。</p>',
      okText: '安装'
    });
    if (!ok) return;
    const button = $('#tsInstall');
    button.disabled = true;
    button.textContent = '安装中…';
    try {
      const data = await U.api('POST', '/api/tailscale/install', {});
      if (data.ok) U.toast(data.already ? 'Tailscale 已经安装' : '安装已启动，请在 UAC 窗口确认', 'ok');
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
      const data = await U.api('POST', '/api/tailscale/up', {});
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
      html: '<p>会先尝试拉起 Tailscale 托盘程序；如果服务仍卡在 <code>NoState</code>，再以管理员权限执行 <code>Restart-Service Tailscale -Force</code>（会弹 UAC）。</p>' +
        '<p>用于 Tailscale 卡在 <code>NoState</code> / “starting” 的情况；恢复后需要再点「登录」。</p>',
      okText: '重启'
    });
    if (!ok) return;
    const button = $('#tsRestart');
    button.disabled = true;
    button.textContent = '重启中…';
    try {
      const data = await U.api('POST', '/api/tailscale/restart', {});
      if (!data.ok) U.toast(data.error || '重启失败', 'bad');
      else U.toast(data.message || data.note || '已请求重启，稍等几秒刷新', data.started ? 'warn' : 'ok');
    } finally {
      await refresh();
      button.textContent = '重启 Tailscale 服务';
      button.disabled = false;
    }
  });

  setInterval(function () {
    if (document.querySelector('dialog[open]')) return;
    const active = document.activeElement;
    if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) return;
    refresh();
  }, 6000);

  setInterval(remoteCheck, 12000);

  refresh().then(remoteCheck);
})();
