# ALE Reviewer V2 Update Feed Format

`updates/latest.json` example:

```json
{
  "schemaVersion": 1,
  "appMinVersion": "2.0.0",
  "bankVersion": "2.1.0",
  "bankUrl": "../question-bank-v2.1.0.json",
  "released": "2026-10-15",
  "questionCount": 360,
  "releaseNotes": ["Added 60 questions"]
}
```

The bank JSON must contain `schemaVersion`, `bankVersion`, and a `questions` array. Each question needs subject, question, four options, answer index (0-3), explanation, and four optionExplanations.

For automatic updating, host the folder over HTTPS and keep `updates/latest.json` at a stable address. The app checks it on launch. You can also enter an absolute HTTPS feed URL inside the app's Updates screen.
