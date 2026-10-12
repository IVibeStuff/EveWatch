# EveWatch — your personal EVEMon

A lightweight EVEMon-style tool that runs on your own computer. It monitors
skill queues for all your characters, plans skills (with training times and
optimal remap suggestions), and shows wallet, assets, and Jita market prices —
all through the official EVE Online API (ESI).

Everything stays on your PC. No accounts, no third-party servers, no installs
beyond Python itself.

## Installing

**Windows / Linux installers** are on the project's *Releases* page: run
`EveWatch-Setup-x.y.z.exe` (Windows, no admin needed) or extract the `.tar.gz`
and run `./install.sh` (Linux). EveWatch then runs from the notification area
(tray) and opens in your browser; right-click the tray icon for *Open / Check
for updates / Quit*. **Help & settings → About & updates** inside the page tells you
when a new version is out. Your data is kept in `%LOCALAPPDATA%\EveWatch`
(Windows) or `~/.local/share/EveWatch` (Linux) and survives updates.

**From source:** install Python and double-click `Start EveWatch.bat`
(or `python3 evewatch.py`); data stays in `evewatch_data/` next to it.

## Setup (once, ~5 minutes)

### 1. Install Python (only for running from source)
Download from https://www.python.org/downloads/ and run the installer.
**Important:** tick the checkbox **"Add python.exe to PATH"** on the first
screen. Everything else can stay default.

### 2. Start EveWatch
Double-click **Start EveWatch.bat**. A black window opens (that's the app —
leave it running) and your browser opens http://localhost:8635 automatically.

### 3. Register your EVE developer application
EveWatch's Characters tab walks you through this (Setup section), but in short:

1. Go to https://developers.eveonline.com/applications and log in with your
   EVE account. (First visit asks you to accept the developer license.)
2. Click **Create new application**.
3. Name / description: anything, e.g. `EveWatch personal`.
4. Connection type: **Authentication & API Access**.
5. Add these permissions (scopes):
   - `publicData`
   - `esi-skills.read_skills.v1`
   - `esi-skills.read_skillqueue.v1`
   - `esi-clones.read_implants.v1`
   - `esi-wallet.read_character_wallet.v1`
   - `esi-assets.read_assets.v1`
   - `esi-universe.read_structures.v1`
   - `esi-location.read_location.v1` (find ships you're flying)
   - `esi-location.read_ship_type.v1`
   - `esi-characters.read_blueprints.v1` (Industry tab)
   - `esi-industry.read_character_jobs.v1` (Industry tab)

   If you add scopes later, also re-add each character with
   **+ Add character** so their login includes the new permissions.
6. Callback URL — paste exactly: `http://localhost:8635/callback`
7. Save, copy the **Client ID**, paste it into the Setup section of EveWatch's Characters tab, click
   **Save**.

### 4. Add your characters
Click **+ Add character** (top right). You'll be sent to EVE's official login
page; log in, pick a character, approve. Repeat for every alt on every
account. Characters stay logged in between sessions.

## Daily use
Double-click **Start EveWatch.bat**, that's it. Close the black window (or
press Ctrl+C in it) to stop.

- **Characters** — every character's queue, wallet, SP, attributes, implants.
  Empty or nearly-finished queues get a warning.
- **Skill Planner** — browse all skills, add targets, and it computes total
  SP, training time, and the optimal attribute remap. Prerequisites are added
  automatically. Plans can be saved. The first time you open it, EveWatch
  downloads the skill catalog from ESI (a minute or two, then cached).
- **Assets** — everything you own, valued at ESI average prices. Sort by
  clicking column headers; use the min/max ISK filter to hide the junk.
- **Wallet** — balance and journal, sortable, with a min/max transaction
  size filter.
- **Market** — build your own list of trade stations (search by solar
  system name, e.g. "Rens"), then compare any item's buy/sell prices across
  all of them, with a price chart. Jita 4-4 is included by default.

- **Item Search / Contracts** — find any item, jump to its market prices or
  to public contracts containing it (the Contracts tab builds a local index
  per region; the first build takes a while and can be resumed).
- **Wallet** also has a date range, entry-type filter and an income summary.
  EveWatch archives your journal locally, so history beyond ESI's ~30 days
  accumulates over time. Optional loyalty-point tracking needs
  `esi-characters.read_loyalty.v1` (loyalty points) and
  `esi-characters.read_agents_research.v1` (research points)
  added in the developer portal, switched on under Help & settings → General → Setup &
  permissions, then characters re-added.
- **Appraisal** processes big lists in batches and shows results as it goes.
- **Accomplishments** — certificates (Basic → Elite) and ship masteries
  worked out from your skills, milestone medals, and the closest next
  unlocks with a one-click "+ Plan".
- **Notifications** (Help & settings → Notifications) — EveWatch tells you when
  a skill finishes, a skill queue runs low or empty, an item sells, a contract
  of yours is completed, or an industry job finishes. Pick which ones you want
  and how often EVE's API is checked (default once an hour). The contract
  notice needs the optional `esi-contracts.read_character_contracts.v1`
  permission (portal → Setup & permissions → re-add characters). Notices only
  arrive while EveWatch is running.

## Good to know
- Your Client ID, character tokens, plans, and caches live in the
  `evewatch_data` folder next to the app. **Don't share that folder** — the
  tokens grant read access to your characters. Delete it to log everything out.
- ESI caches data server-side (skill queue ~5 min, wallet ~2 min), so numbers
  update on their schedule, not instantly.
- If login fails with a redirect error, the callback URL in your developer
  application doesn't exactly match `http://localhost:8635/callback`.
- The tool only ever *reads* data — the scopes granted can't fly your ship,
  move ISK, or change anything in-game.

## Ideas for later
Industry job tracking, PI monitoring, contract appraisal, mail reading, and
corp tools are all the same pattern: one more ESI scope + one more endpoint.
Ask Claude to bolt them on.
