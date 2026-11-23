# Annotation tasks through the ordinary adapter

Image annotation uses the existing [ordinary-task contract](ORDINARY_TASKS.md).
The `annotate-images-directly` skill, maintained separately in `yuki-inaho/skills`,
owns image inspection, annotation, geometry checks, overlays and answer freezing.
Verify its available revision separately; this repository does not bundle it or
provide a new image-model API.

| Layer | Responsibility |
|---|---|
| Image-work skill | Apply the supplied target definitions, inspect permitted images, produce the declared annotation format, check geometry, inspect overlays and freeze every answer. |
| Task-specific executor and judge | Connect an already verified execution entry point and the task's authoritative measurement procedure to literal argv commands. |
| Harness² ordinary adapter | Record executor receipts, run native harness selection and reflection when available, stage verified answer bytes, and retain independent post-selection judgments. |

## Fix the work contract and permitted inputs

Keep the procedure independent of the subject matter. Supply target definitions,
inclusion and exclusion rules, occlusion policy and region identity separately
for each task. State whether disconnected visible regions form separate objects.
Define the output schema, image identity and dimensions, coordinate origin and
normalization, region and vertex limits, and any rounding, clipping, overlap and
polygon-fill rules before annotation starts. Changing a coordinate convention or
a rasterizer changes the task contract, even when the JSON field names stay the same.

Build the annotator's input directory from an explicit allowlist: only the images,
work instructions and necessary target/schema definitions. Do not pass an original
package manifest merely because it lists the images. It may also contain notes
from the reference author, expected object counts, reference paths or other answer
hints. Check sidecars, filenames, image metadata, precomputed contours, reference
overlays, synthetic scene descriptions and helper output for the same indirect leak.
Record which permitted files were supplied and their hashes.

Keep references, evaluator-only metadata and measurement output outside the
annotator's input, trajectories and reusable experience. `leak_globs` is a second
check on copied paths; it does not sanitize fields inside an allowed file and is
not filesystem isolation. Neither the ordinary executor nor its local judge is
an OS sandbox. Do not claim that a path-filtered workspace proves that reference
files could not be read.

The ordinary manifest rejects unknown fields. Put the work contract in `task.md`
and approved `source` files, rather than adding annotation-specific manifest keys.
Use only synthetic images and references for public examples and regressions.

## Annotate, verify and freeze before reference access

1. Inspect every permitted image and produce the required regions using the image
   skill. Geometry helpers may check finite coordinates, bounds, vertex counts,
   nonzero area, self-intersections and duplicate regions. Overlays may show the
   predictions on the permitted image. Neither operation establishes that a
   semantic target was found or that no target was missed.
2. Preserve attempts and reference-free observations. Complete all requested
   answers, verify the declared file set, then record the exact answer bytes and
   hashes. Do not fill a missing answer with an empty prediction to make a pack
   appear complete. An intentional empty prediction must be an explicit,
   schema-valid annotation.
3. Open references only after the entire comparison unit has been frozen. Run the
   independent judge on that frozen output, and retain its input hashes, tool
   revision, rasterization rules, output and failures. Later edits create a new
   attempt; they do not replace the evaluated answer.

For a pack that must be fully answered before any reference access, represent the
whole pack as **one ordinary task** and one declared answer JSON. The ordinary CLI
judges each task after its selection; using one task per image does not provide a
whole-pack reference barrier. The adapter hashes only `executor.answer`. Bundle
the canonical annotations there; external overlays or files named by the answer
need their own explicit integrity records if they are part of the deliverable.

`ManifestAdapter.execute()` records `exit.json.answer_sha256` after execution.
Before judging, `verified_answer_bytes()` checks the successful receipt and reads
one matching answer snapshot; only those bytes are staged for the judge. Missing,
empty or changed answers and invalid receipts raise integrity errors. Core reports
them as ungraded. Completed-execution reuse and the ordinary CLI's judgment cache
repeat the check and do not silently repair or rejudge a changed answer. These
local hashes detect changes relative to the receipt; they are not signed evidence
or protection against a process that can rewrite both files.

## Keep measurements faithful to the task

Region definitions and the independent judge are replaceable task-specific parts.
A mask-based task may rasterize polygons and aggregate each class across all
images: class IoU is the sum of intersections divided by the sum of unions.
Computing an IoU per image and then averaging is a different measurement. Fix the
reference label encoding, class mapping, empty-union treatment, rasterizer and
overlap rules before the run. Retain intersection and union counts so misses and
extra predicted pixels remain visible. Object-count or disconnected-region rules
can require separate conformance checks even when the pixel masks agree.

The native ordinary judge accepts boolean `criteria`, or integer `n_passed` and
`n_criteria`. Its reported fraction is a criterion pass fraction. It does not
preserve arbitrary continuous metrics in the normalized result. A judge may print
detailed class measurements in additional JSON fields, retained in its
`stdout.log`, and separately return predeclared boolean conformance or threshold
criteria. Preserve and report the task's original metrics separately; do not
rename the criterion fraction as mean IoU or encode a continuous result as invented
pass counts. A malformed judge or an answer-integrity failure is unknown. A valid,
unchanged submission that fails a declared requirement can legitimately receive
zero passed criteria.

## Reflect and retry without changing the evidence

Use `findings`, `tips` and `struggles` as a reference-free work record:

| Field | Required distinction |
|---|---|
| `findings` | State observed image or execution facts, with the image/attempt and supporting observation. Keep untested explanations explicitly marked as hypotheses. |
| `tips` | Name a concrete action and the evidence that supports it. A proposed action stays a hypothesis until a recorded attempt checks it. |
| `struggles` | Record ambiguity, tool limits, failed checks and unresolved questions without inventing causes or hiding missing work. |

Place this reference-free record in `trajectory.md` when native editors need to
read it. The ordinary adapter exposes that trajectory; it does not automatically
collect arbitrary sidecar files. Native adopted experience keeps its existing
`cue`, `lesson`, `evidence` and `confidence` format. Native reflection occurs
before independent judging, and adoption alone does not prove an improvement.

Preserve each attempt's input hashes, work contract, tool/model information actually
observed, harness revision, answer hashes and execution conditions. Keep reference
measurements out of model-visible trajectories and later native experience.
If reference feedback informs a later change, record that use as development on
those examples; they no longer provide an independent test of that change.

## Report ordinary delivery separately from native optimization

An ordinary annotation can be completed through the image skill without a native
optimization run. Do not manufacture selection records or model receipts for that
work. Only bind `executor.argv` to an execution command whose capabilities have
actually been checked; `executor.model` is an unverified label, not a model launch.

Native optimization additionally requires the installed OpenCode launcher and a
usable exact editor model. Another installed CLI is not an automatic substitute.
`--help` and the existing synthetic manifest's `--dry-run` check the entry point and
resolved configuration. Scripted-editor tests check integration without a model.
Report each of those checks as such, including unavailable prerequisites. A native
run's status, `editor_health`, actual receipts and post-selection measurements are
separate evidence; neither setup nor a selected candidate establishes better
annotation quality or faster work.
