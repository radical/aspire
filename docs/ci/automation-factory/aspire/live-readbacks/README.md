# Live automation prompt readbacks

These files preserve the exact prompt bodies returned by the owner-scoped
automation readbacks recorded in `deployment.json`. Their SHA-256 values match
the server's `promptSha256`; they are snapshots, not the editable prompt
sources or a deployment mechanism.

The Scout, Worker, and Supervisor prompts currently exceed the stated
12,000-character authoring budget. Keep these exact snapshots for audit and
comparison; edit the modular sources and validate a candidate against the
budget before asking the owner to apply it. The Publisher prompt is unchanged.

The Scout startup instructions fetch `addendums/scout.md` from the fork at
runtime. That file is updated separately; these snapshots preserve the prompt
text that was saved at readback time, not the contents later fetched from that
URL.
