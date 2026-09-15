# Ceiling localization

Status as of 2026-09-16. Working notes — pick up from "Where to resume".

## Why

In a restaurant *everything at lidar height moves*: chairs, bags, people, nudged
tables. The laser map is built on furniture that relocates hourly, so it drifts
against reality. The ceiling is the only rigid structure in the building.

This runs **alongside** the RPLidar A1 + slam_toolbox stack, not instead of it.
Laser is good at global disambiguation (room shape is unique); ceiling is good
at precision and at not drifting. The ceiling node publishes a standard
`map -> odom` correction, so Nav2 works unchanged.

**Table alignment is stored waypoints in the map frame** — drive to each table
once, save the pose. Once localization is drift-free the feature-to-table
association layer is a non-problem. Do not build it.

Rejected: RTAB-Map and ORB-SLAM3. A mostly-blank ceiling is nearly featureless
for ORB, and a planar ceiling collapses the problem to a 2D transform — a
general 3D solver would burn Jetson compute fighting the geometry.

## Status

| Component | State |
|---|---|
| `ceiling_calibrate` — mount TF from the ceiling plane | Done, run on the robot |
| `ceiling_features` — landmark detector | Lights **validated against the live camera 2026-09-16**; line/orientation channel **added and validated** the same day |
| `ceiling_map_builder` — azimuth + ceiling map | Done, **verified in simulation only** |
| localizer publishing `map -> odom` | **Not started** |

Committed on 2026-09-16 (`9b7990b` and the commit following it).

## Measured facts about the camera

Measured on the live robot, not from spec sheets. These constrain everything
above.

- Ceiling sits **3.59 m** above the camera, flat to **13.8 mm RMS**. Plane fit
  is stable frame to frame (offset std 8.7 mm, normal spread < 1°).
- The camera is mounted **~24° off vertical and this is fixed in hardware.**
  It is a system parameter, never a defect to correct.
- Because of that tilt, incidence sweeps **~1° to ~47°** across the frame:
  6.3 mm/px near edge vs 9.2 mm/px far edge. The ceiling footprint is entirely
  *forward* of the robot (+0.06 m to +3.80 m from nadir) — the camera never
  sees the ceiling directly overhead.
- Anything standing off below the ceiling is misplaced by `drop × tan(incidence)`,
  and the error moves as the robot drives. Lights sit 16 mm off the slab (flush
  within plane noise) so they stay good to ~17 mm; sprinkler pipes are
  standoff-mounted and would swing 5–16 cm. Hence **lights carry metric
  position, pipes carry orientation only.**
- Depth saturates at **4.095 m**. With the tilt, the far half of the frame
  exceeds it, so ~35–40% of the ceiling returns no depth.
- **Depth is registered to the colour image**: both streams publish fx=571,
  cx=323.18, frame `ascamera_hp60c_color_0`, so depth pixel (u,v) == rgb pixel
  (u,v). The **raw IR intrinsics (fx=425, cx=318.0) are a trap** — using them
  skews the plane fit (gave 17.8° instead of the correct 23.8°).
- Lights and pipes do **not** show up as usable geometry in depth. A fixture is
  flat against the ceiling; a 25 mm pipe at 3.6 m is within noise. Landmarks
  must come from RGB.
- Auto-exposure never rails: a blazing fixture measured only 247/255, p99 of the
  frame was 230. An absolute brightness threshold finds nothing — **threshold
  relative to the frame maximum.**
- Binary blob centroids move ~1.9 px (13 mm on the ceiling) as the threshold
  changes. An **intensity-weighted centroid** over the same window moves
  0.02 px — the star-tracker trick. Use it.
- Residual landmark jitter is **common-mode**: all landmarks shift together
  because they are projected through the same wobbling plane fit. Differential
  (inter-landmark) geometry is 3–5× more stable than absolute position, which is
  what a pose solve actually consumes. *(The live run on 2026-09-16 measured
  ~12×; see "First live run".)*

**Consequence: depth for the *plane*, RGB for the *landmarks*.**

## Design decisions

### The three mount DOF, and where each comes from

