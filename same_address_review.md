# Same-Address Host Pair Review
## OneTable Trust and Safety | Internal Use Only

You are reviewing same-address host pairs flagged from a OneTable weekly Salesforce report CSV. For each pair, analyze the following and produce a summary document.

**Inputs provided:** uploaded CSV from the weekly Salesforce report.

---

## Step 1 — Identify pairs

Find all dinners from the target week where two or more hosts share the same street address and zip code (normalize to street + zip, strip USA suffix). Flag any address with 2+ distinct host Contact IDs.

---

## Step 2 — For each pair, pull from the full CSV history

- Each host's total dinner count and Nourishment received
- All dinner dates for each host — count how many Fridays both hosted on the same date
- Guest Contact IDs for the target week — check for overlap between the pair
- Dinner descriptions for all dinners — compute pairwise similarity (SequenceMatcher ratio) cross-pair and within-host (flag self-recycling at 70%+)
- AI Not Pass flag on any dinner
- For each host: 15-char Contact ID from the CSV and the Campaign ID of their most recent dinner in the report

---

## Step 3 — Assess each pair

Never use "No concern" — simultaneous hosting at the same address always warrants a note, even when explainable by a large apartment building.

| Label | When to use |
|---|---|
| Program Policy — Not Fraud | Zero guest overlap, low description similarity, large building in major city likely, independent social circles |
| Household policy follow-up | Specific address in small/mid-size city or suburb; OR continuous same-date overlap starting on a specific date; OR material combined Nourishment |
| Monitor | Guest contact overlap detected but explainable; OR description similarity >60% without clear theme explanation |
| Flag for review | Guest contact overlap with corroborating signals; OR description similarity >60% with other anomalies |

Large apartment building in a major city: note it as context but still assign the most appropriate label — building size is not a reason to dismiss.

Specific address in a small city, suburb, or non-major market: treat simultaneous hosting as more likely to reflect a shared household.

---

## Step 4 — Produce a Word document

Style: plain, minimal formatting. Black text, no color-coded cells, standard table borders. Use bold for labels and key figures. No decorative headers.

Structure:

1. Title and date
2. Summary table: Address | Pair | Same-Date Dinners | Guest Overlap | Max Desc Similarity | Assessment
3. Key findings — short bullet list of cross-pair patterns and program quality notes
4. Per-pair sections:
   - Assessment label + one-sentence rationale
   - Context note if small city or specific building
   - Profile table with one row per host containing: name, dinner count, Nourishment received, Salesforce Contact link (`https://onetable.lightning.force.com/lightning/r/Contact/{18-char-ID}/view` — convert 15-char ID from CSV using sf_15_to_18 logic, or query Salesforce for 18-char ID if MCP is available), and a link to their most recent campaign (`https://onetable.lightning.force.com/lightning/r/Campaign/{Campaign-ID}/view`)
   - Same-date history table
   - Description similarity (top cross-pair matches ≥30%, self-recycling ≥70%)
   - 3–5 findings bullets
5. Recommended actions (immediate / before next approval cycle / program quality)

**Always include if applicable:**
- Combined Nourishment for any household policy pair
- Flag when a new host debuts at the same address as an established host
