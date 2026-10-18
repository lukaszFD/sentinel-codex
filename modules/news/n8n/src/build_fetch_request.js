// Build fetch request
// Turns each due source row into the URL the "Fetch source" node calls.
// Google News and Hacker News sources only store a search query.
// Mode: Run Once for All Items.

const GN_LOCALE = {
  en: { hl: 'en-US', gl: 'US', ceid: 'US:en' },
  pl: { hl: 'pl', gl: 'PL', ceid: 'PL:pl' },
};
const qs = (o) => Object.entries(o)
  .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
  .join('&');
const dayAgo = Math.floor(Date.now() / 1000) - 86400;

return $input.all()
  .map((item, idx) => ({ s: item.json, idx }))
  .filter(({ s }) => s.id)
  .map(({ s, idx }) => {
    let url;
    if (s.source_type === 'google_news') {
      url = 'https://news.google.com/rss/search?'
        + qs({ q: `${s.query} when:1d`, ...(GN_LOCALE[s.lang] || GN_LOCALE.en) });
    } else if (s.source_type === 'hn_algolia') {
      url = 'https://hn.algolia.com/api/v1/search_by_date?'
        + qs({ query: s.query, tags: 'story', hitsPerPage: 50, numericFilters: `created_at_i>${dayAgo}` });
    } else {
      url = s.endpoint;
    }
    return {
      json: { source_id: s.id, source_name: s.name, source_type: s.source_type, fetch_url: url },
      pairedItem: { item: idx },
    };
  });
