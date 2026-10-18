// Parse feed
// Normalises one HTTP response (RSS, Atom, Hacker News JSON, CISA KEV JSON)
// into [{url, title, summary, published_at}] for sp_record_fetch().
// Never throws: a failed source is passed on as `error`, so one dead feed
// cannot stop the run.
// Mode: Run Once for Each Item.

const src = $('Build fetch request').item.json;
const res = $json;
const MAX_ITEMS = 100;

const out = (items, error) => ({
  json: {
    source_id: src.source_id,
    source_name: src.source_name,
    items: items.slice(0, MAX_ITEMS),
    error: error ? String(error).slice(0, 500) : '',
  },
});

const decodeEntities = (s) => s
  .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
  .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
  .replace(/&nbsp;/g, ' ')
  .replace(/&quot;/g, '"')
  .replace(/&apos;/g, "'")
  .replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>')
  .replace(/&amp;/g, '&');

// CDATA -> entities -> tags -> entities again: Google News puts escaped
// HTML (with its own &nbsp;) in <description>.
const clean = (s) => decodeEntities(
  decodeEntities(String(s ?? '').replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1'))
    .replace(/<[^>]*>/g, ' '),
)
  .replace(/\s+/g, ' ')
  .trim();

const tag = (block, names) => {
  for (const name of names) {
    const m = block.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, 'i'));
    if (m) return m[1];
  }
  return '';
};

const isoDate = (s) => {
  const d = new Date(clean(s));
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
};

const atomLink = (block) => {
  const links = [...block.matchAll(/<link\b([^>]*)\/?>/gi)].map((m) => m[1]);
  const pick = links.find((a) => !/rel=["']/.test(a) || /rel=["']alternate["']/.test(a)) || links[0];
  const href = pick && pick.match(/href=["']([^"']+)["']/i);
  return href ? decodeEntities(href[1]) : '';
};

const parseXml = (xml) => {
  const rss = [...xml.matchAll(/<item\b[\s\S]*?<\/item>/gi)].map((m) => m[0]);
  if (rss.length) {
    return rss.map((b) => ({
      url: clean(tag(b, ['link'])) || clean(tag(b, ['guid'])),
      title: clean(tag(b, ['title'])),
      summary: clean(tag(b, ['description', 'content:encoded'])).slice(0, 2000),
      published_at: isoDate(tag(b, ['pubDate', 'dc:date', 'published', 'updated'])),
    }));
  }
  const atom = [...xml.matchAll(/<entry\b[\s\S]*?<\/entry>/gi)].map((m) => m[0]);
  return atom.map((b) => ({
    url: atomLink(b),
    title: clean(tag(b, ['title'])),
    summary: clean(tag(b, ['summary', 'content'])).slice(0, 2000),
    published_at: isoDate(tag(b, ['published', 'updated'])),
  }));
};

const parseHn = (data) => (data.hits || []).map((h) => ({
  url: h.url || `https://news.ycombinator.com/item?id=${h.objectID}`,
  title: h.title || '',
  summary: `Hacker News: ${h.points ?? 0} points, ${h.num_comments ?? 0} comments`,
  published_at: h.created_at || null,
}));

// KEV only has a date. Entries added today or yesterday (UTC) are new to us
// and get "now" as published_at, otherwise max_item_age_hours would mark
// yesterday's additions stale. URL dedupe stops them repeating.
const parseKev = (data) => {
  const cutoff = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  const now = new Date().toISOString();
  return (data.vulnerabilities || [])
    .filter((v) => v.dateAdded && v.dateAdded >= cutoff)
    .map((v) => ({
      url: `https://nvd.nist.gov/vuln/detail/${v.cveID}`,
      title: `${v.cveID}: ${v.vulnerabilityName} (${v.vendorProject} ${v.product}) added to CISA KEV`,
      summary: `${v.shortDescription || ''} Required action: ${v.requiredAction || ''}`.trim(),
      published_at: now,
    }));
};

if (res.error) return out([], res.error.message || JSON.stringify(res.error));
if (res.statusCode !== 200) return out([], `HTTP ${res.statusCode} ${res.statusMessage || ''}`.trim());

try {
  const body = res.body ?? res.data ?? '';
  const raw = typeof body === 'string' ? body : JSON.stringify(body);
  let items;
  if (src.source_type === 'hn_algolia') items = parseHn(typeof body === 'string' ? JSON.parse(raw) : body);
  else if (src.source_type === 'cisa_kev') items = parseKev(typeof body === 'string' ? JSON.parse(raw) : body);
  else items = parseXml(raw);
  items = items.filter((i) => /^https?:\/\//.test(i.url) && i.title);
  return out(items, null);
} catch (e) {
  return out([], `parse error: ${e.message}`);
}
