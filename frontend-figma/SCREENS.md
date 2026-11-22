# Screens (from Figma "Ratatouille", file ejdNnvtG1sBfD9MiRx6eI1)

Review before building. Nothing below is implemented yet except tokens and assets.

## Screens

| Figma frame | Node | Size | Becomes |
|---|---|---|---|
| dashboard | 20:2 | 1280x832 | `src/screens/Dashboard.tsx` |
| menu | 22:50 | 350x239 | `src/screens/MenuPanel.tsx` (opened by the menu icon) |

## Shared components (Figma component sets)

| Figma | Node | Becomes | Variants |
|---|---|---|---|
| text-bubble | 22:66 | `src/components/TextBubble.tsx` | border color: beige (default), green (ok), red (error) |
| toggle | 22:92 | `src/components/Toggle.tsx` | on / off |
| bar | 22:121 | `src/components/Bar.tsx` | none (flexible width, 22px high) |

## Dashboard layout (top to bottom)

1. **Header**: title image (352x103, left) and menu icon (30x26, right).
2. **Two cards side by side** (equal width, 40px gap), 2px beige border, 20px radius:
   - Left, "recipe step": step heading (Heading 2, numbered), instruction line, Remy
     image beside a three-paragraph message (last paragraph bold 18), then QR code
     and recipe image placeholders (beige boxes).
   - Right, "conversation": row of four status bubbles, user image beside the user's
     transcript text, then a camera placeholder (beige box, 320:210 aspect).
3. **Bottom card, two columns**: "Latency of last turn" (4 labelled bars with a value
   column showing "-") and "Event log" (text).

## Assets (downloaded to `src/assets/`)

title.png, remy-talking.png, user-talking.png, menu.svg. The title and character
PNGs are 2048px wide; they should be resized before shipping.

## Tokens

See `src/styles/tokens.css`. Fonts: Protest Riot (headings), Nunito (body).

## Placeholders in the design that need live data (wired through `src/api.ts`)

- Step number, step title, step instruction, Remy's message
- Status bubbles: wake word, camera, S3, connection (green = ok, red = error)
- User transcript
- Camera view, QR code, recipe image
- Four latency values and bar fills (first word, first sound, laser moved, on target)
- Event log

## Gaps and questions

1. **Dark mode**: the menu has a light/dark toggle but the file has no dark palette.
   I'll build the toggle and wire it to a `data-theme` attribute, but need dark values
   from you, or I leave the dark theme empty.
2. **Dyslexia font**: no font is specified. Suggest OpenDyslexic (free). OK?
3. **Show developer info**: I'll assume it shows/hides the bottom card (latency + event
   log). Correct?
4. **Menu panel**: assumed to be a popover from the menu icon. Only one frame exists,
   so its position and open/close behavior are my call.
5. **Latency bars**: only the empty track is drawn. I'll add a fill (green) scaled to the
   value. No max is specified; say so if you want a fixed scale (e.g. 2 s).
6. **Layout**: the dashboard is designed at 1280x832. I'll make it fill the window
   (flex layout as in Figma), with the 1280x832 look at that size.
7. **Bubble variants**: the design only shows bubbles in beige, green and red; I'll
   map them to waiting / ok / error.
8. The "Waiting for “Hey Remy!”" bubble and the user line say "Remy" vs "Remi" in the
   design; I'll keep the text as drawn.
