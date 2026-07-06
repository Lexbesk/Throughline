# Reconcile a New Item Against the Existing List

A new action item was extracted from a meeting. The candidate items below are the
existing TODO-list entries most similar to it. Decide how the new item relates to
them and choose exactly one label:

- **NEW** — none of the candidates is the same task.
- **DUPLICATE** — a candidate is the same task and the new item adds no new
  information.
- **UPDATE** — a candidate is the same task, and the new item adds information
  (an owner, a due date, more detail).

Rules:
- "Same task" means the same underlying work, even if worded differently.
- A candidate marked **done** or **cancelled** can only be a DUPLICATE — never
  choose UPDATE for it (completed work is never modified or resurrected).
- For DUPLICATE or UPDATE, return the **number** of the matching candidate.

## New item

{new_item}

## Candidate items

{candidate_items}

## Output

Return only a JSON object — no prose, no code fences:

    {"label": "NEW | DUPLICATE | UPDATE", "candidate": <candidate number or null>, "reason": "<one short sentence>"}
