"""Prompts and output schemas shared by every AI provider."""

CATEGORIES = [
    "delivery",
    "problem-solving",
    "collaboration",
    "leadership",
    "mentoring",
    "communication",
    "process-improvement",
    "customer",
    "learning",
    "admin",
    "other",
]

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence capturing the day."},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "impact": {"type": "string"},
                },
                "required": ["text", "category", "impact"],
                "additionalProperties": False,
            },
        },
        "wins": {"type": "array", "items": {"type": "string"}},
        "projects": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        "challenges": {"type": "array", "items": {"type": "string"}},
        "follow_ups": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "highlights", "wins", "projects", "people", "challenges", "follow_ups", "tags"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = f"""You turn someone's spoken or typed end-of-day brain dump about their job into a clear,
honest work log they can later use for performance reviews.

The input is often a rambling voice transcript: it may jump around, repeat itself, contain filler words
or transcription mistakes. Extract what actually happened.

Write in the first person ("Fixed...", "Led...", "Helped..."), past tense, concise and specific.
Keep concrete details: names of projects, systems, people, numbers, outcomes.

Fields:
- headline: one sentence summing up the day.
- highlights: each distinct piece of work or contribution. `text` says what was done; `category` is one of
  {", ".join(CATEGORIES)}; `impact` says why it mattered (who benefited, what it unblocked, time/money/risk
  saved). If the impact isn't stated, infer a modest, plausible one from context, or leave `impact` empty.
  Never invent facts, numbers or outcomes that weren't said or clearly implied.
- wins: notable achievements or positive feedback, if any.
- projects: projects, products or systems mentioned.
- people: people or teams worked with (as named).
- challenges: blockers, frustrations or problems.
- follow_ups: things they said they need to do next.
- tags: 2-6 short lowercase topic tags useful for searching later.

Use empty lists where nothing applies. Ignore small talk that isn't about work."""


REPORT_STYLES = {
    "review": {
        "label": "Performance review",
        "instructions": """Write a self-assessment for a performance review. Structure:
# title, a short executive summary, then sections grouped by theme (e.g. Delivery, Technical impact,
Collaboration & leadership, Growth). Under each theme, give the strongest examples with concrete impact,
in a situation/action/result style where the logs support it. Finish with "Challenges & lessons" and
"Looking ahead" (from follow-ups and recurring themes).""",
    },
    "brag": {
        "label": "Brag document",
        "instructions": """Write a brag document: a thorough, scannable list of everything worth remembering.
# title, then sections: Key achievements, Projects (one subsection per project with bullets), Collaboration &
helping others, Learning, Recognition. Every bullet should be specific and include impact where known.""",
    },
    "update": {
        "label": "Status update",
        "instructions": """Write a concise status update for a manager. # title, then: TL;DR (3 bullets),
What I delivered, In progress, Blockers / risks, Next up. Keep it tight; favour the most important items.""",
    },
    "blog": {
        "label": "Blog post",
        "instructions": """Write an engaging first-person blog post reflecting on this period of work: what I
worked on, what I learned, what went well and what didn't. Use a # title and a few ## sections. Keep internal
names generic if they look sensitive (e.g. "an internal billing service") since this may be shared publicly.""",
    },
}

REPORT_SYSTEM = """You write reports about someone's work from their own daily work log.
Ground every statement in the log entries provided. Do not invent achievements, numbers or feedback.
Merge duplicates and connect related work across days into bigger stories (e.g. a project that ran for weeks).
Prefer impact and outcomes over activity. Write in the first person. Output GitHub-flavoured Markdown only,
starting with a level-1 heading for the title."""
