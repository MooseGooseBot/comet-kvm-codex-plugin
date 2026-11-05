# Sessions and Targets

Read this file when the task needs a non-default connection, multi-target
selection, or initial machine-state discovery.

## The server owns the default connection

The server manages its own connection to the default Comet appliance. The
first step for any task is the operation itself — a screenshot, OCR read, HID
input, power check, media action — not `kvm_status`, not `kvm_connect`. Device
tools establish or reuse a healthy session automatically.

## When to use kvm_connect

`kvm_connect` is an override, not a routine step. Call it only for:

- a non-default host (a Comet other than the managed default);
- explicit credentials instead of the resolved default/Doppler password;
- a named multi-target session (`target=...` other than `default`);
- `force_reconnect=True` to force a fresh session over a live one.

It takes no required arguments. Calling it against a host that already has a
matching live session returns `reused: true` without touching Doppler. Never
call it as a routine first step before an operation on the default target.

## Multi-target discipline

When multiple sessions exist:

- call `kvm_select_target` before using KVM input tools;
- pass an explicit `target` to Comet-specific tools that accept it;
- never assume the selected target matches an ambiguous request — confirm
  which machine the user means before acting.

Selected-target tools such as screenshots, OCR, HID input, and `bios_*` do not
take a target in the current schema. Select once before using tools that do
not expose `target`; call target-aware tools with `target=...` when you need
to override the current selection.

Named targets are not auto-managed the way the default is: a cold named
target fails closed. Connect it explicitly with
`kvm_connect(host=..., target=...)` before using it.

Use the connection capability profile to decide whether ATX, virtual media,
OCR, recording, and other subsystems are available. Call `comet_capabilities`
with refresh only after a firmware, hardware, or configuration change, or when
the cached profile is missing.

## Disconnect semantics

`kvm_disconnect` closes the session now and releases the device streamer. It
is non-sticky for the default target: the next device operation reconnects
that target automatically. A named target must be connected explicitly again
with `kvm_connect(host=..., target=...)`. Use it when you are done observing a
target or need to free the appliance for another session — not as end-of-task
ceremony. Preserve other sessions unless the user asked to close them.

## Recovery

Never manually reconnect to work around an error. If a tool call fails in a
way that suggests the transport is genuinely gone, use
`kvm_connect(force_reconnect=True)` as the escape hatch, not a fresh
`kvm_connect` call. After any reconnect, recapture the screen instead of
replaying the last input — a replayed keystroke or click can land on a
different screen than the one it was meant for.

## Establish the machine phase

Capture the console and classify the visible state as powered off or no
signal, POST, BIOS or UEFI, bootloader, one-time boot menu, installer,
operating system, recovery shell, crash, boot loop, or unknown. Use OCR only
when text materially improves the classification. Do not send input merely to
discover state when a read-only observation is sufficient. When ATX reports
`enabled: false`, ignore the power field and classify state from the console
instead.

## kvm_status is inspection only

`kvm_status` observes without connecting — it never establishes a session.
Use it to inspect the configured defaults (`default_host`, `default_username`,
`default_target`, `auto_connect`), the live connection, and capture
diagnostics, not as a precondition for calling a device tool.

## Comet X channels are not targets

A Comet X (GL-RM4PE) is one target with four channels. `target=` still names
the Comet; the channel is chosen with `comet_switch_set_active(port)` (0-based)
and read with `comet_switch_state`. Before console input on a Comet X, confirm
`summary.active_port` is the machine you mean, then capture the screen. After
a switch, remount virtual media if the new machine needs it; it detaches on
every switch. On a single-port Comet the switch tools fail with
`SwitchUnavailable`, which is the normal answer, not a fault.