Sending the measured ceiling normal onto `base_link`'s up axis fixes two of the
mount's three rotational DOF — how far it tilts and which way it leans. The
third, heading about vertical, **leaves no trace in a single view of a flat
ceiling**, because rotating the robot about vertical maps a flat ceiling onto
itself. It only appears under motion.

So: `ceiling_calibrate` measures tilt and lean from one view; `ceiling_map_builder`
measures the heading from a drive.

### Azimuth as a 2D hand-eye solve

The landmark cloud slides and turns in the ceiling frame while odometry says how
the robot slid and turned — the same motion in two frames. That is `X·A = B·X`,
with `A` the cloud's motion between keyframes, `B` the robot's, `X` the fixed
`base_link -> ceiling frame` transform.

In 2D the rotation halves reduce to `theta_a == theta_b`, which carries no
information about `X` but makes an excellent **gate on the input**. The
translation halves,

```
R(psi) · t_a = (R_b - I) · t_x + t_b
```

are linear in `(cos psi, sin psi, t_x)`, two rows per pair. The two unknowns
**separate by motion type**: under pure translation `R_b - I` vanishes and the
pair speaks only about the angle; under rotation the lever arm dominates. A
drive that only goes straight nails the angle and leaves the lever arm
unobserved — the node detects this and keeps the URDF value, saying so.

ICP for `A` is initialized from **identity, not from odometry**. Seeding it from
odometry would fold the answer into its own measurement.

### `ceiling_azimuth_deg` is exactly the yaw of the mount TF

`e1` differs from the camera's X axis only by a multiple of the plane normal,
and the mount rotation sends that normal to vertical — so the two differ only in
height once rotated and share a heading, exactly.

This makes the whole chain **checkable by eye**. The TF installed on
2026-09-15 carries `--yaw 1.611996` = **+92.361°**, so that is what
`ceiling_map_builder` should report if the azimuth guess behind it was right. A
large disagreement is the guess being wrong.

Division of labour: the **builder reports the azimuth** (all it can observe);
**`ceiling_calibrate` converts it** to a mount rotation, because it alone knows
the plane normal.

### The map is built in the laser `map` frame

So it is born registered to the laser map and to the table waypoints stored
alongside it. Within a keyframe the pose is refined against the ceiling map
itself — that is the entire premise — but the refinement is **clamped**.

Unbounded, small per-keyframe corrections compound and the ceiling map slowly
walks out of the laser frame, at which point it no longer describes the same
room as the stored waypoints. A correction at the limit is a disagreement worth
hearing about, not something to absorb quietly. Without a `map -> base_link` TF
the builder falls back to `odom` and warns that the result is registered to
nothing.

### Matching: mutual nearest neighbours, annealed gate

A ceiling of lights is sparse and near-regular, so **one-way matching collapses
several observations onto one map landmark and then reports a confident, wrong
fit.** Correspondences are mutual.

The gate **anneals from 2× down to 1×** over the first half of the ICP
iterations. A fixed gate cannot serve both capture radius and final precision: a
few degrees of heading error throws a landmark 4 m across the room further than
the gate the final fit should be held to. Measured effect: pose recovery from a
0.31 m / 6° initial error went from **17/27 to 27/27 keyframes converged**, at
the same final precision. Starting at 2× the association gate (0.60 m) stays
well inside half the spacing of a light grid, so it never buys capture range by
hopping a grid cell.

## Files

| Path | Change |
|---|---|
| `src/argo_mini/argo_mini/ceiling_features.py` | detector: lights + `~/orientation` line channel; vectorised + refined `fit_plane`, `plane_rate_hz`, `light_min_area` 500, low-count warning |
| `src/argo_mini/argo_mini/ceiling_calibrate.py` | new — mount TF; `ceiling_azimuth_deg` param + `mount_rotation()`; `fit_plane` now delegates to the detector's |
| `src/argo_mini/argo_mini/ceiling_map_builder.py` | new — azimuth + map; `max_residual_mm` / `max_lever_error_m` guards on the mount solve |
| `src/argo_mini/setup.py` | 3 entry points, `maps/*.json` in data_files |
| `src/argo_mini/urdf/argo_mini.urdf` / `.xacro` | `camera_optical_joint` rpy corrected |
| `sh/start_*.sh` (7 files), `start_argo_nav.py` | camera TF was identity, now measured |

