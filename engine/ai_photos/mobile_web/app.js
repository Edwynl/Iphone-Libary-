'use strict';
let token = '';
let photos = [];
let nextOffset = 0;
let loading = false;
let current = -1;
let prepared = null;  // The current original as a File, ready for the share sheet.
const el = id => document.getElementById(id);
async function api(url, options = {}) {
  const res = await fetch(url, { ...options, headers: { Authorization: 'Bearer ' + token, ...(options.headers || {}) } });
  if (!res.ok) { const msg = await res.json().catch(() => ({error: res.statusText})); throw new Error(msg.error || res.statusText); }
  return res;
}
function remember(value) {
  try { value ? localStorage.setItem('pairingToken', value) : localStorage.removeItem('pairingToken'); } catch (e) {}
}

// Library: pages of 60, newest first, loaded as the end of the grid scrolls into view.
async function loadMore() {
  if (loading || nextOffset === null) return;
  loading = true; el('more').textContent = 'Loading…';
  try {
    const data = await (await api('/api/photos?offset=' + nextOffset)).json();
    for (const p of data.photos) addCard(p, photos.push(p) - 1);
    nextOffset = data.next_offset;
    el('more').textContent = nextOffset === null ? `${photos.length.toLocaleString()} items` : '';
  } catch (e) { el('more').textContent = 'Could not load photos: ' + e.message; }
  finally { loading = false; }
}
function addCard(p, index) {
  const card = document.createElement('button'); card.className = 'photo'; card.setAttribute('aria-label', p.name);
  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  if (p.thumb) img.src = p.thumb;
  card.append(img);
  if (p.video || p.motion) {
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = p.video ? 'VIDEO' : 'LIVE';
    card.append(badge);
  }
  card.addEventListener('click', () => openViewer(index));
  el('grid').append(card);
}
function reload() {
  photos = []; nextOffset = 0; el('grid').replaceChildren();
  return loadMore();
}
new IntersectionObserver(entries => { if (entries[0].isIntersecting && !el('library').hidden) loadMore(); },
  { rootMargin: '600px' }).observe(el('more'));

// Viewer: the original over the whole screen, swipe or arrows to step through what has loaded.
function openViewer(index) {
  el('viewer').hidden = false; document.body.classList.add('viewing');
  show(index);
}
function closeViewer() {
  stopVideo(); el('viewer-image').removeAttribute('src');
  el('viewer').hidden = true; document.body.classList.remove('viewing');
  current = -1;
}
function stopVideo() {
  const video = el('viewer-video');
  video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
}
function show(index) {
  current = index; prepared = null;
  const p = photos[index];
  stopVideo();
  el('viewer-title').textContent = p.name;
  el('viewer-count').textContent = `${index + 1} / ${photos.length}${nextOffset === null ? '' : '+'}`;
  el('viewer-prev').disabled = index === 0;
  el('viewer-next').disabled = index === photos.length - 1 && nextOffset === null;
  el('viewer-live').hidden = !p.motion;
  el('viewer-save').textContent = 'Save to iPhone'; el('viewer-save').disabled = false;
  el('viewer-note').hidden = true;
  const img = el('viewer-image');
  if (p.video) {
    img.hidden = true; img.removeAttribute('src');
    play(p.original, false);
    return;
  }
  img.hidden = false;
  img.onerror = () => {
    // This browser cannot decode the original (HEIC outside Safari): keep the preview on screen.
    img.onerror = null;
    if (p.thumb) img.src = p.thumb;
    note('Preview shown: this browser cannot display the original. Saving still keeps the full original.');
  };
  img.src = p.original;
}
function play(url, once) {
  const video = el('viewer-video');
  video.src = url; video.hidden = false; video.controls = !once;
  video.onended = once ? stopVideo : null;
  video.play().catch(() => {});
}
function note(text) { el('viewer-note').textContent = text; el('viewer-note').hidden = false; }
async function step(delta) {
  const next = current + delta;
  if (next < 0) return;
  if (next >= photos.length) { await loadMore(); if (next >= photos.length) return; }
  show(next);
}

// Save: the share sheet's "Save Image" / "Save Video" puts the original in the Photos library.
// Without file sharing the browser downloads it instead.
function download(p) { location.href = p.original + '&download=1'; }
async function save() {
  const p = photos[current];
  const button = el('viewer-save');
  if (prepared) return share(prepared);
  if (!navigator.canShare) return download(p);
  button.disabled = true; button.textContent = 'Preparing…';
  try {
    const res = await fetch(p.original);
    if (!res.ok) throw new Error(res.statusText);
    const blob = await res.blob();
    if (photos[current] !== p) return;
    const file = new File([blob], p.name, { type: blob.type });
    if (!navigator.canShare({ files: [file] })) return download(p);
    prepared = file;
    await share(file);
  } catch (e) { note('Could not prepare the file: ' + e.message); }
  finally { if (photos[current] === p) button.disabled = false; }
}
async function share(file) {
  const button = el('viewer-save');
  try {
    await navigator.share({ files: [file] });
    button.textContent = 'Save to iPhone';
  } catch (e) {
    // Safari only opens the share sheet straight from a tap; a large file takes too long to arrive.
    if (e.name === 'NotAllowedError') button.textContent = 'Ready: tap to save';
    else if (e.name !== 'AbortError') note('Could not save: ' + e.message);
  }
}

let touchX = null;
el('stage').addEventListener('touchstart', e => { touchX = e.touches.length === 1 ? e.touches[0].clientX : null; }, { passive: true });
el('stage').addEventListener('touchend', e => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX; touchX = null;
  if (Math.abs(dx) > 60) step(dx < 0 ? 1 : -1);
});
el('viewer-close').addEventListener('click', closeViewer);
el('viewer-prev').addEventListener('click', () => step(-1));
el('viewer-next').addEventListener('click', () => step(1));
el('viewer-live').addEventListener('click', () => play(photos[current].motion, true));
el('viewer-save').addEventListener('click', save);
document.addEventListener('keydown', e => {
  if (el('viewer').hidden) return;
  if (e.key === 'Escape') closeViewer();
  else if (e.key === 'ArrowLeft') step(-1);
  else if (e.key === 'ArrowRight') step(1);
});

async function connect(value) {
  token = value;
  await api('/api/status');
  remember(token);
  el('pair').hidden = true; el('library').hidden = false;
  await reload();
}
el('connect').addEventListener('click', () => connect(el('token').value.trim())
  .catch(e => { remember(''); el('connection').textContent = 'Connection failed: ' + e.message; }));
el('refresh').addEventListener('click', () => reload());
el('upload').addEventListener('click', async () => {
  const files = Array.from(el('files').files || []);
  for (let i = 0; i < files.length; i++) {
    const file = files[i]; el('progress').textContent = `Uploading ${i + 1}/${files.length}: ${file.name}`;
    try {
      const result = await (await api('/api/upload', {method: 'POST', headers: {'X-Filename': encodeURIComponent(file.name), 'Content-Type': 'application/octet-stream'}, body: file})).json();
      el('progress').textContent = `${i + 1}/${files.length}: ${result.status}`;
    } catch (e) { el('progress').textContent = `Failed ${file.name}: ${e.message}`; break; }
  }
  await reload();
});

// The token is kept on this iPhone after a successful connection; a rejected one is forgotten.
try { const saved = localStorage.getItem('pairingToken'); if (saved) connect(saved).catch(() => remember('')); } catch (e) {}
