'use strict';
let token = '';
const el = id => document.getElementById(id);
async function api(url, options = {}) {
  const res = await fetch(url, { ...options, headers: { Authorization: 'Bearer ' + token, ...(options.headers || {}) } });
  if (!res.ok) { const msg = await res.json().catch(() => ({error: res.statusText})); throw new Error(msg.error || res.statusText); }
  return res;
}
async function loadPhotos() {
  const data = await (await api('/api/photos')).json();
  const grid = el('grid'); grid.replaceChildren();
  for (const p of data.photos) {
    const card = document.createElement('div'); card.className = 'photo';
    const img = document.createElement('img'); img.alt = p.name;
    img.loading = 'lazy';
    if (p.has_thumb) {
      api('/api/thumb?id=' + encodeURIComponent(p.id))
        .then(r => r.blob()).then(blob => {const url = URL.createObjectURL(blob); img.onload = () => URL.revokeObjectURL(url); img.src = url;})
        .catch(() => {});
    }
    const text = document.createElement('span'); text.textContent = p.name;
    card.append(img, text); grid.append(card);
  }
}
el('connect').addEventListener('click', async () => {
  token = el('token').value.trim();
  try { await api('/api/status'); el('pair').hidden = true; el('library').hidden = false; await loadPhotos(); }
  catch (e) { el('connection').textContent = 'Connection failed: ' + e.message; }
});
el('refresh').addEventListener('click', () => loadPhotos().catch(e => el('progress').textContent = e.message));
el('upload').addEventListener('click', async () => {
  const files = Array.from(el('files').files || []);
  for (let i = 0; i < files.length; i++) {
    const file = files[i]; el('progress').textContent = `Uploading ${i + 1}/${files.length}: ${file.name}`;
    try {
      const result = await (await api('/api/upload', {method: 'POST', headers: {'X-Filename': encodeURIComponent(file.name), 'Content-Type': 'application/octet-stream'}, body: file})).json();
      el('progress').textContent = `${i + 1}/${files.length}: ${result.status}`;
    } catch (e) { el('progress').textContent = `Failed ${file.name}: ${e.message}`; break; }
  }
  await loadPhotos().catch(() => {});
});