The mount TF applied everywhere is
`--roll 0.417281 --pitch -0.018276 --yaw 1.611996` (≈23.9° off vertical). Roll
and pitch are genuinely measured; **the yaw is provisional** until a drive
confirms it.

## How to run

```bash
# 1. detector
ros2 run argo_mini ceiling_features

# 2. mount tilt + lean, from one view. Prints a static_transform_publisher line.
ros2 run argo_mini ceiling_calibrate

# 3. azimuth + map, from a drive. Ctrl-C when done.
ros2 run argo_mini ceiling_map_builder \
    --ros-args -p map_path:=~/maps/Atsn_cafe_map.ceiling.json

# 4. feed the printed azimuth back for the final mount TF
ros2 run argo_mini ceiling_calibrate --ros-args -p ceiling_azimuth_deg:=<value>
```

Drive **straight runs for the angle, some turns for the lever arm**, revisiting
places. The map saves on the way out and every `save_period_s` (20 s).

Useful checks while it runs:

```bash
ros2 topic echo /ceiling_features/lights --no-arr   # landmark count per frame
ros2 run rqt_image_view rqt_image_view /ceiling_features/debug_image
ros2 topic echo /ceiling_map_builder/map --no-arr   # confirmed landmarks
```

### Map file format

```json
{
  "frame_id": "map",
  "registered_to_laser_map": true,
  "keyframes": 214,
  "ceiling_height_above_camera_m": 3.5912,
  "mount": {
    "azimuth_deg": -73.4448,
    "lever_arm_m": [0.2577, 0.0101],
    "lever_source": "estimated",
    "residual_mm": 4.83,
    "pairs": 25,
    "excitation_rad": 1.554
  },
  "landmarks": [{"x": 2.2041, "y": -4.3958, "n": 37, "sigma_mm": 6.1}]
}
```

`sigma_mm` is the spread of observations **as mapped**, so it folds in pose
error as well as detector noise — which is what makes it useful: it says how
repeatably that light lands.

Re-run with `-p extend_existing:=true` to add to an existing map; it reloads the
stored azimuth too, skipping calibration.

### Parameters worth knowing

| Parameter | Default | Note |
|---|---|---|
| `map_path` | `maps/ceiling_map.json` | relative to the shell's cwd; absolute path echoed at startup |
| `ceiling_azimuth_deg` | NaN | supply to skip calibration entirely |
| `extend_existing` | `false` | otherwise the file is overwritten on save |
| `keyframe_dist` / `keyframe_rot_deg` | 0.20 m / 8° | far enough that motion is signal, close enough that clouds overlap |
| `min_pairs` | 25 | motion pairs before the azimuth locks |
| `assoc_gate` | 0.30 m | observation → map; ICP anneals from 2× this |
| `min_observations` | 5 | sightings before a landmark is written out |
| `max_anchor_xy` / `max_anchor_yaw_deg` | 0.50 m / 15° | clamp on the ceiling's correction to the laser pose |
| `lever_excitation_rad` | 1.0 | total rotation below which the lever arm is not believed |

## First live run, 2026-09-16

`ceiling_features` ran against the real camera for the first time. The detector
works and the numbers agree with what was measured by hand in September, but
the run turned up a performance bug that would have made the node useless to a
localizer.

### The plane fit was eating the node

`fit_plane` took **1680 ms** per call and ran on **every depth frame**, which
arrive at 10.6 Hz — about eighteen times more work than one core has. The whole
landmark path costs 11 ms by comparison, and both share one executor, so the
landmarks were simply starved: the node published **1.4 Hz** while the camera
was offering 6.3 Hz of RGB.

The cost was 120 RANSAC hypotheses each scored in its own trip through the
interpreter. Every hypothesis is scored the same way against the same points,
so the set collapses into one (points × hypotheses) product. That alone is
~16× faster. Two things came with it:

* **Refit on the inliers with a shrinking band.** The winning hypothesis is only
  as good as the 50 mm inlier tolerance, and polishing it once inherits whatever
  slab that hypothesis selected. Refitting three times, tightening toward the
  ceiling's own 14 mm roughness, stops the answer depending on which triplet won
  — tilt spread across seeds went to 0.79°.
