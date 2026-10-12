# Changelog

## 2.0.0
**New**
- **Accomplishments tab** for the active character: skill-point and skill tiles, milestone medals (SP, skills at V, Elite certificates, Mastery V ships, battleship / Tech II / Tech III / capital pilot), "Next up" (the closest next certificate levels and ship masteries, with training time and + Plan), every certificate (Basic → Elite) grouped by area with what the next level needs, and ship masteries by ship class. Worked out from your skills and CCP's data — no new permissions.
- **My contracts** (Contracts tab; the public contract search moves to "Public search"): your own contracts for one or all characters, grouped as Outstanding, Assigned to me, In progress (couriers) and Finished / expired (last 30 days), with price, location, the other party and time left. Click a contract for its items, an estimated value (EVE's average market price, with how your asking price compares) and auction bids.
- **Notifications**: EveWatch watches your characters in the background and tells you when a skill finishes, the skill queue runs low or empty (threshold in hours), an item sells on the market, one of your contracts is completed, or an industry job finishes — each can be switched on or off. EVE's API is checked once an hour by default (15 min to 4 hours, or "Check now"); skill and industry timers are watched in between without extra API calls. Notices appear as desktop pop-ups (tray notification on Windows, notify-send on Linux) and in an open EveWatch tab; a gold dot on Help & settings marks unread ones.
- **Help & settings** window replaces the Help menu: About & updates, General (Start with Windows / at login, Close on Exit, Setup & permissions, ship mastery data, data folder) and Notifications.
- **Optional contracts permission** (`esi-contracts.read_character_contracts.v1`) for My contracts and the contract notice, alongside the loyalty- and research-points permissions in Setup & permissions.
- **Safer logins**: when optional permissions are switched on, adding a character first shows an EveWatch "Log in with EVE" step. If EVE answers "invalid_scope" (the developer application doesn't list a permission yet — portal changes can take a few minutes), press Back and add the character without that permission, or with the standard permissions only.
- Characters show which optional permissions are switched on but not part of their login; **⚙ → Update permissions…** logs a character in again to add them.
- Skill plans are named after what you add ("Ishtar · Mastery IV", "Armor Tanking · Elite", "Fly Drake", "Drones V"); the name shows above the plan and is suggested when saving.

**Fixed**
- Skills that finished while you were offline now show as trained (and leave the queue) right away; EVE's API only records them after the character next logs in.
- Header tabs stay on one line with the new Accomplishments tab.

## 1.7.0
- Help → "Start with Windows" ("Start at login" on Linux): toggle auto-start any time, not only during installation.
- Items without a picture (ship skins etc.) no longer show an empty frame.

## 1.1.0
- Skills tab: Queue (timeline), My skills (by group, level pips) and Planner as sub-tabs; Overview shows the training skill with a link.
- Market: Hub scanner finds items cheap in one hub and dear in another (whole market or chosen item groups), with activity, tax and cargo filters.
- Overview tab redesigned as a single-character sheet: portrait, attribute tiles with icons (training attributes outlined), implants in their 10 slots, skill queue.
- Ship bonuses (role + per-skill-level) shown at the active character's current skills; missing skills for mastery shown as an EVE-style prerequisite tree.
- Ship mastery (I-V) per character in the Item Search ship panel, with missing skills and one-click plan; data from CCP's static data export (bundled; Help → Refresh ship mastery data updates it).
- LP Store tab: research-agent points → datacores (100 RP + 10,000 ISK each) priced at all market hubs. Optional research-points permission.
- Setup and loyalty-points settings move to Help → "Setup & loyalty points…" once characters exist (first-launch flow unchanged).
- Help → "Close on Exit" (default OFF): when ON, EveWatch quits after its last browser tab closes.
- Start-up order: Characters tab first (Setup lives there), other tabs unlock once a character is selected; header shows the active character only; Add/Remove/theme are on the Characters screen. Old Characters dashboard is now "Overview".
- Fixed: Help menu hidden behind the page; "Check for updates" result vanished.
- Character select screen on start-up (portrait, training, wallet, ship, location);
  per-character theme and Remove live in each card's ⚙ menu; "Switch character" button in the header.
- Installable Windows and Linux builds with a tray icon.
- Help menu: check for updates, about, open data folder, quit.
- Per-character themes, LP Store (ISK per LP) tab, Contracts tab, batched
  appraisal, wallet date range + journal archive, redesigned item/ship panel,
  built-in trade hubs.

## 1.0
- First version: skill queues, planner, assets, wallet, market tools.
