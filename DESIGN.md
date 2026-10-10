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
