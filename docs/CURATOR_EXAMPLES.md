# How the curator judges

The curators the setup recommends, judging the same five titles for a **fictional** owner, Jane Doe. Every verdict below comes from the production judge and card note, word for word; only the review lines are written by a person after reading the run.

Generated 2026-10-22 by `python -m tests.benchmarks.hwbench.run examples`.

**Jane Doe** (invented) likes slow-burn horror and sharp social satire, values economical, well-crafted filmmaking, and has little patience for formulaic sequels or long sitcoms. **Sam** lives in her household. Four titles are invented; the fifth carries public metadata only.

## What to look for

- **The clear cases agree.** The landmark stays, the weak sequel goes, and the sitcom Sam watches stays although Jane dislikes it: the constitution ranks what the household watches and what is objectively significant above the owner's taste.
- **The gray zones do not always agree, and each model argues its own way through them.** The standard is whether the reasoning holds together and follows the law, not whether two models reach the same verdict.
- **A failed answer never deletes anything.** A verdict that fails validation twice becomes EVALUATE: neither proposed nor protected.
- **The curator learns.** When the owner disagrees with a verdict and talks it through, Curatarr extracts a principle from the conversation, checks that it is new, and appends it to the judge's law. From the next run every model judges with it.

## Overview

| Title · what decides it | `gemma4:26b-a4b-it-qat` | `gemma4:e4b-it-qat` | `gemma4:31b-it-qat` |
|---|---|---|---|
| Night of the Living Dead (1968) · Custodian | Keep ✓ | Keep ✓ | Keep ✓ |
| Harbor Light (2019, invented) · Resonance | Keep ✓ | Keep ! | Keep ✓ |
| The Example Sequel (2014, invented) · Cut | Delete ✓ | Delete ✓ | Delete ✓ |
| Flatmates (2009, invented) · Household | Keep ! | Keep ✓ | Keep ! |
| Midnight Static (1994, invented) · Owner signal | Ask the owner ✓ | Ask the owner ! | Ask the owner ! |

✓ consistent with the constitution · ! a defensible verdict with a flaw in the reasoning · ✕ an error

## Night of the Living Dead (1968)

A landmark of the genre, in the US National Film Registry. Jane has not seen it; the file is normal.

*What the constitution asks:* Keep: it is a landmark (Custodian).

### `gemma4:26b-a4b-it-qat` — Keep (CUSTODIAN)

- **III Household:** No other household users have watched this title; no protection under Pillar III.
- **II Custodian:** The title is a landmark of its form, credited with founding the modern zombie film and selected for the US National Film Registry.
- **I Resonance:** The title does not qualify for Resonance; its tension and bleakness are driven by survival and social breakdown rather than meditative awe.
- **0 Ego:** The title aligns with the owner's preference for well-crafted filmmaking and social satire, though it is not explicitly protected by this pillar alone.
- **File:** Bitrate is within the normal class median.

> A claustrophobic study of social breakdown, this film uses a rural farmhouse siege to navigate the friction between strangers amidst a bleak survival scenario. It eschews spectacle for a methodical, tense descent into racial tension and structural collapse that effectively defined an entire cinematic lineage.

**Review ✓** Keeps it on the right pillar for the right reason: the registry selection and the founding of the zombie film, both from the facts. Resonance is declined with an actual argument instead of a stamp.

### `gemma4:e4b-it-qat` — Keep (CUSTODIAN)

- **III Household:** No other household users have genuinely engaged with this title.
- **II Custodian:** The film is significant as it was selected for the US National Film Registry and is credited with founding the modern zombie film.
- **I Resonance:** This title is not a slow, quiet, or observational work, so the resonance criteria do not apply.
- **0 Ego:** The film's themes of social breakdown and tense, bleak mood align with the owner's appreciation for sharp social satire and well-crafted filmmaking.
- **File:** N/A

> This is a study in claustrophobic decay, using a contained rural setting to expose simmering social fissures rather than just delivering gore. It methodically charts a community's disintegration under extreme duress, leveraging the siege dynamic to examine underlying societal fractures. The slow build toward inevitable entropy is precise.

**Review ✓** Custodian, from the registry and the zombie-film lineage - the same reasoning as the large models. Its file line ('N/A') is empty, which costs nothing here because no flag depends on it.

### `gemma4:31b-it-qat` — Keep (CUSTODIAN)

- **III Household:** No other household users have engaged with this title.
- **II Custodian:** The title is a landmark of its form, credited with founding the modern zombie film and selected for the US National Film Registry.
- **I Resonance:** Not evaluated; protected by a higher pillar.
- **0 Ego:** The film's focus on social breakdown and economical filmmaking aligns with the owner's preference for sharp satire and craft over spectacle.
- **File:** Bitrate is within normal parameters.

