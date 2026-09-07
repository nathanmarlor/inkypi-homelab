# InkyPi homelab screens

A set of screens for a 7.3" Pimoroni Inky Impression (2025 Spectra 6 edition) on a Raspberry Pi,
built on top of [InkyPi](https://github.com/fatihak/InkyPi) by [@fatihak](https://github.com/fatihak).

**This repository is a fork of InkyPi.** Everything under `src/` that is not listed below is InkyPi's
own code, unchanged, and the installer, web UI, playlist scheduler and display drivers are all InkyPi's
work. It is published under the same GPL-3.0 licence (see `LICENSE`); the original README is kept at
[`docs/UPSTREAM_README.md`](docs/UPSTREAM_README.md). Bug reports about the framework belong upstream;
anything about the screens below belongs here.

It is a GitHub fork, so InkyPi's full history is here and the additions are a single commit on top. To pull
upstream changes: `git remote add upstream https://github.com/fatihak/InkyPi.git && git fetch upstream && git merge upstream/main`.

## What's added

| Plugin | Screen | Data |
|---|---|---|
| `big_clock` | Large clock with date, week and day-of-year band | local time |
| `local_weather` | Sky-coloured band with current conditions, 12-hour strip with rain chance, 5-day row | [Open-Meteo](https://open-meteo.com/) (no key) |
| `bbc_news` | Lead story plus three cards, with the opening paragraphs of each article | BBC RSS + article pages |
| `ai_usage` | Cloud spend avoided by a local LLM box, GPU busy/now/memory, lifetime gateway totals; Claude Code plan limits and API-equivalent cost | Prometheus, [LiteLLM](https://github.com/BerriAI/litellm) proxy API, `scripts/claude_usage_exporter.py` |
| `home_energy` | Solar, battery, grid and house power with a day chart and today's totals | Home Assistant REST API (FoxESS, Solcast, myenergi, Octopus entities) |
| `bitcoin_miners` | Bitaxe hashrate and health, odds of finding a block, best share, node stats | Bitaxe/ForgeOS JSON API, `bitcoind` exporter in Prometheus |
| `homelab_status` | Host cards with CPU/load/memory/disk gauges, k3s summary, platform tiles, alerts line | Prometheus (node_exporter, kubelet, Flux, Traefik) |
| `night_screen` | A static dark frame for an overnight playlist window | none |

Also included:

- `src/plugins/_theme/` – a shared stylesheet and a `ThemedPlugin` mixin the screens use for a consistent look.
- `src/utils/homelab.py` – small Prometheus and Home Assistant clients, plus the local settings loader.
- `scripts/spectra6_preview.py` – previews a rendered frame through the same 6-colour quantiser the Inky driver uses, so you can see what the panel will actually show before deploying.
- `scripts/claude_usage_exporter.py` – aggregates Claude Code transcripts and plan limits on the machine you use Claude Code on, and pushes a small JSON snapshot to the Pi (see `install/examples/`).
- `scripts/deploy_to_pi.sh` – rsyncs this tree to the Pi, runs the InkyPi installer or updater, copies secrets and builds the device config from `src/config/device_dev.json`.

Two small changes to InkyPi itself: `src/inkypi.py` runs the web server with four threads instead of one
(with one, the UI is unreachable for the ~30 s an e-ink refresh takes), and the deploy script turns off the
start-up splash for the same reason.

## Design notes for the Spectra 6 panel

The panel has six inks: black, white, red, yellow, green and blue. Anything else is dithered, so the screens
use flat fills in the exact colours the driver quantises to at its default saturation (`#cd2425`, `#1e1dae`,
`#e7de23`) and avoid greys, gradients and thin glyphs. Small middle-dot separators and anti-aliased text under
about 12 px vanish after quantisation, which is why the theme uses a solid square for separators and keeps
body text at 12 px or larger. `scripts/spectra6_preview.py` will show you the problem before the panel does.

## Setup

1. Follow the InkyPi installation in `docs/UPSTREAM_README.md`, or use `scripts/deploy_to_pi.sh user@host`
   from a checkout on your workstation.
2. Copy `src/config/local_settings.example.json` to `src/config/local_settings.json` and fill in your
   Prometheus, Home Assistant, LiteLLM and miner addresses, host names and any entity id overrides. The file is
   gitignored. Every value can also be set per screen in the web UI.
3. Add tokens under **API Keys** in the web UI (or to `.env`): `HOME_ASSISTANT_TOKEN` for the energy screen,
   `LITELLM_MASTER_KEY` for the gateway totals.
4. For the Claude Code figures, run the exporter on the machine where you use Claude Code; see
   `install/examples/claude-usage-exporter.service`.
5. Add the screens to a playlist. For a dark panel overnight, create a second playlist with a window such as
   22:00 to 07:00 containing only the Night Screen; InkyPi picks the playlist with the shortest window.

## Licence

GPL-3.0, as InkyPi. See `LICENSE` and `docs/attribution.md` for the fonts and icons InkyPi bundles.
