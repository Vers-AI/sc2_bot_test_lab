# Lab Roadmap

## Next: Usability Pass + PiGBot Onboarding (week of Oct 5, 2026)

Decided 2026-10-03 (Drekken + Praxis). Sequenced after the BOC 11 video ships.

### 1. Generalize experiments into a Compare view

The experiment dashboard (commit b94b919) hard-codes the Oct 2026 patch
experiment: `_condition_of()` maps map names to "A: Pre-patch" /
"B: Patch 5.0.16". A stranger running their own experiments gets a
dashboard that cannot chart their matches.

The fix: experiments are **test-group pairings**. Pick any two groups as
A/B (e.g. `/test_lab/compare/?a=3&b=4` or a small picker); the dashboard
derives survival rate, avg + median durations, the win/loss duration
split, and fast-loss % from the selected groups. Map names stop mattering;
the group is the unit.

- [ ] Compare view: select any two test groups as A/B
- [ ] Derive all stats from group data (no map-name coupling)
- [ ] Nav: fold the top-level Experiment tab into Results -> Compare
      (alongside Test Groups and Maps); remove the standalone tab
- [ ] Group drill-down stat cards stay as-is (already generalized)
- [ ] README: document how a stranger defines their own experiment
      (create two groups, open Compare)

### 2. Onboard PiGBot as the next test subject

- [ ] PiGBot (Versus_bot flagship) enters the lab alongside the nav
      restructure so the first new test subject and the usability pass
      land as one coherent change
- [ ] PiGBot calibration series (20-30 games) to measure local step-time
      distribution against the Speed Budget tiers (20ms gate / 10ms
      performance)
