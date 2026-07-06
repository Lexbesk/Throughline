# Extract Action Items

The meeting took place on **{meeting_date}** (for context only).

## About the user

{profile}

Use the profile only as context: recognizing the user's projects and priorities,
and resolving first-person mentions — if the notes say "I"/"me"/"my", the owner
is the user (their name from the profile, when given). It is never a reason to
invent items, owners, or dates the notes don't state.

Read the notes below and extract the action items, following the global rules:
extract only real action items (not background discussion, still-open decisions,
or FYIs), one action per item, phrased as a short imperative, with a source
snippet for each. Never invent an owner the notes do not state.

**Soft or unowned actions still count.** Phrasings like "we should…", "someone
needs to…", "let's…", or items explicitly noted as having no owner ("nobody owns
this yet") are real action items — extract them with `owner` set to null. A
missing owner is never a reason to drop an item. Keep precision, though: pure
discussion, background, and closed decisions that carry no follow-up action stay
excluded.

**Do not compute dates.** Copy each deadline *phrase* exactly as written into
`due_date_text` (e.g. "by Friday", "next Tuesday", "before the July 3 review").
The app resolves phrases into real dates in code. Use `null` when no deadline is
mentioned.

**Explain each item.** Give every item a one-sentence `rationale`: why it counts
as an action item and anything notable about how you read it (soft/unowned
phrasing, first-person owner, vague deadline, and so on). Keep it short and
concrete — it is shown to the user as your stated reasoning.

## Output format

Return **only** a JSON object — no prose, no markdown, no code fences — with this
shape:

    {
      "items": [
        {
          "title": "Short imperative, e.g. Send the Q3 deck to the leadership list",
          "description": "Optional extra context, or null",
          "owner": "Person responsible — only if explicitly stated, else null",
          "due_date_text": "The deadline phrase exactly as written, or null (do not compute a date)",
          "source_snippet": "The exact note text this item was derived from",
          "rationale": "One short sentence: why this is an action item / how you read it"
        }
      ]
    }

If there are no action items, return `{"items": []}`.

## Notes

{notes}