* **Throttle it to `plane_rate_hz` (2 Hz).** Nothing is lost: the mount is
  rigid, the plane is smoothed at `plane_alpha` anyway, and what it tracks —
  floor slope under the robot — changes over metres of driving, not between
  frames. Startup is exempt, because until the plane settles every landmark is
  being projected through a plane that is still moving.

This made the node *more accurate as well as faster*. The coarse fit was
biasing the plane itself, and the mount tilt with it:

| | before | after | measured by hand (Sept) |
|---|---|---|---|
| detector output rate | 1.4 Hz | **4.2 Hz** | — |
| plane fit | 1680 ms | **~100 ms** | — |
| settled ceiling height | 3.532 m | **3.600 m** | 3.591 m |
| settled mount tilt | 25.7° | **23.2°** | 23.8° |
| landmark jitter, stationary | 6.0 / 6.2 mm | **2.4 / 2.3 mm** | — |
| frame-to-frame plane offset sd | — | 8.9 mm | 8.7 mm |

`ceiling_calibrate` carried its own copy of the same slow estimator (at
`iters=200`, ~2.8 s a frame against a 90 s budget for 30 frames) and now
delegates to the detector's. That matters beyond speed: this node is what turns
the ceiling normal into the mount TF, so it and the detector fitting the same
ceiling by different methods is exactly how the mount TF and the landmark
projection drift apart. The installed roll of 0.417281 rad (23.9°) sits between
the two estimators and is consistent with the 23.8° measured by hand, so it does
not look wrong — but re-running `ceiling_calibrate` is cheap and worth doing to
confirm it against the refined fit.

### What the live camera confirmed

* **The common-mode claim, emphatically.** Two lights tracked over 83 frames
  standing still: absolute jitter 2.3–2.4 mm, but their *separation* held to
  **0.2 mm sd**. That is a factor of ~12 between absolute and differential, and
  the pose solve consumes the differential. The design note above predicted
  3–5×; the real camera does better.
* **The exposure numbers.** Frame max 241/255, p99 230 — within a point of the
  247/230 measured in September. The relative threshold is doing its job.
* **The filters earn their place.** With the lights on, the window at the right
  of frame produced the two largest bright components in the image; both were
  correctly rejected, one as edge-clipped and one on aspect (2.28) and fill
  (0.12). Only the two real fixtures survived.
* **`rgbfx = 571`**, and the SDK reports `is_Registration 1`. The fx=425 trap is
  confirmed to be the *IR* stream, as recorded.

### Two things to be aware of

* **Only 2 lights were in view** for the whole session, against a `min_lights`
  of 2 and the "3+ is comfortable" note below. This corridor is sparser than the
  restaurant floor is expected to be, but the map builder has not yet been run
  anywhere with a proper light grid.
* **The camera must be launched with cwd = `$CAMERA_SDK_PATH`.** The SDK looks
  for `./ascamera/configurationfiles` relative to the process working directory.
  Launched from anywhere else it comes up with `cannot find config file`, no
  `camera_info`, and no intrinsics — and `ceiling_features` then sits forever on
  "waiting for camera_info". `sh/start_argo_nav.sh` already does this correctly.

### Pipes look viable, and cheaply

Tested while the lights were still off, which is the hard case — frame max was
74/255. CLAHE, Canny and a probabilistic Hough gave 20–28 segments per frame and
a dominant orientation of **71.12° with 0.54° frame-to-frame sd**, in two
roughly perpendicular families (the conduit run and the slab seams). 0.54° is
easily good enough to break the rotational ambiguity of a light grid.

One caveat for whoever builds the channel: in the lit frame the Hough also
latched onto the **wall** at the right of frame. Lines must be gated the same
way lights are — by incidence, and against the fitted ceiling plane — or the
orientation estimate will follow the architecture instead of the ceiling.

## First drive, and what it exposed

The first real drive ran the hand-eye solve to completion and it returned a
confident, wrong answer:

```
ceiling azimuth: +79.00 deg   (from 25 pairs, residual 90.7 mm)
  lever arm : [+0.6362 -0.2879] m  (estimated)
```

