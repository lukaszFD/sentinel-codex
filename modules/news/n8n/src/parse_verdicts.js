// Parse verdicts
// Keeps only well-formed verdicts for ids that were actually claimed.
// On any error nothing is stored: the items stay pending_ai and are
// retried; after ai_max_attempts housekeeping marks them ai_error.
// Mode: Run Once for All Items.

const req = $('Build Gemini request').first().json;
const res = $input.first().json;

let verdicts = [];
let error = '';

if (res.error) {
  error = `Gemini request failed: ${res.error.message || JSON.stringify(res.error)}`;
} else if (res.statusCode !== 200) {
  error = `Gemini HTTP ${res.statusCode}: ${res.body?.error?.message || res.statusMessage || ''}`;
} else {
  try {
    const parts = res.body?.candidates?.[0]?.content?.parts || [];
    const parsed = JSON.parse(parts.map((p) => p.text || '').join(''));
    const claimed = new Set(req.ids);
    verdicts = (Array.isArray(parsed) ? parsed : [])
      .map((v) => ({
        id: Number(v.id),
        score: Math.round(Number(v.score)),
        summary_pl: String(v.summary_pl ?? '').slice(0, 1000),
        reason: String(v.reason ?? '').slice(0, 500),
      }))
      .filter((v) => claimed.has(v.id) && v.score >= 1 && v.score <= 5);
  } catch (e) {
    error = `Cannot parse Gemini answer: ${e.message}`;
  }
}

return [{
  json: {
    verdicts,
    model: req.model,
    prompt_version: req.prompt_version,
    claimed: req.ids.length,
    error,
  },
}];
