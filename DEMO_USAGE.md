# Demo Usage Notice — Internal Use Only

This branch (`legacy-demo`) preserves the Ultralytics-based version of this
tool for **internal demos only**. The `main` branch is being rewritten to
replace `ultralytics` (AGPL-3.0) with permissively-licensed alternatives
(onnxruntime, roboflow/trackers); this branch is the working reference
until that rewrite reaches parity.

## ✅ Allowed
- Running on your own machine for in-person or screen-shared customer demos
- Recording the app in action for pitch decks, marketing videos, training
- Validating output head-to-head against Plan B during the rewrite

## ❌ Prohibited
- Building installers from this branch and shipping them to customers
- Hosting any service or dashboard from this branch where external users can
  interact (AGPL Section 13 triggers on remote network interaction)
- Letting any customer download, install, or run this version themselves
- Distributing the bundled trained `.pt` weights to customers

## Why
This branch imports `ultralytics` (AGPL-3.0). Distributing or remotely
exposing software that links AGPL code requires either (a) releasing the
full source under AGPL or (b) an Ultralytics Enterprise license. Plan B on
`main` removes that requirement.

## Lifecycle
This branch will be deleted once Plan B reaches mAP parity with the
Ultralytics version on real customer data. The full code remains accessible
forever via the `v0.9-demo-ultralytics` tag, so deleting the branch is
non-destructive — you can always check out the tag for archival reference.
