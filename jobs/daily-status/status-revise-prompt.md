You are revising your own daily status draft after its privacy check withheld it. The post goes to
#team_status, a public Slack channel the whole company reads. This is an unattended run: do not ask
questions, and do not post to Slack.

Read `RUN_DIR_PLACEHOLDER/review.json`. Its `reasons` list names each item the check withheld and
the rule it breaks. Then edit `RUN_DIR_PLACEHOLDER/draft.json` in place:
- Fix only the items the reasons name. Leave every other item, and every link, exactly as it is.
- Reword a flagged item so it no longer breaks the rule. Describe the work by what it changes for
  the product, and drop the name or detail rather than hint at it. A customer name becomes nothing,
  not an initial or a nickname.
- If an item cannot be reworded without the flagged content, remove it.
- Keep the file valid `daily-status/v1` JSON with the same `schema`, `date`, `covers` and five
  sections. Item text still carries no URL.

The rules for a public channel are unchanged: no customer, carrier or shipper names, contact
details, load or account specifics; nothing credential-shaped; no security findings or IAM detail;
no production data, error contents or incident detail beyond a ticket reference; no quotes from
conversations. Do not add new items or new claims. Do not edit `review.json`; a fresh check reviews
the revised draft.
