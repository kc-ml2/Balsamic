/* The same dependency-free review client runs on the workspace and from a file. */
(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('report-config').textContent);
  const $ = id => document.getElementById(id);
  const content = $('report-content');
  const originalBody = content.innerHTML;
  const originalHead = document.head.cloneNode(true);
  originalHead.querySelector('#report-review-style')?.remove();
  const online = Boolean(config.workspace_id && /^https?:$/.test(location.protocol));
  const key = `report-review-v1:${config.workspace_id || config.review_copy_id || 'portable'}:${config.id}:${config.source_hash}`;
  const clone = value => JSON.parse(JSON.stringify(value));
  const uid = () => 'mark_' + Array.from(crypto.getRandomValues(new Uint8Array(12)), n => n.toString(16).padStart(2, '0')).join('');
  const fields = ['id', 'section_id', 'start', 'end', 'exact', 'tier', 'note'];
  const cleanMark = mark => Object.fromEntries(fields.map(field => [field, mark[field] ?? (field === 'note' ? '' : undefined)]));
  const fromFeedback = feedback => ({ annotations: (feedback?.annotations || []).map(cleanMark), comment: feedback?.comment || '', focus: feedback?.focus ?? config.revision?.editorial?.focus ?? '', expected_submission_id: feedback?.submission_id || feedback?.expected_submission_id || null });
  let state = fromFeedback(config.feedback);
  let submitted = config.feedback?.submission_id ? clone(state) : null;
  let lastSubmission = config.feedback?.submission_id || null;
  let pending = [], active = null, undo = [], busy = false, pendingSubmission = null, alertTimer, storageFailed = false;
  let noteTarget = null, writerTimer = null;
  const tierNames = { good: 'Very good · keep', fine: 'Fine · remark', poor: 'Poor · improve', bad: 'Bad · remove' };
  const sectionElements = [...content.querySelectorAll('section[id]')];
  const excluded = 'svg,script,style,input,button,select,textarea,label,nav';

  function nodes(section) {
    const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT, { acceptNode(node) {
      return node.parentElement.closest(excluded) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    }});
    const result = []; let node, offset = 0;
    while ((node = walker.nextNode())) { result.push({ node, start: offset, end: offset + node.data.length }); offset += node.data.length; }
    return result;
  }
  const texts = Object.fromEntries(sectionElements.map(section => [section.id, nodes(section).map(entry => entry.node.data).join('')]));
  const fingerprint = value => JSON.stringify({ annotations: value.annotations.map(cleanMark).sort((a, b) => a.id.localeCompare(b.id)), comment: value.comment, focus: value.focus || '' });
  const dirty = () => !submitted || fingerprint(state) !== fingerprint(submitted);

  function alert(message) {
    $('review-alert').textContent = message; $('review-alert').hidden = false;
    clearTimeout(alertTimer); alertTimer = setTimeout(() => { $('review-alert').hidden = true; }, 12000);
  }
  function validate(value) {
    if (!value || !Array.isArray(value.annotations) || value.annotations.length > 1000 || typeof value.comment !== 'string' || value.comment.length > 20000) throw Error('This is not a valid review.');
    if (value.focus !== undefined && (typeof value.focus !== 'string' || value.focus.length > 4000)) throw Error('Writing focus must be at most 4000 characters.');
    const ids = new Set(), spans = {};
    for (const mark of value.annotations) {
      if (!/^[a-zA-Z0-9_-]{1,100}$/.test(mark.id) || ids.has(mark.id) || !Object.hasOwn(tierNames, mark.tier) || typeof mark.note !== 'string' || mark.note.length > 4000 || !Number.isInteger(mark.start) || !Number.isInteger(mark.end) || mark.start < 0 || mark.end <= mark.start || !Object.hasOwn(texts, mark.section_id) || texts[mark.section_id].slice(mark.start, mark.end) !== mark.exact || mark.end > texts[mark.section_id].length) throw Error('A highlight does not match this report. Nothing was imported.');
      ids.add(mark.id);
      spans[mark.section_id] ||= [];
      if (spans[mark.section_id].some(other => other.start < mark.end && mark.start < other.end)) throw Error('This review has overlapping marks. Resolve them in the original review first.');
      spans[mark.section_id].push(mark);
    }
  }
  function checkpoint() { undo.push(clone(state)); if (undo.length > 60) undo.shift(); }
  function save() {
    pendingSubmission = null;
    try { localStorage.setItem(key, JSON.stringify({ schema_version: 1, report_id: config.id, source_hash: config.source_hash, state })); storageFailed = false; }
    catch { storageFailed = true; }
    updateStatus();
  }
  function updateStatus() {
    $('review-save-status').textContent = storageFailed ? 'Browser storage unavailable — download your review to keep it.' : dirty() ? 'Draft saved in this browser · not submitted' : 'Submitted review · saved in this browser';
    if (!online && !storageFailed) $('review-save-status').textContent = 'Saved in this browser · download to send your changes';
    $('review-undo').disabled = !undo.length;
    $('review-revise').hidden = !online || !lastSubmission;
    $('review-revise').disabled = busy || dirty();
    $('review-submit').disabled = busy;
  }
  function selected() {
    const selection = window.getSelection();
    if (!selection?.rangeCount || selection.isCollapsed) return [];
    const range = selection.getRangeAt(0);
    if (!content.contains(range.startContainer) || !content.contains(range.endContainer)) return [];
    const spans = [];
    for (const section of sectionElements) {
      const intersections = nodes(section).filter(entry => range.intersectsNode(entry.node)).map(entry => ({
        start: entry.start + (range.startContainer === entry.node ? range.startOffset : 0),
        end: entry.start + (range.endContainer === entry.node ? range.endOffset : entry.node.data.length),
      })).filter(entry => entry.end > entry.start);
      if (!intersections.length) continue;
      const start = intersections[0].start, end = intersections.at(-1).end;
      if (texts[section.id].slice(start, end).trim()) spans.push({ section_id: section.id, start, end });
    }
    return spans;
  }
  function selectionStatus() {
    const count = pending.reduce((sum, span) => sum + span.end - span.start, 0);
    $('review-selection').textContent = count ? `${count} characters selected` : 'No text selected';
  }
  document.addEventListener('selectionchange', () => {
    const spans = selected();
    if (spans.length) { pending = spans; selectionStatus(); }
    else if (document.activeElement === document.body || content.contains(document.activeElement)) {
      pending = []; selectionStatus();
    }
  });
  $('review-tools').addEventListener('pointerdown', event => { if (event.target.closest('button')) event.preventDefault(); });
  function markFor(span, tier, note = '', id = uid()) { return { ...span, id, tier, note, exact: texts[span.section_id].slice(span.start, span.end) }; }
  function replaceSpan(span, tier, note, commentOnly = false) {
    const old = state.annotations.filter(mark => mark.section_id === span.section_id && mark.start < span.end && span.start < mark.end);
    state.annotations = state.annotations.filter(mark => !old.includes(mark));
    for (const mark of old) {
      if (mark.start < span.start) state.annotations.push(markFor({ section_id: mark.section_id, start: mark.start, end: span.start }, mark.tier, mark.note));
      if (span.end < mark.end) state.annotations.push(markFor({ section_id: mark.section_id, start: span.end, end: mark.end }, mark.tier, mark.note));
    }
    if (commentOnly) {
      const boundaries = [...new Set([span.start, span.end, ...old.flatMap(mark => [Math.max(span.start, mark.start), Math.min(span.end, mark.end)])])].sort((a, b) => a - b);
      boundaries.slice(0, -1).forEach((start, i) => {
        const end = boundaries[i + 1], prior = old.find(mark => mark.start <= start && end <= mark.end);
        state.annotations.push(markFor({ ...span, start, end }, prior?.tier || 'fine', note));
      });
    } else {
      const remarks = note ?? [...new Set(old.map(mark => mark.note).filter(Boolean))].join('\n');
      if (tier !== 'fine' || remarks) state.annotations.push(markFor(span, tier, remarks));
    }
  }
  function applyTier(tier, note, commentOnly = false) {
    if (!pending.length) { alert('Select words in the report first.'); return; }
    checkpoint();
    for (const span of pending) replaceSpan(span, tier, note, commentOnly);
    pending = []; active = null; window.getSelection()?.removeAllRanges();
    save(); render(); selectionStatus();
  }
  document.querySelectorAll('[data-tier]').forEach(button => { if (button.tagName === 'BUTTON') button.addEventListener('click', () => applyTier(button.dataset.tier)); });
  document.addEventListener('keydown', event => {
    if (event.target.closest('textarea,input,select,dialog,[contenteditable]') || event.ctrlKey || event.metaKey || event.altKey) return;
    if (pending.length && ['1', '2', '3', '4'].includes(event.key)) { event.preventDefault(); applyTier(['good', 'fine', 'poor', 'bad'][Number(event.key) - 1]); }
    if (event.key === 'Escape') { pending = []; window.getSelection()?.removeAllRanges(); selectionStatus(); }
  });
  function render() {
    content.querySelectorAll('mark[data-review-id]').forEach(mark => mark.replaceWith(...mark.childNodes));
    sectionElements.forEach(section => {
      section.normalize();
      const marks = state.annotations.filter(mark => mark.section_id === section.id).sort((a, b) => a.start - b.start);
      for (const entry of nodes(section)) {
        const intersections = marks.filter(mark => mark.start < entry.end && entry.start < mark.end);
        if (!intersections.length) continue;
        const fragment = document.createDocumentFragment(); let offset = 0;
        for (const mark of intersections) {
          const start = Math.max(0, mark.start - entry.start), end = Math.min(entry.node.data.length, mark.end - entry.start);
          fragment.append(document.createTextNode(entry.node.data.slice(offset, start)));
          const element = document.createElement('mark'); element.dataset.reviewId = mark.id; element.dataset.tier = mark.tier;
          element.title = tierNames[mark.tier] + (mark.note ? ': ' + mark.note : ''); element.textContent = entry.node.data.slice(start, end);
          if (mark.id === active) element.classList.add('review-active');
          fragment.append(element); offset = end;
        }
        fragment.append(document.createTextNode(entry.node.data.slice(offset))); entry.node.replaceWith(fragment);
      }
    });
    renderCards(); updateStatus();
  }
  function button(label, handler) { const element = document.createElement('button'); element.textContent = label; element.addEventListener('click', handler); return element; }
  function focusMark(mark) {
    active = mark.id;
    const element = [...content.querySelectorAll('mark[data-review-id]')].find(item => item.dataset.reviewId === mark.id);
    element?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    content.querySelectorAll('mark[data-review-id]').forEach(item => item.classList.toggle('review-active', item.dataset.reviewId === mark.id));
    renderCards();
  }
  function renderCards() {
    $('review-count').textContent = String(state.annotations.length);
    $('review-totals').textContent = ['good', 'poor', 'bad'].map(tier => `${state.annotations.filter(mark => mark.tier === tier).length} ${tier === 'good' ? 'green' : tier === 'poor' ? 'yellow' : 'red'}`).join(' · ');
    const list = $('review-annotations'); list.replaceChildren();
    if (!state.annotations.length) { const p = document.createElement('p'); p.textContent = 'No marks yet. Select any passage to start.'; list.append(p); }
    const order = sectionElements.map(section => section.id);
    [...state.annotations].sort((a, b) => order.indexOf(a.section_id) - order.indexOf(b.section_id) || a.start - b.start).forEach(mark => {
      const card = document.createElement('article'); card.className = 'review-card' + (active === mark.id ? ' is-active' : ''); card.dataset.tier = mark.tier;
      const title = document.createElement('div'); title.className = 'review-card-title'; title.textContent = tierNames[mark.tier];
      const quote = document.createElement('blockquote'); quote.textContent = mark.exact;
      const note = document.createElement('p'); note.className = 'review-remark'; note.textContent = mark.note;
      const actions = document.createElement('div'); actions.className = 'review-card-actions';
      actions.append(button('Find', () => focusMark(mark)), button(mark.note ? 'Edit remark' : 'Add remark', () => openNote(mark)), button('Clear', () => { checkpoint(); state.annotations = state.annotations.filter(item => item.id !== mark.id); save(); render(); }));
      card.append(title, quote); if (mark.note) card.append(note); card.append(actions); list.append(card);
    });
  }
  content.addEventListener('click', event => {
    if (selected().length) return;
    const id = event.target.closest('mark[data-review-id]')?.dataset.reviewId;
    if (id) { const mark = state.annotations.find(item => item.id === id); active = id; pending = [{ section_id: mark.section_id, start: mark.start, end: mark.end }]; selectionStatus(); renderCards(); }
  });
  function openNote(mark) {
    if (!mark && !pending.length) { alert('Select a passage or use Add remark on a marked passage.'); return; }
    noteTarget = mark ? { id: mark.id } : { spans: clone(pending) };
    $('review-note-quote').textContent = mark?.exact || pending.map(span => texts[span.section_id].slice(span.start, span.end)).join('\n');
    $('review-note-text').value = mark?.note || ''; $('review-note-dialog').showModal(); $('review-note-text').focus();
  }
  $('review-note').addEventListener('click', () => openNote(null));
  $('review-note-dialog').querySelector('form').addEventListener('submit', event => {
    if (event.submitter?.value !== 'save') return;
    event.preventDefault(); $('review-note-dialog').close('save');
    const note = $('review-note-text').value;
    if (noteTarget.id) { checkpoint(); const mark = state.annotations.find(item => item.id === noteTarget.id); if (mark) mark.note = note; save(); render(); }
    else if (note.trim()) { pending = noteTarget.spans; applyTier('fine', note, true); }
  });
  $('review-undo').addEventListener('click', () => {
    if (!undo.length) return;
    const base = state.expected_submission_id; state = undo.pop(); state.expected_submission_id = base;
    $('review-comment').value = state.comment; $('review-focus').value = state.focus || ''; pending = []; active = null; save(); render(); selectionStatus();
  });
  $('review-comment').addEventListener('focus', checkpoint);
  $('review-comment').addEventListener('input', event => { state.comment = event.target.value; save(); });
  $('review-focus').addEventListener('focus', checkpoint);
  $('review-focus').addEventListener('input', event => { state.focus = event.target.value; save(); });
  $('review-toggle').addEventListener('click', () => { const hidden = document.body.classList.toggle('review-sidebar-hidden'); $('review-toggle').setAttribute('aria-expanded', String(!hidden)); });
  const resize = new ResizeObserver(() => document.documentElement.style.setProperty('--review-header', $('review-header').offsetHeight + 'px'));
  resize.observe($('review-header'));

  function feedback() { return { schema_version: 1, report_id: config.id, source_hash: config.source_hash, submission_id: uid(), expected_submission_id: state.expected_submission_id, annotations: clone(state.annotations), comment: state.comment, focus: state.focus || '' }; }
  function download(text, filename, type) {
    const url = URL.createObjectURL(new Blob([text], { type })); const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  function portableHTML() {
    const documentCopy = document.documentElement.cloneNode(true);
    const exported = { ...config, workspace_id: null, review_copy_id: uid(), feedback: { ...feedback(), submission_id: null } };
    documentCopy.querySelector('#report-config').textContent = JSON.stringify(exported).replaceAll('<', '\\u003c');
    documentCopy.querySelector('#report-content').innerHTML = originalBody;
    documentCopy.querySelector('#review-annotations').replaceChildren();
    documentCopy.querySelector('#review-note-dialog').removeAttribute('open');
    documentCopy.querySelector('#review-alert').hidden = true;
    documentCopy.querySelector('#review-receipt').replaceChildren();
    documentCopy.querySelector('#review-writer-status').replaceChildren();
    return '<!doctype html>\n' + documentCopy.outerHTML;
  }
  function cleanHTML() { return '<!doctype html><html lang="en">' + originalHead.outerHTML + '<body>' + originalBody + '</body></html>'; }
  $('review-share').addEventListener('click', () => { download(portableHTML(), config.id + '-review.html', 'text/html;charset=utf-8'); alert('Review HTML downloaded with your current marks and remarks. Send that file to a coworker.'); });
  $('review-feedback').addEventListener('click', () => download(JSON.stringify(feedback(), null, 2), config.id + '-feedback.json', 'application/json'));
  $('review-clean').addEventListener('click', () => download(cleanHTML(), config.id + '.html', 'text/html;charset=utf-8'));
  $('review-print').addEventListener('click', () => window.print());
  function markdown() {
    const root = document.createElement('div'); root.innerHTML = originalBody;
    root.querySelectorAll('script,style,nav,.screen-only').forEach(element => element.remove());
    function convert(node) {
      if (node.nodeType === Node.TEXT_NODE) return node.data.replace(/[\t\r\n ]+/g, ' ').replace(/([\\*`\[\]])/g, '\\$1');
      if (node.nodeType !== Node.ELEMENT_NODE) return '';
      const tag = node.tagName.toLowerCase();
      const inner = () => [...node.childNodes].map(convert).join('');
      if (/^h[1-6]$/.test(tag)) return '\n\n' + '#'.repeat(Number(tag[1])) + ' ' + inner().trim() + '\n\n';
      if (['p', 'div', 'section', 'blockquote'].includes(tag)) return '\n\n' + inner().trim() + '\n\n';
      if (tag === 'br') return '  \n';
      if (['b', 'strong'].includes(tag)) return '**' + inner() + '**';
      if (['i', 'em'].includes(tag)) return '*' + inner() + '*';
      if (tag === 'code') return '`' + node.textContent.replaceAll('`', '\\`') + '`';
      if (tag === 'a') return '[' + inner() + '](' + node.getAttribute('href') + ')';
      if (['sub', 'sup'].includes(tag)) return node.outerHTML;
      if (tag === 'figure') return '\n\n' + node.outerHTML + '\n\n';
      if (tag === 'table') {
        const rows = [...node.rows].map(row => [...row.cells].map(cell => [...cell.childNodes].map(convert).join('').trim().replaceAll('|', '\\|').replace(/\n+/g, '<br>')));
        if (!rows.length) return '';
        const width = Math.max(...rows.map(row => row.length)); rows.forEach(row => { while (row.length < width) row.push(''); });
        rows.splice(1, 0, Array(width).fill('---')); return '\n\n' + rows.map(row => '| ' + row.join(' | ') + ' |').join('\n') + '\n\n';
      }
      if (['ul', 'ol'].includes(tag)) return '\n\n' + [...node.children].map((child, i) => (tag === 'ol' ? `${i + 1}. ` : '- ') + [...child.childNodes].map(convert).join('').trim()).join('\n') + '\n\n';
      return inner();
    }
    return '# ' + config.title + '\n\n' + [...root.childNodes].map(convert).join('').replace(/\n{3,}/g, '\n\n').trim() + '\n';
  }
  $('review-md').addEventListener('click', () => download(markdown(), config.id + '.md', 'text/markdown;charset=utf-8'));
  $('review-import').addEventListener('change', async event => {
    try {
      const file = event.target.files[0]; if (!file) return;
      if (file.size > 8_000_000) throw Error('Review file is too large (maximum 8 MB).');
      const raw = await file.text(); let packet;
      if (/^\s*</.test(raw)) {
        const imported = new DOMParser().parseFromString(raw, 'text/html');
        const data = JSON.parse(imported.getElementById('report-config')?.textContent || 'null');
        if (!data) throw Error('This HTML file has no embedded review.');
        packet = data.feedback; if (!packet) throw Error('This HTML file has no feedback.');
      } else { const data = JSON.parse(raw); packet = data.feedback || data; }
      if (packet.report_id !== config.id || packet.source_hash !== config.source_hash) throw Error('This review belongs to another report version. Open that draft before importing it.');
      const next = { annotations: packet.annotations.map(cleanMark), comment: packet.comment || '', focus: packet.focus || '', expected_submission_id: state.expected_submission_id };
      validate(next); checkpoint(); state = next; active = null; pending = []; $('review-comment').value = state.comment; $('review-focus').value = state.focus;
      save(); render(); alert('Imported review loaded. Submit feedback to save it for the writer. Undo restores your previous draft.');
    } catch (error) { alert(error.message || 'The review could not be imported.'); }
    finally { event.target.value = ''; }
  });

  async function request(path, body) {
    const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json', 'X-Workspace-Id': config.workspace_id }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const result = await response.json();
    if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'The request was rejected; your local review is still saved.');
    return result;
  }
  function receipt() {
    const box = $('review-receipt'); box.replaceChildren();
    if (lastSubmission) {
      const text = document.createElement('p'); text.textContent = `Submitted · ${lastSubmission}`; box.append(text);
      const link = document.createElement('a'); link.href = `/api/reports/${config.id}/feedback/${lastSubmission}/packet`; link.textContent = 'Download writer handoff'; box.append(link);
    }
    box.append(button('Load submitted review', async () => {
      try {
        const data = await request(`/api/reports/${config.id}`); if (!data.feedback) return;
        checkpoint(); state = fromFeedback(data.feedback); submitted = clone(state); lastSubmission = data.feedback.submission_id;
        $('review-comment').value = state.comment; $('review-focus').value = state.focus; save(); render(); receipt();
      } catch (error) { alert(error.message); }
    }));
  }
  $('review-submit').addEventListener('click', async () => {
    if (!online) { download(portableHTML(), config.id + '-reviewed.html', 'text/html;charset=utf-8'); alert('Send this reviewed HTML back to the writer. It contains your marks and remarks.'); return; }
    if (busy) return;
    busy = true; updateStatus(); $('review-submit').textContent = 'Submitting…';
    const packet = pendingSubmission || feedback(); pendingSubmission = packet;
    try {
      validate(state); const result = await request(`/api/reports/${config.id}/feedback`, packet);
      lastSubmission = result.submission_id; submitted = fromFeedback(result); state.expected_submission_id = result.submission_id;
      save(); receipt(); alert('Feedback submitted. The report wording is unchanged. Ask for a revision when you are ready.');
    } catch (error) { alert(error.message); }
    finally { busy = false; $('review-submit').textContent = 'Submit feedback'; updateStatus(); }
  });
  async function watchJob(id) {
    try {
      const job = await request(`/api/report-writer/jobs/${id}`);
      if (job.status === 'running') { $('review-writer-status').textContent = `Writing stage: ${(job.stage || 'preparing').replaceAll('_', ' ')}. You can leave this page and return later.`; writerTimer = setTimeout(() => watchJob(id), 3000); return; }
      $('review-revise').disabled = dirty();
      if (job.status === 'completed' || job.status === 'needs_attention') {
        const link = document.createElement('a'); link.href = '/reports/' + job.result_id; link.textContent = job.status === 'needs_attention' ? 'Open draft with unresolved review issues →' : 'Open revised draft →'; $('review-writer-status').replaceChildren(link);
      } else if (job.status === 'awaiting_focus') {
        const link = document.createElement('a'); link.href = '/#reports'; link.textContent = 'Answer on the Reports page →';
        $('review-writer-status').textContent = job.question + ' '; $('review-writer-status').append(link);
      } else $('review-writer-status').textContent = job.error || `Writer ${job.status}. Original and feedback are saved.`;
    } catch (error) { $('review-writer-status').textContent = error.message; }
  }
  $('review-revise').addEventListener('click', async () => {
    if (!online || !lastSubmission || dirty()) return;
    $('review-revise').disabled = true;
    try { const job = await request(`/api/reports/${config.id}/revise`, { submission_id: lastSubmission, request_id: uid() }); await watchJob(job.id); }
    catch (error) { alert(error.message); $('review-revise').disabled = false; }
  });
  window.addEventListener('storage', event => { if (event.key === key) alert('This review changed in another tab. Download this tab’s draft before reloading if you need both versions.'); });
  window.addEventListener('beforeunload', event => { if (storageFailed && (state.annotations.length || state.comment)) { event.preventDefault(); event.returnValue = ''; } });

  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null');
    if (saved?.source_hash === config.source_hash) {
      validate(saved.state);
      if (saved.state.expected_submission_id === state.expected_submission_id) state = saved.state;
      else if (fingerprint(saved.state) !== fingerprint(state)) {
        state = saved.state; alert('A newer review was submitted elsewhere. Your local draft is retained; download it or load the submitted review before continuing.');
      }
    }
  } catch { storageFailed = true; }
  $('review-title').textContent = config.title; $('review-comment').value = state.comment; $('review-focus').value = state.focus || '';
  if (config.revision?.editorial) {
    const notes = config.revision.editorial, box = $('review-editorial-details'); box.replaceChildren(); $('review-editorial').hidden = false;
    function paragraph(label, value) { if (!value) return; const p = document.createElement('p'); const b = document.createElement('strong'); b.textContent = label + ': '; p.append(b, document.createTextNode(value)); box.append(p); }
    paragraph('Evidence coverage', notes.coverage);
    paragraph('Inferred intent', (notes.brief?.inferred_intent || []).join(' '));
    paragraph('Reader and depth', notes.brief?.reader_and_depth);
    paragraph('Argument', notes.selection?.argument);
    for (const item of notes.selection?.claims || []) { const claim = notes.claims?.find(claim => claim.id === item.claim_id); paragraph(item.place === 'archive' ? 'Left out' : 'Selected', `${claim?.statement || item.claim_id} — ${item.reason}`); }
    for (const gap of [...(notes.snapshot_gaps || []), ...(notes.missing_evidence || [])]) paragraph('Evidence gap', gap);
    for (const issue of notes.unresolved || []) paragraph('Unresolved review issue', issue.problem + ' ' + issue.action);
    for (const proposal of notes.proposed_experiments || []) paragraph('Proposed follow-up', proposal);
    paragraph('Review status', notes.unresolved?.length ? 'The revision limit was reached. These issues still need attention.' : 'The automated reviews completed. The researcher still evaluates the scientific conclusions.');
    if (online) { const link = document.createElement('a'); link.href = `/api/report-writer/jobs/${config.revision.job_id}/artifacts`; link.textContent = 'Download full writing record'; box.append(link); }
    if (notes.unresolved?.length) { $('review-editorial').open = true; alert('This draft has unresolved scientific review issues. See Writing notes & evidence limits.'); }
  }
  if (config.revision) {
    $('review-revision').hidden = false;
    $('review-change-summary').textContent = config.revision.change_summary;
    $('review-change-details').replaceChildren();
    for (const [id, reason] of Object.entries(config.revision.feedback_response || {})) {
      const paragraph = document.createElement('p'); paragraph.textContent = reason;
      $('review-change-details').append(paragraph);
      if (config.revision.preservation_exceptions?.[id]) {
        const exception = document.createElement('p'); exception.textContent = 'Green phrase changed: ' + config.revision.preservation_exceptions[id];
        $('review-change-details').append(exception);
      }
    }
    if (online && config.parent_id) {
      const previous = document.createElement('a'); previous.href = '/reports/' + config.parent_id; previous.textContent = 'Open previous draft'; $('review-change-details').append(previous);
    }
  }
  if (!online) {
    $('review-home').hidden = true; $('review-mode').textContent = 'Portable review'; $('review-submit').textContent = 'Download reviewed HTML';
    $('review-submit-help').textContent = 'Send the downloaded file back to the writer. Changes stay in this browser until you download them.';
  } else {
    receipt();
    request(`/api/reports/${config.id}`).then(data => { const jobs = data.jobs || []; if (jobs.length) void watchJob(jobs.at(-1).id); }).catch(() => {});
  }
  render(); selectionStatus();
})();
