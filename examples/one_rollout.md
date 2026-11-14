# One example rollout

Same run as `python3 scripts/run_example_rollout.py`. No API key. The tools are scripted so the story stays the same. The search code is the real one.

The block hangs when the other side stops taking data. The fix must stay at or under 100 cells. Ripple is not told which tool to call.

Each round tries three ideas with one tool call each. A hard rule can kill one. Then only one survivor is played all the way out. Later rounds can go back and play out a different one.

**Round 1.** Three first calls:

- Lint stays alive. Judge guess 0.82.
- Full place-and-route is killed. Judge guess was 0.90. The checker ignores it.
- Simulation stays alive. Judge guess 0.55.

No play-out yet, so the formula picks lint, the higher guess. Played out, lint never fixes the hang. Real score 0.15. Mixed score 0.48.

**Round 2.** Simulation was never played out, so the formula has to give it a turn before it can stick with lint. Played out, this sample still does not finish the fix. Real score 0.20. Mixed score 0.38. Lint is still ahead.

**Round 3.** The search digs into lint and tries three next calls. It plays out the best guess, another simulation. The hang is still there. Score 0.20.

**Round 4.** It goes back to the original simulation branch and tries three next calls:

- An edit that waits until the other side is ready. Played out to 0.92. Hang gone, 80 cells, 2 cycles.
- An edit that adds extra registers. Killed. 140 cells is over the cap. The judge's 0.60 does not save it.
- Open a waveform. Left sitting, not played out yet.

Later rounds play out the ideas that were skipped, including the waveform. None of them beat 0.92.

The first move it keeps is simulation, not lint. Lint looked better after the first play-out. The search came back, tried the other branch, and found the real fix there.