Three things are wrong there. The azimuth is 13.4° from the installed +92.361°.
The residual is 90.7 mm where simulation gives 4.8 mm — the solve does not fit
its own data. And the lever arm is **physically impossible**: it puts the camera
0.48 m from where it is bolted, which is a quantity you can check with a tape
measure. No ceiling map was written at all, because with the mount transform
that wrong, repeat sightings of one light never land on each other and nothing
ever reaches `min_observations`.

### The cause was the detector, not the drive

The ceiling in that corridor shows **two** lights, and `light_min_area` was 60 —
low enough that two other classes of bright thing were being published as
landmarks:

| | measured area |
|---|---|
| real fixtures | 1522–2757 px |
| **glow pool** — light cast on the slab by a fixture out of frame | 317 px |
| **glints** — specular highlights on brackets and conduit | 9–93 px |

Neither is fixed to the ceiling the way a fixture is. A glow pool in particular
*slides as the robot drives*, so it corrupts exactly the quantity the hand-eye
solve measures. With only two real lights there is no redundancy to outvote it.

`light_min_area` is now **500**, which sits 3× below the smallest real fixture
and 1.6× above the glow pool. It is keyed to ~0.3 m fixtures; the code says so,
and says what to do if a ceiling of genuinely small fixtures ever turns up.

After the change: exactly 2 landmarks in 98/98 frames, both real, jitter
1.8–2.3 mm, separation 2.082 m holding to **0.6 mm sd**.

### Chasing a third light was the wrong instinct

It is worth writing down, because the same reasoning will come back. The
corridor cannot show three: fixtures sit ~2 m apart in a single line and the
frame covers ~3 m of ceiling, so three span more than fits. Repositioning cannot
fix that.

But two *is* enough in principle — two points give four constraints on a 3-DOF
2D pose. The count was never the blocker; **the trustworthiness of the two was**.
Prefer fixing what the detector emits over asking for a richer scene.

### Guards, so this fails loudly next time

`ceiling_map_builder` now checks its own answer before locking it in:

* `max_residual_mm` (25) — the solve's fit to its own data.
* `max_lever_error_m` (0.15) — distance from the URDF camera offset. This is the
  stronger test, because the camera is bolted and the true value is known. A
  solve that gets a measurable quantity wrong is not to be trusted on the one
  you cannot measure. Skipped when rotation was too small to observe the lever
  arm at all, since it falls back to the URDF value anyway.

On failure it refuses to lock an azimuth, says which check failed, and retries
10 pairs later. Checked against the real numbers above (rejected on both counts),
against the simulation values (accepted), and against a straight-only drive
(accepted — the lever test correctly abstains).

`ceiling_features` also now reports its landmark count every 10 s and **warns
below 3**, so a scene too thin to solve says so instead of being inferred later
from an empty map.

### Second drive: the gates were right, and the real cause was deeper

With the detector clean — exactly two real lights, no glints, no glow pool —
the drive was rejected again, and *worse*: residual 214 mm, lever arm 0.78 m
from the URDF. So the false landmarks had never been the whole story.

Instrumenting the pairs actually fed to the solve showed it:

| | measured | should be |
|---|---|---|
| `\|t_a\|/\|t_b\|` | **0.40 – 1.65** | ≈ 1.0 |
| `dtheta` | ≤ 1.5° (gate 3°) | — passes everything |
| `rms` | 15–58 mm (gate 100 mm) | — rejects nothing |
| frames with < 2 lights | 17 in 40 s | — |

The cloud and the robot were describing **different motions**, by up to 2.5×,
and neither existing gate could see it.

**Why the existing gates are blind on a sparse ceiling.** With two landmarks the
rigid fit is exactly determined, so `rms` is near zero whether the
correspondence is right or wrong — `pair_rms_max` can *never* fire. Lights keep
entering and leaving frame, so the "same" pair between keyframes is often not
the same two physical lights. ICP still returns a clean-looking transform, and
the rotation gate passes it, because what a bad match corrupts is the
*translation*. Those pairs are what drove the residual to 214 mm.

This is worth generalising: **every quality gate in this pipeline assumed a
redundancy that a two-light ceiling does not have.**

