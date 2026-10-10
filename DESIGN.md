# Panel design

Subject: one VPN node and a daily traffic budget. The primary job is to judge
whether the node can safely keep running and act on a clear state.

Palette: fog `#edf2f7`, paper `#ffffff`, navy `#18344f`, signal blue `#326ac3`,
fault red `#b24749`, caution amber `#a36b20`. Color encodes state or traffic direction.
No decorative gradients. Typeface: IBM Plex Sans with Trebuchet MS and Segoe UI fallbacks; tabular
numerals for measurements, one consistent family, sentence-case Russian labels.

The distinctive element is a calibrated daily quota ruler, with consumed and
remaining portions and the next 03:00 reset beside it. Supporting controls stay
quiet. The chart and the node controls have different widths and hierarchy.

```
 Navigation | Node name                          State / last update
            | Waiting or fault message, if needed
            | Daily quota ruler                           Reset at 03:00
            | Traffic chart, 24 hours          | Node control
            | RX / TX and day selector         | Start / stop / recovery
            | History table or chronological event list
```

Review against the brief: a terminal-style dark dashboard would be a generic
default; replaced it with a light monitoring instrument. Identical metric cards
would hide the decision; replaced them with one quota ruler and an asymmetric
chart/control layout. Alignment is left; the sidebar becomes a compact horizontal
navigation on mobile. Google Fonts is optional; Referrer-Policy: no-referrer prevents forwarding the secret URL. Local font fallbacks keep the panel usable offline.

Keyboard focus is explicit; charts include textual values; state never relies on
color alone. Reduced motion disables transitions. Login is a focused form, with a
plain warning about HTTP. Empty history explains when real data will appear.

The live settings form uses the existing navy/blue palette and typography. A large
GB field makes the main value easy to scan. Two native radio cards explain the
stop and monitor policies without an ambiguous toggle. A consequence message
explains immediate stops and quota-related restarts before saving. Unsaved edits
survive background polling; saving shows progress and waits for controller
confirmation rather than optimistic success. Validation is linked to the field,
focus remains visible, and the primary action has a 44px minimum touch target.

Applied [UI UX Pro Max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)
alongside [frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design).
The explicit UX search `inline validation saving feedback` returned form inline
validation, accessible error summaries, and error placement. The existing page
structure was retained; recommendations were scoped to the settings form and
its desktop/mobile interaction states.
