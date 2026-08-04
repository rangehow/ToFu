// Tofu Browser Bridge — Popup

document.addEventListener('DOMContentLoaded', () => {
  // The badge is DERIVED from the manifest — its hardcoded twin sat at
  // v4.3 through two version bumps and read as "you didn't update".
  const badge = document.getElementById('versionBadge');
  if (badge) badge.textContent = 'v' + chrome.runtime.getManifest().version;
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const serverInput = document.getElementById('serverUrl');
  const saveBtn = document.getElementById('saveBtn');
  const secretInput = document.getElementById('bridgeSecret');
  const saveSecretBtn = document.getElementById('saveSecretBtn');
  const toggleBtn = document.getElementById('toggleBtn');
  const statsDiv = document.getElementById('stats');
  const repairRow = document.getElementById('repairRow');
  const repairBtn = document.getElementById('repairBtn');

  // Load current secret state (we never echo the actual value into the
  // popup — only show whether one is set, like a password reset flow).
  chrome.storage.local.get(['bridgeSecret'], (data) => {
    if (data.bridgeSecret) {
      secretInput.placeholder = '••••••••  (configured — leave blank to keep)';
    }
  });

  function updateStatus() {
    chrome.runtime.sendMessage({ type: 'getStatus' }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        statusDot.className = 'status-dot disconnected';
        statusText.textContent = 'Service Worker inactive';
        return;
      }

      statusDot.className = resp.connected ? 'status-dot connected' : 'status-dot disconnected';
      statusText.textContent = resp.connected ? 'Connected' : (resp.lastError || 'Disconnected');

      // The repair row appears exactly when the background has declared the
      // credential dead — and disappears the moment the silent ladder heals
      // it. It never asks for a secret: the button runs the same automatic
      // ladder, only allowed to open a foreground Tofu tab (user gesture).
      if (repairRow) {
        repairRow.style.display = (!resp.connected && resp.needsRepair) ? '' : 'none';
        if (repairBtn && resp.repairBusy) {
          repairBtn.disabled = true;
          repairBtn.textContent = 'Re-pairing…';
        } else if (repairBtn && repairBtn.textContent === 'Re-pairing…') {
          repairBtn.disabled = false;
          repairBtn.textContent = 'Re-pair now';
        }
      }

      if (resp.serverUrl && serverInput) {
        serverInput.value = resp.serverUrl;
      }

      toggleBtn.textContent = resp.pollActive ? '⏸ Pause' : '▶ Resume';

      // Show client ID (first 12 chars for readability)
      const clientIdText = document.getElementById('clientIdText');
      if (clientIdText && resp.clientId) {
        clientIdText.textContent = resp.clientId.substring(0, 12) + '…';
        clientIdText.title = resp.clientId;  // Full ID on hover
      }

      // Stats
      if (statsDiv) {
        statsDiv.innerHTML = `
          <div>✓ ${resp.commandsExecuted || 0} executed</div>
          <div>✗ ${resp.commandsFailed || 0} failed</div>
          <div>📤 ${resp.resultQueue || 0} queued</div>
          <div>⏳ ${resp.inflight || 0} in-flight</div>
        `;
      }
    });
  }

  saveBtn.addEventListener('click', () => {
    const url = serverInput.value.trim();
    if (!url) return;
    chrome.runtime.sendMessage({ type: 'setServer', url }, () => {
      saveBtn.textContent = '✓ Saved';
      setTimeout(() => { saveBtn.textContent = 'Save'; }, 1500);
      setTimeout(updateStatus, 500);
    });
  });

  saveSecretBtn.addEventListener('click', () => {
    const secret = secretInput.value;
    chrome.runtime.sendMessage({ type: 'setBridgeSecret', secret }, () => {
      saveSecretBtn.textContent = '✓ Saved';
      secretInput.value = '';
      setTimeout(() => { saveSecretBtn.textContent = 'Save'; }, 1500);
      setTimeout(updateStatus, 500);
    });
  });

  if (repairBtn) {
    repairBtn.addEventListener('click', () => {
      repairBtn.disabled = true;
      repairBtn.textContent = 'Re-pairing…';
      chrome.runtime.sendMessage({ type: 'repairNow' }, (resp) => {
        repairBtn.disabled = false;
        repairBtn.textContent = (resp && resp.ok) ? '✓ Re-paired' : 'Re-pair now';
        setTimeout(updateStatus, 500);
      });
    });
  }

  toggleBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'toggle' }, () => {
      setTimeout(updateStatus, 300);
    });
  });

  updateStatus();
  setInterval(updateStatus, 2000);
});