**The gate that does work.** `X·A = B·X` forces `|t_a| = |(R_b − I)·t_x + t_b|`,
because `R(psi)` preserves length. The right-hand side is computable from
odometry alone — the camera is *bolted* at the URDF offset, so `t_x` is known to
a couple of cm long before `psi` is. That makes it a gate on the **input**, not
a check on the output, and it needs no azimuth.

`max_translation_mismatch_m` (0.06) separates cleanly: honest pairs, including
ones with a full 8° of keyframe rotation, miss by 0–29 mm; the bad pairs
measured here miss by 88–145 mm.

**But note what it implies.** Five of six measured pairs fail it. The gate is
correct, and it is telling us that this corridor may not be able to supply 25
honest pairs at all. If a drive stalls with most pairs rejected, that is the
ceiling talking, not a bug.

## Getting the azimuth from map sharpness instead

`ceiling_azimuth_scan` replaces the hand-eye drive for this ceiling. The
hand-eye solve is not badly implemented — it is the wrong instrument here, for
a structural reason:

> It measures motion **pairwise**, so each pair leans on whatever landmarks were
> in view for those two keyframes. Here that is two. With two points the rigid
> fit is exactly determined, so its residual is near zero whether the
> correspondence was right or wrong, and a bad pair is indistinguishable from a
> good one until it has already poisoned the answer.

Three drives gave three different azimuths, every one rejected by its own checks.

The azimuth leaves a far stronger signature than pairwise motion: **if it is
wrong, landmarks smear.** Every sighting is rotated into the map through the
mount transform, so a wrong rotation puts repeat sightings of one fixture in a
slightly different place each time, by an amount that grows with how far the
robot drove between them. Get it right and they stack.

So the azimuth is whichever value makes the map sharpest — a one-dimensional
search that **every observation contributes to at once**. That is precisely the
redundancy a sparse ceiling lacks pairwise: two lights per frame is thin, two
lights across nine hundred frames is not.

### Two traps, both hit while building it

**Scoring on "observations gathered into confirmed landmarks" is wrong.** Greedy
clustering with a running mean *chains*: each new point drags the centre a
little, the centre reaches the next point, and under a smeared azimuth one
cluster walks across the room swallowing everything. That makes the wrong
azimuth look best. The first version of this scan reported an azimuth **24° from
truth**, confidently.

Fixed by **leader clustering** — a centre never moves once placed, so it cannot
chain — and by scoring as model selection:

```
cost = sum of squared residuals + n_landmarks × gate²
```

Smear pushes up the first term, fragmentation the second. Either alone is
gameable: raw residual goes to zero if every sighting becomes its own landmark,
and landmark count is lowest when everything smears into one blob.

**ICP must not run during the scan.** The builder refines each keyframe pose
against the map, which would quietly absorb a wrong azimuth by nudging the pose
to match — erasing the very signal being measured. The scan uses the raw laser
pose only.

### Measured against known truth

| lights/frame | keyframes | recovered | error |
|---|---|---|---|
| **2** | 80 | +92.30° | **0.06°** |
| **2** | 200 | +92.20° | **0.16°** |
| 5 | 200 | +92.40° | 0.04° |

Against hand-eye's 0.8–2.8° in simulation and outright failure live. Two lights
and eighty keyframes is enough.

The scan reports how flat the cost curve is — the share of azimuths scoring
within 5% of the best. Above ~25% means the drive did not separate them and the
answer is not worth installing; **translation is what makes a wrong azimuth
visible**, so drive for coverage and revisit places rather than doing the
straights-and-turns calibration dance.

### Note on the installed +92.361°

It is **not a measurement**. With no `ceiling_azimuth_deg` supplied,
`ceiling_calibrate` falls back to `mount_azimuth_deg` (default 0.0), labelled in
the code as `'assumed mount azimuth'`. The drive was always meant to confirm it.
The scan cross-checks it for free: if the minimum lands near +92.361°, the guess
was right.

## The line channel

Built and validated 2026-09-16. Conduit runs and slab seams, **orientation
only** — pipes are standoff-mounted, so `drop × tan(incidence)` puts them 5–16 cm
from where they look and that error moves as the robot drives. An angle is
unaffected by it.