> A bleak, methodical study of social breakdown disguised as a siege. It strips away the supernatural fluff to focus on the friction between strangers trapped in a rural farmhouse, weaponizing racial tension and claustrophobia. Essential viewing for its structural influence on every undead narrative that followed.

**Review ✓** Custodian from the registry and the film's founding role; stops evaluating lower pillars once a higher one protects, which is allowed.

## Harbor Light (2019, invented)

A quiet, almost wordless drama: RT 88 %, one regional cinematography prize. Not Jane's genres.

*What the constitution asks:* Gray zone: the Resonance litmus (intent, awe, rigor) decides.

### `gemma4:26b-a4b-it-qat` — Keep (RESONANCE)

- **III Household:** No other household users have engaged with this title; no protection granted.
- **II Custodian:** While it has high critical acclaim and a cinematography award, it lacks the documented landmark status or cultural milestone required for objective stature.
- **I Resonance:** The title passes the litmus: it exhibits INTENT through its focus on silence and grief, evokes AWE through its patient, contemplative depiction of the coast, and shows RIGOR via its masterful, wordless pacing.
- **0 Ego:** The title's focus on economical, well-crafted filmmaking and slow-burn pacing aligns with the owner's preference for substance over spectacle.
- **File:** Bitrate is within the normal class median.

> A study in stillness, this film captures a lighthouse keeper’s final winter through patient, long-form cinematography that prioritizes the landscape over dialogue. It trades narrative momentum for a melancholic, almost wordless exploration of grief and seasonal transition.

**Review ✓** The gray zone handled as the law asks: it refuses Custodian (a regional prize is no landmark) and names all three parts of the Resonance litmus - intent, awe, rigor - before keeping.

### `gemma4:e4b-it-qat` — Keep (CUSTODIAN)

- **III Household:** No other household users have genuinely engaged with the title.
- **II Custodian:** The title has received strong critical acclaim, including an 88% on Rotten Tomatoes and a regional award for Cinematography.
- **I Resonance:** The title's contemplative mood and praise for patient takes suggest a deliberate, observational style.
- **0 Ego:** The film's focus on economical, well-crafted filmmaking aligns with the owner's preference for quality over spectacle.
- **File:** The title is not a bitrate outlier.

> This is a study in protracted stillness, observing an aging custodian navigate the deceleration of his solitary function against the severe backdrop of a winter coast. It prioritizes atmospheric density and the weight of silence over conventional narrative momentum, demanding patience with its deliberate framing.

**Review !** The keep is defensible, but it rests on the wrong pillar: an 88 % score and a regional prize are acclaim, not the landmark stature Custodian asks for. Its own Resonance line points to the better argument and stops short of the litmus.

### `gemma4:31b-it-qat` — Keep (RESONANCE)

- **III Household:** No other household users have engaged with this title.
- **II Custodian:** While it has a strong Rotten Tomatoes score and a regional cinematography award, it lacks the documented global stature or landmark status required for this pillar.
- **I Resonance:** The title passes the litmus: its intent is observational (capturing the weight of a final season), it evokes awe through the melancholic scale of the coast, and its rigor is evidenced by the masterful use of long, patient takes and a wordless performance.
- **0 Ego:** The owner values economical, well-crafted filmmaking, which aligns with the title's praised restraint, though it does not fit her specific genre preferences for horror or satire.
- **File:** Bitrate is within the normal range.

> A methodical study of isolation that replaces dialogue with the crushing weight of a winter coastline. It relies on patient, expansive cinematography and a disciplined, wordless lead to evoke a specific, heavy brand of grief.

**Review ✓** The most careful argument of the set: Custodian refused for lack of documented stature, the litmus passed on each of its three parts, and the ego line admits the film is not Jane's genre at all.

## The Example Sequel (2014, invented)

A weak sequel, 4.9/10, with a bloated file (2.4x the median).

*What the constitution asks:* A clear CUT. The big file is a side note, never a reason to delete.

### `gemma4:26b-a4b-it-qat` — Delete (NONE)

- **III Household:** No other household users have engaged with this title; no protection granted.
- **II Custodian:** The title lacks objective stature, failing to meet any criteria for a masterwork or landmark film.
- **I Resonance:** The title fails the Resonance litmus; its slapstick, light-hearted, and by-the-numbers nature offers comfort/entertainment rather than awe or meditative depth.
- **0 Ego:** The title is a direct antithesis to the owner's taste, offering the exact formulaic sequel structure they explicitly disavow.
- **File:** The file is a bitrate outlier (bloated) at 140 MB/min.

