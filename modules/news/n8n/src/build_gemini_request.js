// Build Gemini request
// One generateContent call for the whole claimed batch. The active prompt
// (prompts) gets the items in place of [[ITEMS_JSON]]; JSON mode +
// responseSchema make the answer machine-readable.
// Mode: Run Once for All Items.

const MODEL = 'gemini-3.1-flash-lite';

const rows = $input.all().map((i) => i.json).filter((r) => r.id);
if (!rows.length) return [];

const items = rows.map((r) => ({
  id: Number(r.id),
  source: r.source,
  title: r.title,
  summary: (r.summary || '').slice(0, 600),
  published_at: r.published_at,
  matched_keywords: r.matched_keywords,
}));

const prompt = rows[0].system_prompt.replace('[[ITEMS_JSON]]', JSON.stringify(items, null, 1));

const body = {
  contents: [{ role: 'user', parts: [{ text: prompt }] }],
  generationConfig: {
    temperature: 0.2,
    maxOutputTokens: 4096,
    responseMimeType: 'application/json',
    responseSchema: {
      type: 'ARRAY',
      items: {
        type: 'OBJECT',
        properties: {
          id: { type: 'INTEGER' },
          score: { type: 'INTEGER' },
          summary_pl: { type: 'STRING' },
          reason: { type: 'STRING' },
        },
        required: ['id', 'score', 'summary_pl', 'reason'],
      },
    },
  },
};

return [{
  json: { model: MODEL, prompt_version: rows[0].prompt_version, ids: items.map((i) => i.id), body },
}];