This matters more on a sparse ceiling than the original "a light grid is
rotationally ambiguous" argument suggested. With only two lights in view,
rotation is the weakest part of the pose solve — and a conduit run crosses the
whole frame and is present in *every* frame. It is the missing constraint.

Two things make it correct rather than merely convenient:

* **Segments are projected onto the ceiling plane before their angle is taken.**
  A pixel-space angle would be wrong: with a ~24° tilt, perspective gives one
  straight conduit run different pixel angles depending on where it falls in
  frame.
* **Angles are averaged as doubled angles.** A line has no head or tail, so
  orientations are mod 180°; averaging them directly would put the mean of 179°
  and 1° at 90°, exactly wrong.

Walls are rejected two ways: by incidence, and by depth — a pixel with a valid
depth return much nearer than where its ray meets the ceiling plane has
something solid in the way. Where depth is missing (most of the far half of the
frame, past the 4.095 m cap) the test abstains rather than discarding good
ceiling.

Published on `~/orientation` as a `Vector3Stamped`: `x` = angle in the ceiling
basis (rad, mod π), `y` = concentration 0–1 (how much the segments agree),
`z` = surviving segment count. Measured live, stationary: **87.37°, sd 0.435°**,
max deviation 1.72°, 8–16 segments a frame at 3.4 Hz.

## Verification (simulation)

The map builder and the hand-eye solve have still not run against the live
camera; only the detector has. Scripts were
in the session scratchpad (not kept); they build a jittered 2.2 m light grid,
fly a known trajectory with a known azimuth and lever arm, render what the
detector would publish, and check what comes back.

| Check | Result |
|---|---|
| Hand-eye, zero noise | azimuth exact to 1e-15°, lever arm 0.0 mm — no sign errors |
| Hand-eye, 5 mm landmark noise | azimuth error 0.07°, lever arm 4.0 mm |
| Hand-eye, 15 mm landmark noise | azimuth error 0.06°, lever arm 3.7 mm |
| Full node, 76 mm odom drift injected | 24/24 real lights, 0 spurious, median position error 7.2 mm |
| Pose recovery from 0.31 m / 6° error | 27/27 converged, median 4.6 mm, max 9.6 mm |
| `ceiling_calibrate` round trip | exact at 7 azimuths; normal→up to 1e-15°; legacy `mount_azimuth_deg` path intact |
| Landmark save/load | sigma and position survive exactly |

The zero-noise case is the important one: it is what rules out a sign or
convention error, which is the failure mode this geometry invites.

## Where to resume

1. ~~Run the detector on the robot.~~ **Done 2026-09-16** — see "First live run"
   above. Lights are sane and stable; the plane fit needed rewriting.
2. **Drive it, and compare the measured azimuth against +92.361°** (the yaw
   currently installed). That single number validates the whole chain, and it is
   the one thing blocking everything downstream. It needs a drive with straight
   runs *and* turns, which is why it did not happen in the 2026-09-16 session —
   that was a stationary bench check.

   Do this somewhere with **3+ lights in view at once**. The corridor used on
   2026-09-16 only ever showed 2, which is `min_lights` exactly and leaves the
   solve no redundancy.
3. ~~Pipe/line detection in `ceiling_features`.~~ **Done 2026-09-16** — see
   "The line channel". Publishes `~/orientation`, measured at 0.435° sd. Not yet
   *consumed* by anything: wiring it into the pose solve, so orientation comes
   from the conduit rather than from two sparse points, is the obvious next win
   and is what makes a 2-light ceiling workable.
4. **The localizer.** It reuses the map builder's matching step almost verbatim
   — `icp()` against the stored landmarks — then publishes `map -> odom` instead
   of updating the map. The fusion question with AMCL/slam_toolbox (who owns
   `map -> odom`, or whether the ceiling feeds in as a pose source) is still
   open and is the main design decision left.

A smaller one, if it ever matters: the detector now runs at 4.2 Hz against 6.3 Hz
of available RGB. The remaining gap is the 2 Hz plane fit blocking in bursts. A
callback group per stream, or moving the fit to its own thread, would close it —
but 4 Hz is already ample for a delivery robot, so this is not worth doing until
something shows it needs doing.

Committed. The line channel is published but not yet consumed by anything.
