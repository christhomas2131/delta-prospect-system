This is a follow-up to the Delta Prospect System that's already running. Read CLAUDE.md for full context. The rule-based enrichment engine and React dashboard are already working. Now we're adding a premium "Deep Analysis" feature powered by the Claude API that layers ON TOP of the existing rule-based signals.

DO NOT touch or break anything that's already working. This is additive only.

PHASE 1 — API KEY SETTINGS:

1. Add an API Settings page/modal to the React frontend (route "/settings" or a modal from nav):
   - A single input field: "Anthropic API Key" with a password-type input
   - A "Save" button that stores the key in the FastAPI backend (in memory or .env, NOT in the database)
   - A "Test Connection" button that makes a minimal Claude API call to verify the key works
   - A status indicator: "Not configured" (grey), "Valid" (green), "Invalid" (red)
   - A note: "Optional. Enables Deep Analysis for more granular pressure signal detection. API calls cost ~$0.01-0.03 per company."

2. Add a new FastAPI endpoint:
   - POST /api/settings/api-key — accepts {"api_key": "sk-ant-..."}, validates it with a test call to Claude, stores in memory
   - GET /api/settings/api-key/status — returns {"configured": true/false, "valid": true/false}
   - The key should persist in a server-side variable (or write to .env), NOT in the database

PHASE 2 — DEEP ANALYSIS ENGINE:

Create a new file `deep_analysis.py` alongside the existing enrichment_agent.py. This module:

1. Takes a single company (ticker) and its existing rule-based signals as input
2. Fetches the company's ASX announcements (reuse the fetch logic from enrichment_agent.py)
3. Sends announcement titles + any existing signal summaries to Claude API with this prompt strategy:
   - System prompt: "You are an expert industrial analyst specializing in ASX-listed mining, energy, and heavy industry. Analyze with precision."
   - User prompt: Provide the company name, ticker, sector, existing rule-based signals, and all announcement titles
   - Ask Claude to:
     a) Validate or invalidate each existing rule-based signal (confirm/deny with reasoning)
     b) Detect NEW signals the keyword engine missed (subtle language, context-dependent, multi-announcement patterns)
     c) Generate a refined strategic profile with more specific tailwind/headwind analysis
     d) Provide a refined likelihood score with reasoning
   - Response format: JSON with validated_signals[], new_signals[], refined_profile{}

4. Merges results:
   - Rule-based signals marked as "validated by AI" get a confidence boost (0.8 -> 0.95)
   - Rule-based signals marked as "invalidated by AI" get confidence dropped (-> 0.2) and flagged
   - New AI-detected signals are inserted with source "claude_deep_analysis"
   - Profile fields are updated if Claude provides better specifics
   - Score is recalculated

5. Uses model: claude-sonnet-4-20250514, max_tokens: 4096

PHASE 3 — BACKEND ENDPOINTS:

Add to api.py:
- POST /api/prospects/{id}/deep-analysis — triggers deep analysis for one prospect
  - Returns 402 if no API key configured
  - Returns the analysis results when complete (synchronous, not background — it's one company, takes ~5 seconds)
  - Saves results to database
  - Adds an enrichment_log entry with agent_version="claude-deep-v1" and tokens_used from the API response

- GET /api/prospects/{id} — update the existing detail endpoint to include a "deep_analysis_available" boolean and "last_deep_analysis_at" timestamp

PHASE 4 — DASHBOARD UI:

Update the Prospect Detail View:

1. Add a "Deep Analysis" button next to the existing "Enrich Now" button:
   - If API key NOT configured: button is greyed out with tooltip "Configure API key in Settings"
   - If API key IS configured: button is active, styled as a premium/gold accent button
   - When clicked: shows a loading state ("Analyzing with Claude..."), calls POST /api/prospects/{id}/deep-analysis
   - On completion: refreshes the signals list and profile data

2. In the pressure signals list, add visual distinction:
   - Rule-based signals: show a small "Rule" badge
   - AI-detected signals: show a small "AI" badge with a subtle gold/premium color
   - AI-validated rule signals: show both "Rule" + "AI Verified" badges
   - AI-invalidated signals: show with strikethrough or dimmed styling + "AI Disputed" badge

3. In the strategic profile section:
   - If deep analysis has been run, show "AI-Enhanced Profile" label
   - Show the AI's reasoning for the likelihood score as a tooltip or expandable section

4. Add a nav indicator:
   - If API key is configured, show a small "PRO" or diamond icon in the nav near Settings
   - This gives visual feedback that premium features are active

PHASE 5 — VERIFICATION:

1. Confirm the settings page works and can save/validate an API key
2. Confirm the Deep Analysis button appears greyed out when no key is set
3. Ask me for my API key, save it via settings
4. Run deep analysis on BHP
5. Show me the before/after: rule-based signals vs AI-enhanced signals
6. Confirm the UI shows the Rule/AI badges correctly
7. Show me the cost (tokens used) for that single analysis

Keep the industrial dark theme consistent. The premium/AI elements should use a subtle gold (#D4AF37) accent to distinguish them from the standard rule-based features.