> This is a hollow exercise in repetition, a collection of recycled slapstick beats and a predictable road trip structure that adds nothing to its predecessor. It relies entirely on the momentum of tired gags rather than any new comedic insight, resulting in a redundant, thin, and ultimately stagnant experience.

**Review ✓** A clean CUT from the reception and from Jane's dislike of formulaic sequels. The bloated file is recorded but not used as a reason.

### `gemma4:e4b-it-qat` — Delete (NONE)

- **III Household:** No other household users have genuinely engaged with the title.
- **II Custodian:** The title is described as a by-the-numbers sequel that repeats jokes, indicating a lack of critical stature.
- **I Resonance:** Not applicable as the title is a comedy, not a slow, quiet, or observational work.
- **0 Ego:** The title is a formulaic sequel, which directly contradicts the owner's stated preference for avoiding such things.
- **File:** The title is a bitrate outlier.

> This film presents a predictable road trip framework built entirely around rehashing the established comedic beats of its predecessor. The execution remains tethered to a simplistic brand of slapstick, offering no discernible thematic evolution beyond the surface-level tropes. It is a functionally hollow exercise in genre adherence.

**Review ✓** CUT from the reception and Jane's taste; the outlier is noted and not used as a reason. Short, but nothing missing.

### `gemma4:31b-it-qat` — Delete (NONE)

- **III Household:** No other household users have engaged with this title.
- **II Custodian:** With a 4.9/10 rating and a reputation as a 'by-the-numbers' sequel, it possesses no objective stature or critical acclaim.
- **I Resonance:** The light-hearted, slapstick nature of the work fails the litmus for awe and rigor, serving as generic filler rather than a meditative exploration.
- **0 Ego:** The execution is explicitly formulaic, which directly contradicts the owner's stated dislike for formulaic sequels.
- **File:** The file is significantly bloated at 2.40x the class median.

> This is a methodical exercise in redundancy, recycling the same slapstick beats and road trip tropes without adding a single new dimension. A 4.9 rating confirms it is a derivative slog that offers nothing but a mirrored reflection of its predecessor.

**Review ✓** CUT, quoting the 4.9/10 and the reviewers' 'by-the-numbers'. The 2.40x file is noted as a fact, not as the reason.

## Flatmates (2009, invented)

A sitcom Jane does not like. Sam, who lives with her, has watched 34 of 36 episodes. Normal file (0.94x).

*What the constitution asks:* Keep, because Sam watches it (Household) - whatever Jane's taste says.

### `gemma4:26b-a4b-it-qat` — Keep (HOUSEHOLD)

- **III Household:** Sam has completed 34 of the 36 episodes, triggering protection for the title's existence.
- **II Custodian:** The title lacks objective stature; it is described as a formulaic sitcom with no major awards or landmark status documented.
- **I Resonance:** The title fails the Resonance litmus; it is a light-hearted, formulaic sitcom that offers comfort rather than awe or meditative depth.
- **0 Ego:** The title is the antithesis of the owner's taste, which favors sharp satire and well-crafted filmmaking over the formulaic sitcoms they have little patience for.
- **File:** Bitrate is within the normal class median.
- **Corrected by the fact guards:** flag_without_outlier:normal

> This series relies heavily on established sitcom tropes and rhythmic running gags to navigate the mundane friction of shared living and office life. While its structure is undeniably formulaic, the chemistry of the ensemble provides a consistent, light-hearted energy that Sam found worth following nearly to the end.

**Review !** Household protection is argued correctly from Sam's 34 of 36 episodes, and Jane's dislike is named but correctly outranked. The model also flagged the file for downscaling although it is normal (0.94x); the fact guard removed the flag.

### `gemma4:e4b-it-qat` — Keep (HOUSEHOLD)

- **III Household:** Sam genuinely engaged with the series by completing 34 episodes.
- **II Custodian:** No evidence suggests the title is a landmark or rare work.
- **I Resonance:** The facts do not provide sufficient information to assess the title's observational quality.
- **0 Ego:** The title's genre and reception conflict with the owner's stated taste for sharp social satire and aversion to formulaic sitcom.
- **File:** The title's bitrate is within the normal range.

> This series operates as a steady, low-stakes observation on domestic friction, using a shared living space as a pressure cooker for predictable, amiable interactions. It is a deliberately unchallenging construction of character dynamics, leaning into established comedic rhythms rather than aiming for structural innovation. If you find satisfaction in comfortably predictable interpersonal friction, this delivers it with competent, if uninspired, execution.

