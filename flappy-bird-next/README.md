# Flappy Bird Next.js

A small Flappy Bird clone built with Next.js, React, and an HTML canvas.

## Run

```bash
npm install
npm run dev
```

Open <http://localhost:3000>.

## Controls

- Browser: `SPACE`, `ArrowUp`, click/tap the game, or the **Flap** button.
- Reset: `R` in the browser or the **Reset** button.
- Terminal running `npm run dev`: `SPACE`, `f`, or `ENTER` sends a flap to connected browsers; `r` resets; `a` toggles terminal autopilot; `q` quits.

## Terminal logging / verification

The custom `server.js` wraps Next.js and adds:

- `GET /events`: Server-Sent Events bridge for terminal flap/reset commands.
- `POST /api/log`: game telemetry logging endpoint.

When the page is open, it posts telemetry every ~1.8 seconds so the terminal shows score, bird position, velocity, game state, and pipe count.
