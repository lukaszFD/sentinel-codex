// Build digest e-mail
// Input: rows of "Digest due?" — nothing when no slot is due, one row with
// only digest_id when the slot is due but nothing qualifies, otherwise one
// row per item. Every dynamic value is HTML-escaped (titles and summaries
// come from the internet).
// Mode: Run Once for All Items.

const rows = $input.all().map((i) => i.json).filter((r) => r.digest_id);
if (!rows.length) return [];

const digestId = rows[0].digest_id;
const items = rows.filter((r) => r.id);
if (!items.length) {
  return [{ json: { digest_id: digestId, empty: true, ids: '{}' } }];
}

const esc = (v) => String(v ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? esc(u) : '#');
const local = (ts) => DateTime.fromISO(new Date(ts).toISOString()).setZone('Europe/Warsaw');

const SCORE_COLOR = { 5: '#f44336', 4: '#ff9800', 3: '#ffc107', 2: '#90a4ae', 1: '#607d8b' };
const TOPICS = [
  ['cyber', '🛡️ Cyber'],
  ['ai', '🤖 AI'],
  ['both', '📌 Other'],
];

const card = (r) => {
  const score = r.ai_score ?? '–';
  const color = SCORE_COLOR[r.ai_score] || '#607d8b';
  const summary = r.ai_summary_pl || '';
  return `
  <div style="border-left:4px solid ${color};background:#252525;border-radius:4px;padding:10px 12px;margin:0 0 10px;">
    <div style="font-size:15px;line-height:1.4;">
      <span style="display:inline-block;min-width:22px;text-align:center;background:${color};color:#111;font-weight:700;border-radius:3px;padding:0 5px;margin-right:6px;">${esc(score)}</span>
      <a href="${safeUrl(r.url)}" style="color:#90caf9;text-decoration:none;">${esc(r.title)}</a>
    </div>
    ${summary ? `<div style="margin-top:6px;font-size:14px;line-height:1.5;color:#e0e0e0;">${esc(summary)}</div>` : ''}
    <div style="margin-top:6px;font-size:12px;color:#888;">${esc(r.source || '')} · ${esc(local(r.published_at).toFormat('dd.MM HH:mm'))}</div>
  </div>`;
};

const sections = TOPICS
  .map(([topic, label]) => {
    const list = items.filter((r) => (r.topic || 'both') === topic);
    if (!list.length) return '';
    return `<h3 style="margin:18px 0 8px;color:#bbb;font-size:14px;text-transform:uppercase;letter-spacing:.05em;">${label} (${list.length})</h3>${list.map(card).join('')}`;
  })
  .join('');

const now = DateTime.now().setZone('Europe/Warsaw');
const top = Math.max(...items.map((r) => Number(r.ai_score) || 0));
const subject = `📰 News Radar ${now.toFormat('HH:mm')} — ${items.length} ${items.length === 1 ? 'news' : 'newsów'}`
  + (top >= 5 ? ' (jest coś pilnego)' : '');

const html = `<!DOCTYPE html>
<html><body style="font-family:'Segoe UI',Tahoma,sans-serif;background:#121212;color:#e0e0e0;margin:0;padding:20px;">
<div style="max-width:680px;margin:auto;background:#1e1e1e;border:1px solid #333;border-top:4px solid #3fb68b;border-radius:8px;overflow:hidden;">
  <div style="background:#1b3320;padding:14px;text-align:center;">
    <h2 style="margin:0;color:#81c784;">Sentinel Codex · News Radar</h2>
    <div style="color:#9e9e9e;font-size:13px;margin-top:4px;">${esc(now.toFormat('dd.MM.yyyy HH:mm'))} · ${items.length} pozycji, od najważniejszych</div>
  </div>
  <div style="padding:18px 22px;">${sections}</div>
  <div style="padding:10px 22px 16px;color:#777;font-size:11px;">Ocena 1–5 i streszczenia: Gemini. Źródła, słowa kluczowe i godziny wysyłki: strona Sentinel Codex · News Radar.</div>
</div>
</body></html>`;

return [{
  json: {
    digest_id: digestId,
    empty: false,
    ids: `{${items.map((r) => r.id).join(',')}}`,
    subject,
    html,
  },
}];