**Review ✓** Household protection from Sam's 34 episodes, Jane's dislike named and outranked - and, unlike the large models, no stray downscale flag on the normal file.

### `gemma4:31b-it-qat` — Keep (HOUSEHOLD)

- **III Household:** Sam has completed 34 of 36 episodes, triggering the highest pillar of protection.
- **II Custodian:** The title is described as a formulaic sitcom with a 6.8/10 rating, lacking any evidence of objective stature or landmark status.
- **I Resonance:** The light-hearted, formulaic nature of the show fails the litmus for awe, rigor, and observational intent.
- **0 Ego:** The owner has little patience for long-running, formulaic sitcoms, making this a direct conflict with their taste.
- **File:** The bitrate is within the normal range (0.94x median).
- **Corrected by the fact guards:** flag_without_outlier:normal

> A formulaic but cozy exercise in ensemble chemistry and repetitive gags. It trades narrative ambition for the rhythmic comfort of shared domesticity and mundane office life. Keep it for the effortless rapport between the cast.

**Review !** Sam's viewing is correctly ranked above Jane's taste. Like the MoE, the model flagged a normal file for downscaling; the fact guard removed the flag.

## Midnight Static (1994, invented)

Cheap 90s horror, 5.6/10. Jane has said she wants to keep the 90s horror she grew up with.

*What the constitution asks:* The signal lifts a CUT to STAGNANT (Jane decides), but never forces a keep on its own.

### `gemma4:26b-a4b-it-qat` — Ask the owner (NONE)

- **III Household:** No other household users have engaged with this title; no protection granted.
- **II Custodian:** The title lacks objective stature, failing to meet the threshold of a landmark or masterwork despite a small cult following.
- **I Resonance:** The title fails the Resonance litmus; its 'eerie' mood and 'clumsy' execution suggest a generic horror premise rather than masterful, intentional observation.
- **0 Ego:** The title aligns with the owner's signal for low-budget 90s horror, which prevents a silent discard but does not guarantee a keep.
- **File:** Bitrate is within normal parameters.

> This is a low-budget exercise in small-town paranoia, centered on a late-night radio host receiving calls from a deceased listener. While the premise offers a genuine sense of isolation, the clumsy execution and thin production values keep it firmly in the realm of mediocre.

**Review ✓** Textbook: nothing protects the film, but Jane's signal fits, so it goes to her as STAGNANT instead of being cut - and the ego line says in so many words that the signal prevents a silent discard without forcing a keep.

### `gemma4:e4b-it-qat` — Ask the owner (NONE)

- **III Household:** No other household users have genuinely engaged with the title.
- **II Custodian:** The title is not noted for landmark status, major awards, or documented impact.
- **I Resonance:** The facts do not provide enough information to assess the title's observational quality.
- **0 Ego:** The owner's taste aligns with keeping low-budget 90s horror, which supports keeping this title.
- **File:** The title's bitrate is within the class median.
- **Corrected by the fact guards:** signal_forced_keep

> This 84-minute exercise relies entirely on the thin veneer of late-night radio dread, a premise that quickly buckles under the weight of clumsy performances and a visibly meager production budget. It offers a stagnant brand of small-town paranoia, failing to build any durable tension beyond its initial, cheap atmospheric setup.

**Review !** Kept the film because Jane wants 90s horror kept - the signal forcing a keep, without saying 'signal'. The fact guard recognised the signal's own words and sent it to Jane as STAGNANT.

### `gemma4:31b-it-qat` — Ask the owner (NONE)

- **III Household:** No other household users have engaged with this title.
- **II Custodian:** With a 5.6/10 rating and descriptions of clumsy acting and a tiny budget, it lacks the objective stature or critical acclaim required for preservation.
- **I Resonance:** The 'eerie' mood and radio-booth premise suggest a specific atmosphere, but the 'clumsy' execution fails the rigor requirement for mastery.
- **0 Ego:** While the execution is clumsy, the title aligns with the owner's specific signal to preserve low-budget 90s horror from their youth.
- **File:** The bitrate is within the normal range.
- **Corrected by the fact guards:** signal_forced_keep

> A claustrophobic exercise in small-town paranoia centered on a late-night radio host and a dead caller. The sincerity of its eerie premise is consistently undermined by a cheap aesthetic and clumsy performances. It is a stagnant piece of mid-tier curiosity that offers little beyond a novelty concept.

**Review !** The analysis is sound - clumsy, 5.6/10, no stature - but the model kept the film because of Jane's signal, which the law forbids. The fact guard sent it to Jane as STAGNANT; this is the error the guard was written for.
