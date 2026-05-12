# `report.xlsx` metric semantics

Quick reference for what each dwell-time column in `report.xlsx` actually
counts. Written 2026-05-12 after a customer-facing investigation traced
a "suspiciously long Area dwell" back to a perfectly correct computation
that just doesn't mean what the name first suggests.

The implementation lives in [`core/event_exporter.py`](../core/event_exporter.py)
(`compute_area_pairings`, `_zone_summary_row`, `_area_summary_row`).

# ======================================
# -------- 1. THE FOUR DWELL FLAVOURS --------
# ======================================

| Where it appears                       | What "dwell" means                                                                                                   |
|----------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `Zones` sheet, scope=`object`, dwell_sec | Time the object was continuously inside **that one zone polygon** during this enter→exit pair.                       |
| `Summary` sheet, `Zone_N` row, *_dwell_sec | Aggregate (avg / min / max) over all per-object dwells in that zone across the whole session.                       |
| `Summary` sheet, `Area` row, *_dwell_sec | **Pathway-only** time per visit — see §2 below. Sum of segments where the object was inside the entrance/exit area **AND outside any zone**. |
| `Lines` sheet                            | No dwell column. Lines are crossings, not durations.                                                                  |

The four can disagree on the same physical visit. That's intentional and
not a bug; the rest of this doc explains why.


# ======================================
# -------- 2. AREA DWELL IN DETAIL --------
# ======================================

The **Area** scope models a parking lot:

- **Pathway** = the road through the lot (the area between the entrance
  and exit lines, *outside* every zone polygon).
- **Zones** = the parking spots themselves (each polygon = one spot or
  a group of spots).
- **Visit** = one full pass through the lot, starting with an
  entrance-line crossing and ending with an exit-line crossing.

`Area dwell_sec` answers: **"how long was this vehicle on the road
between zones during one visit?"** Not "how long on the lot total".
The metric pauses whenever the vehicle enters a zone, resumes when it
leaves. Each visit's dwell is the **sum** of all such pathway segments,
not the longest one.

## Worked example — `car#46` from `4Market_RF-DETR_phase_2_v1` (May-12)

The Zones sheet for `car#46` records six zone transitions inside one
entrance→exit visit:

```
10:26:46  IN Entrance   (visit starts; seg_start = 10:26:46)
10:26:53  ENTER Zone_3  (+7 s pathway; pause)
10:26:56  EXIT  Zone_3  (resume)
10:26:56  ENTER Zone_4  (+0 s pathway; pause — same frame)
10:27:19  EXIT  Zone_4  (resume)
10:27:27  ENTER Zone_4  (+8 s pathway; pause — re-entered same zone)
10:30:14  EXIT  Zone_4  (resume)
10:30:18  ENTER Zone_3  (+4 s pathway; pause)
10:30:22  EXIT  Zone_3  (resume)
10:30:32  IN Exit       (+10 s pathway; visit ends, pairing="matched")
                        -----
                        29 s   ← matches the 28.81 s reported as Area max_dwell
```

If the metric were defined differently:

| Alternative semantics             | Would have reported |
|-----------------------------------|---------------------|
| Longest single pathway segment    | 10 s                |
| Raw entrance→exit gap             | 226 s               |
| **Actual: sum of pathway segments** | **29 s** ≈ 28.81    |


# ======================================
# -------- 3. WHY A LONGER `Area max_dwell` USUALLY MEANS BETTER DETECTION --------
# ======================================

A car that browses several parking spots before parking will rack up
many short pathway segments. The Area dwell sums them — so the dwell
reflects "indecision time" / "browsing time", not parked time.

Counter-intuitively, **a detector that misses in-zone frames produces
SHORTER apparent `Area dwell`** for the same physical visit, because
no `zone_enter` events get logged → no pause → the entire entrance→exit
gap gets attributed to pathway, but the next visit by the same car
might never see a zone enter and so look like a shorter trip overall.

So when comparing two detectors against the same video:

- **Lower `Area max_dwell` ≠ better detection.** It can mean the detector
  misses zone entries.
- **Higher `Area avg_dwell` may indicate** the detector caught more zone
  transitions in real "browsing" visits.

Trust **`Zone_N enter_count` + per-zone `dwell_sec`** more than
`Area max_dwell` when judging detector quality.


# ======================================
# -------- 4. PAIRING QUALITY (the `pairing` column) --------
# ======================================

Each visit's row has a `pairing` value telling you why the visit closed:

| pairing       | Visit ended because…                                                       | Counted toward dwell stats? |
|--------------:|----------------------------------------------------------------------------|----------------------------:|
| `matched`     | Exit-line crossing in the expected direction.                              | ✓                           |
| `still_inside`| Session ended (export time) before the object left — dwell is a lower bound. | ✓ (folded in)              |
| `lost_id`     | Tracker dropped the ID before any exit crossing.                            | ✗ (excluded — exit time unknown) |
| `reversed`    | Crossed a strict entrance/exit line in the wrong direction (real reverse driving OR camera shake — indistinguishable from event data alone). | ✗ |

The `*_count` columns in the Summary row tell you how many of each
landed in this session, so you can sanity-check whether the aggregate
stats are based on a representative population.


# ======================================
# -------- 5. WHEN TO TRUST WHICH METRIC --------
# ======================================

| Customer question                                              | Use                                                |
|----------------------------------------------------------------|----------------------------------------------------|
| "How many cars came in/out this hour?"                         | `Lines` sheet `in_count` / `out_count` (peak hour) |
| "How long do people typically park here?"                      | Per-zone `avg_dwell_sec`                           |
| "Which zone is busiest?"                                       | Per-zone `enter_count_total` / `unique_objects`    |
| "How long do people spend looking for a spot?"                 | `Area avg_dwell_sec`                               |
| "What's the longest a car browsed without parking?"            | `Area max_dwell_sec`                               |
| "How many cars came through total, no double-counting flicker?" | `Area unique_objects` / `Area enter_count_clean`   |
