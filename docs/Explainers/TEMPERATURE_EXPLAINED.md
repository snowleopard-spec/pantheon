# Temperature, explained

*Companion note for the scoring pipeline and the v2.5 deep-score spec. Non-technical.*

## What it is

When a language model writes its answer, it doesn't produce words directly — at each step it produces a *probability list* over possible next words ("revenue": 62%, "sales": 20%, "income": 9%, …) and then picks one. **Temperature is the knob that controls how that pick is made.**

- **Temperature 0** ("cold"): always pick the single most likely word. The model becomes as close to deterministic as it gets — ask the same question twice, and you'll usually get the same answer twice.
- **Higher temperatures** ("warm", typically up to 1.0): sometimes pick a less-likely word. The output gets more varied, more surprising, occasionally more creative — and less repeatable.

A useful analogy: temperature 0 is a pianist playing the sheet music exactly as written, every performance identical; temperature 1 is the same pianist improvising around the melody — recognisably the same piece, different every night.

## Why Pantheon's scoring prompt says "temperature 0"

The scoring pipeline asks the model a question with a *right-ish answer* ("what fraction of this company's revenue comes from this activity?"), not a creative one. We want the same filing to produce the same score if we ask again — variety is a bug here, not a feature. So the original spec (prompt v1.6) pinned temperature to 0, and every Haiku scoring request sends it.

Two honest footnotes:

1. **Temperature 0 never fully guaranteed identical outputs** — it makes repeats very likely, not certain. That's one reason the pipeline scores every company **twice** and averages, with QC flagging the pairs that disagree by more than 0.2. The two-run design, not the temperature setting, is the real repeatability mechanism.
2. **Turning temperature *up* is how people get deliberate variety** — brainstorming, fiction, generating multiple design options. Pantheon never wants that mode.

## The twist: the newest models removed the knob

Anthropic's latest models (Claude Opus 5, Claude Fable 5, and the recent Opus 4.7/4.8 generation) **no longer accept a temperature setting at all** — sending one is an error, and the model manages its own decoding. The reasoning-heavy way these models work replaced the old sampling knob; repeatability is approached through prompt design instead.

**What this means for Pantheon:** the Haiku requests (broad universe) keep sending temperature 0 as always. The v2.5 deep-score requests to Opus 5 simply **omit the parameter** — that's not a loosening of rigour, it's the only valid way to call that model, and the two-run averaging + QC disagreement checks continue to do the real consistency work for both models.
