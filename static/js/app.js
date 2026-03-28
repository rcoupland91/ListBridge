/* ListBridge frontend — Socket.IO real-time updates */

(function () {
  'use strict';

  const socket = io({ transports: ['websocket', 'polling'] });

  socket.on('connect', () => {
    console.log('ListBridge socket connected');
  });

  // ── Live activity log ───────────────────────────────────────────────────────

  const logContainer = document.getElementById('log-entries');

  socket.on('sync_log', (data) => {
    if (!logContainer) return;

    const entry = document.createElement('div');
    entry.className = 'log-entry px-3 py-1 border-bottom border-secondary small d-flex gap-2 align-items-start';

    const ts = (data.ts || new Date().toISOString()).substring(0, 19).replace('T', ' ');
    const badgeClass = eventBadgeClass(data.event_type);
    const plName = data.playlist_name ? `<strong>${data.playlist_name}</strong>: ` : '';

    entry.innerHTML = `
      <span class="text-muted text-nowrap" style="font-size:.75rem">${ts}</span>
      <span class="badge ${badgeClass} text-nowrap">${data.event_type || ''}</span>
      <span class="text-truncate">${plName}${data.message || ''}</span>
    `;

    logContainer.prepend(entry);

    // Keep max 200 entries
    while (logContainer.children.length > 200) {
      logContainer.removeChild(logContainer.lastChild);
    }
  });

  // ── Playlist card updates ───────────────────────────────────────────────────

  socket.on('playlist_updated', (data) => {
    const id = data.playlist_id;
    const card = document.getElementById(`card-${id}`);
    if (card) {
      card.classList.add('syncing');
      setTimeout(() => card.classList.remove('syncing'), 2000);
    }
    // If on dashboard, flash the row
    const row = document.getElementById(`pl-row-${id}`);
    if (row) {
      row.classList.add('table-success');
      setTimeout(() => row.classList.remove('table-success'), 2000);
    }
  });

  // ── Status badge polling ────────────────────────────────────────────────────

  function refreshStatus() {
    fetch('/api/status')
      .then(r => r.json())
      .then(d => {
        setStatusBadge('badge-plex', d.plex_connected, 'Plex');
        setStatusBadge('badge-navi', d.navidrome_connected, 'Navidrome');
      })
      .catch(() => {});
  }

  function setStatusBadge(id, ok, label) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `badge rounded-pill ${ok ? 'bg-success' : 'bg-danger'}`;
    el.innerHTML = `<i class="bi bi-${id.includes('plex') ? 'server' : 'music-player'}"></i> ${label} ${ok ? 'OK' : 'Offline'}`;
  }

  setInterval(refreshStatus, 30000);

  // ── Helpers ─────────────────────────────────────────────────────────────────

  function eventBadgeClass(eventType) {
    const map = {
      sync_start: 'bg-primary',
      sync_done: 'bg-success',
      track_added: 'bg-info text-dark',
      track_matched: 'bg-success',
      track_not_found: 'bg-warning text-dark',
      error: 'bg-danger',
      file_changed: 'bg-purple',
      plex_change_detected: 'bg-warning text-dark',
      sync_info: 'bg-secondary',
    };
    return map[eventType] || 'bg-secondary';
  }

})();
