# ff-manager

A fantasy football manager that runs the whole season. It drafts your team,
then watches it: who to start each week, who to pick up off waivers, and what
is about to go wrong. Every call comes with the reason it was made.

```
ffm run   --sleeper 1234567890 --user yourname   # draft day
ffm week  --league  9876543210 --user yourname   # every Sunday after that
ffm watch --league  9876543210 --user yourname   # or let it tell you
```

It starts from the projection and moves it only for what the projection
cannot know: an injury tag, and anything you know that the data does not,
which goes in `opinions.toml`. Team form, matchup and role are measured and
shown beside every call, but not priced in — backtested, they made the
projection worse. See [Beyond the projections](#beyond-the-projections).

## Quick start (Sleeper — no auth needed)

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync          # fetches Python 3.14 if needed, then .venv from uv.lock
uv run ffm --help
```

You do not need a Python installed first — `.python-version` pins 3.14 and uv
provisions it. (`requires-python = ">=3.11"` in `pyproject.toml` is the floor
`tomllib` imposes, not the target; development and the container both run
3.14.)

`uv run` needs no activated environment. If you prefer one:
`source .venv/bin/activate`, then just `ffm`. Every example below uses the
bare `ffm`; prefix with `uv run` if you have not activated.

During the season:

```bash
ffm leagues --user <your_sleeper_username>     # find your league_id
ffm week    --league <league_id> --user <your_sleeper_username>
ffm waivers --league <league_id> --user <your_sleeper_username>
```

On draft day:

```bash
ffm drafts --user <your_sleeper_username>     # find your draft_id
ffm run --sleeper <draft_id> --user <your_sleeper_username>
```

The in-season commands are Sleeper-only. Yahoo works for the draft; its
in-season endpoints are not wired up yet.

## Quick start (Yahoo — needs a free API key)

Yahoo's API requires OAuth, which needs an app registered under your own Yahoo
account. It takes about three minutes:

1. Go to <https://developer.yahoo.com/apps/create/> and sign in.
2. Fill in:
   - **Application Name**: `ff-manager` (anything works)
   - **Application Type**: `Installed Application`
   - **Redirect URI**: leave blank, or `oob`
   - **API Permissions**: tick **Fantasy Sports**, and select **Read**
3. Create the app. You'll land on a page with a **Client ID (Consumer Key)** and
   **Client Secret (Consumer Secret)**.
4. Put them in a `.env` file next to this README:

   ```
   YAHOO_CLIENT_ID=<your client id>
   YAHOO_CLIENT_SECRET=<your client secret>
   ```

5. Authorize once — this opens a browser, you approve, and paste the code back:

   ```bash
   ffm yahoo-auth
   ffm yahoo leagues          # find your league_key, e.g. 461.l.123456
   ffm run --yahoo <league_key>
   ```

The token is cached in `~/.config/ff-manager/yahoo_token.json` and refreshes
itself, so you only do this once.

## Rehearse before the real thing

```bash
ffm sim --teams 12 --slot 5 --scoring half_ppr        # watch the live board
ffm sim --auto                                        # draft a full team instantly
```

## Commands

| Command | What it does |
| --- | --- |
| `ffm leagues --user NAME` | List your Sleeper leagues and their ids |
| `ffm watch --league ID` | Run continuously and push alerts to your phone |
| `ffm telegram-setup` | One-time: find your Telegram chat id |
| `ffm notify-test` | Send yourself a test notification |
| `ffm week --league ID` | Weekly report: alerts, start/sit, and top waiver adds |
| `ffm waivers --league ID` | The full waiver board, and who to drop for each add |
| `ffm news --league ID` | Injuries, depth-chart promotions and market moves right now |
| `ffm models` | List OpenRouter model ids for `--brief` |
| `ffm health` | Exit non-zero if the watcher has stalled (used by the healthcheck) |
| `ffm drafts --user NAME` | List your Sleeper drafts and their ids |
| `ffm run --sleeper ID` | Live dashboard for a Sleeper draft |
| `ffm run --yahoo KEY` | Live dashboard for a Yahoo draft |
| `ffm pick --sleeper ID` | Print the current recommendation once and exit |
| `ffm sim` | Offline mock draft against ADP-driven bots |
| `ffm yahoo-auth` | One-time Yahoo OAuth |
| `ffm yahoo leagues` | List your Yahoo leagues |
| `ffm check` | Audit the engine for internal inconsistencies |
| `ffm cache-clear` | Drop cached player/projection data |

Useful draft flags: `--slot N` if it can't work out your draft position,
`--interval N` to change the poll rate, `--bpa 0.0…1.0` to shift the engine
between pure positional scarcity and best-player-available.

Useful in-season flags: `--week N` to look at another week, `--horizon N` to
change how many weeks ahead waiver adds are valued over (default 4),
`--lookback N` for how many games of history feed the context signals
(default 5), `--opinions PATH` to point at a different opinions file.

## Keeping it running

A report you have to remember to run is a report you will not run. `ffm watch`
polls the league and pushes what changed to your phone.

### Telegram, once

```bash
# 1. In Telegram, message @BotFather, send /newbot, follow two prompts.
#    It gives you a token. Put it in .env:
#       TELEGRAM_BOT_TOKEN=8123456:AAH...
# 2. Send your new bot any message ("hi") — Telegram will not reveal a chat
#    id until the chat has spoken to the bot first.
ffm telegram-setup        # prints your chat id
#       TELEGRAM_CHAT_ID=123456789
ffm notify-test           # check it arrives
```

Prefer not to make a bot? Set `NTFY_TOPIC` to an unguessable string and install
the [ntfy](https://ntfy.sh) app instead, or `DISCORD_WEBHOOK_URL` for Discord.
The first channel configured wins; `FFM_NOTIFY` picks one explicitly.

### Then

```bash
ffm watch --league <league_id> --user <name>
```

It checks every 30 minutes and sends you:

| When | What |
| --- | --- |
| Immediately | One of your players downgraded — with the replacement to start and which slot to put him in |
| Immediately | **Anyone's** starter ruled out, and the free agent standing behind him on that team's depth chart |
| When it moves | A sudden spike in how many leagues are adding a free agent |
| When it changes | A new start/sit call, with the reason |
| 09:00 daily | The full digest — matchup, projected score, every swap, top adds |

### Watching for the backup nobody has picked up yet

This is the part worth understanding, because it is where every statistical
signal in the app is blind. When a starting back goes down on Friday, his
backup's snap share is still last week's, his projection has not been redrawn,
and his box score is unremarkable. Nothing in the numbers knows yet.

What *has* changed is the depth chart, and that is knowable immediately. So
when any player is ruled out, `ffm news` walks his team's depth chart, finds
the man directly behind him, checks he is unowned in your league, and tells you
to go and get him:

```
PICKUP     Pick up Braelon Allen (RB-NYJ) — Breece Hall is Out
           Breece Hall is Out; Braelon Allen is next on the NYJ depth chart
           and is a free agent in your league.
```

That fires for every team, not just yours — someone else's injury is your
pickup. When it is *your* player who went down it is flagged urgent and marked
as his handcuff.

Alongside it, a sharp jump in league-wide adds is treated as news: it is the
crowd reacting to a beat report the projections have not absorbed, which makes
it a usable proxy for reading the news without parsing any.

Run it on demand with `ffm news --league <id> --user <name>`.

**It stays quiet by design.** A first run reports standing opportunities but no
injuries, because with no previous poll there is no such thing as a change — an
early version announced 34 players as "now Questionable" on startup. A
Questionable tag on a player you do not own is never reported at all; half the
league carries one by Friday. Someone else's player only surfaces once he is
actually sidelined, because that is when a job opens up.

Poll freshness is handled for you: cached data older than the poll interval
would make every diff empty, so `watch` shortens the live TTLs to half the
interval automatically (`--freshness` to override). The current week's
projections — which carry the freshest injury tag Sleeper exposes — are about
0.2 MB, so this is close to free.

Change the digest with `--digest-at 09:00,17:30`, or `--digest-at ""` for
alerts only. `--interval` sets the poll rate.

**It will not spam you.** Every message carries a dedupe key and nothing is
ever sent twice. The first check of a week sends one digest and records
everything it saw as already delivered, so you hear about changes rather than
history; if a lot moves at once (a bye week landing) it collapses into a single
digest instead of a burst.

### On a VM with Docker

This is what I would use on a VM: it survives reboots, pins the whole
dependency tree via `uv.lock`, and moves between machines unchanged.

```bash
cp .env.example .env       # fill in the tokens, FFM_LEAGUE_ID, FFM_SLEEPER_USER, TZ
docker compose up -d --build
docker compose logs -f
```

Three things in that compose file are load-bearing, and two of them are
mistakes that are easy to make and annoying to discover:

- **`TZ`.** `--digest-at 09:00` is local time. A container is UTC unless told
  otherwise, so without this your morning digest arrives in the middle of the
  night. `tzdata` is installed in the image specifically so setting it works.
- **The `ffm-data` volume.** `/data` holds the dedupe keys and the injury
  snapshot, not just cached NFL data. Lose it and the watcher forgets what it
  has already told you, so the next poll re-sends every alert from scratch.
  This is the one that will actually make you mute the bot.
- **The healthcheck.** A wedged poller is still a running process, so
  `restart: unless-stopped` alone proves nothing. `ffm health` asks whether a
  pass has actually *completed* recently — the watcher writes a heartbeat after
  each successful one.

The image is a two-stage build: `uv sync --frozen` in the builder, and only
the resulting virtualenv copied into a clean `python:3.14-slim` runtime, which
runs as a non-root user and carries no build tooling. `--frozen` means the
lockfile is used exactly and never silently re-resolved.

Both versions are pinned, but differently on purpose. The base tracks the
`3.14` series rather than an exact patch, so rebuilding picks up CPython
security fixes on its own. uv is pinned exactly (`0.12.9`), because it is the
thing that resolves the dependency tree and that should never move without you
asking.

Prefer a scheduler to a long-running process? The same image does that too —
override the command with `watch --once` and drive it from host cron:

```bash
*/30 * * * * cd /srv/ff-manager && docker compose run --rm watcher \
  watch --once --league ID --user NAME
```

I default to the long-running form because `ffm watch` already has its own
error backoff, keeps no state in memory, and restarts cheaply — so the extra
scheduler is machinery you do not need.

#### Building it on a Mac first

Docker Desktop is not required and needs GUI setup. Colima gives you a real
Docker daemon from the CLI:

```bash
brew install colima docker docker-compose docker-buildx
# Register the Homebrew CLI plugins so `docker compose` / `docker buildx` resolve:
mkdir -p ~/.docker && python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".docker" / "config.json"
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg["cliPluginsExtraDirs"] = sorted(
    set(cfg.get("cliPluginsExtraDirs") or []) | {"/opt/homebrew/lib/docker/cli-plugins"}
)
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY

colima start --cpu 2 --memory 4 --disk 20
docker build -t ff-manager:latest .
```

Colima does not start at login by default; `brew services start colima` if you
want it to. If Docker Desktop was ever installed on this machine, check
`~/.docker/config.json` for a leftover `"credsStore": "desktop"` or
`"currentContext": "desktop-linux"` — both point at things that no longer
exist and will break pulls with a confusing error.

**`docker compose config` prints your secrets.** It interpolates `.env` and
renders the result in full, tokens included. Useful for debugging, but do not
paste the output anywhere.

### On a VM without Docker

Honestly simpler if the VM does nothing else — no image builds, no daemon, and
journald handles the logs. uv still pins the dependencies:

```bash
git clone <your fork> /srv/ff-manager && cd /srv/ff-manager
uv sync --frozen --no-dev

sudo tee /etc/systemd/system/ffm-watch.service >/dev/null <<'UNIT'
[Unit]
Description=ff-manager watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ffm
WorkingDirectory=/srv/ff-manager
Environment=TZ=America/New_York
EnvironmentFile=/srv/ff-manager/.env
ExecStart=/srv/ff-manager/.venv/bin/ffm watch --league YOUR_ID --user YOUR_NAME
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl enable --now ffm-watch
journalctl -u ffm-watch -f
```

`WorkingDirectory` matters — that is where `opinions.toml` is read from.

### On your Mac (launchd)

If the VM is not up yet, launchd can run `--once` on a schedule locally:

```bash
cat > ~/Library/LaunchAgents/com.ffm.watch.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ffm.watch</string>
  <key>ProgramArguments</key><array>
    <string>/ABSOLUTE/PATH/TO/.venv/bin/ffm</string>
    <string>watch</string>
    <string>--once</string>
    <string>--league</string><string>YOUR_LEAGUE_ID</string>
    <string>--user</string><string>YOUR_SLEEPER_NAME</string>
  </array>
  <key>WorkingDirectory</key><string>/ABSOLUTE/PATH/TO/ff-manager</string>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/ffm-watch.log</string>
  <key>StandardErrorPath</key><string>/tmp/ffm-watch.err</string>
</dict></plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.ffm.watch.plist
tail -f /tmp/ffm-watch.log
```

`WorkingDirectory` matters — that is where `.env` and `opinions.toml` are read
from. To stop it: `launchctl unload ~/Library/LaunchAgents/com.ffm.watch.plist`.

On Linux, the same thing with cron: `*/30 * * * * cd /path/to/ff-manager &&
.venv/bin/ffm watch --once --league ID --user NAME`.

Sleeper's API is public and unauthenticated, and completed weeks are cached
permanently, so a check every 30 minutes is a handful of small requests.

## Optional: a written summary

`ffm week --brief` adds a short paragraph over the top of the report, via
[OpenRouter](https://openrouter.ai). Set `OPENROUTER_API_KEY` in `.env`;
`ffm models` lists model ids and prices, and `OPENROUTER_MODEL` overrides the
default.

The model is a **writer, not an analyst**. It is handed facts that have already
been computed, checked and bounded — it never sees the raw player pool, and it
is never in the loop that decides anything. Pull the key and the report is
identical except for one paragraph.

Three things keep it honest:

- **No invented numbers.** Every figure in the output must appear in the input,
  or the paragraph is discarded and the computed reasons print instead.
- **No invented vocabulary.** Numbers alone are not enough — the payload is
  dense with figures, so a fabricated claim can borrow one by coincidence.
  ("11.4 targets a game" once passed the numeric check because 11.4 happened to
  be another player's projected points.) A stat word the input never uses is
  also grounds for rejection.
- **No predictions.** The app never claims to know who wins; a summary that did
  would undo that in one sentence.

What that does *not* catch is a true number drawn to a wrong conclusion — the
guards are structural, not semantic. That is precisely why the summary is
decoration: nothing downstream reads it, and every number in it also appears in
the tables underneath, where you can check it.

Responses are cached against a hash of the exact payload, so a watcher polling
every thirty minutes pays for one call per genuine change, not one per poll.

## How the draft recommendation works

The headline number is **VONA** — value over next available. A pick is worth
what it adds to your starting lineup *now*, minus what you could expect from
that same position at your next pick.

1. **Projections.** Sleeper publishes season projections with raw stat lines
   (`rec`, `rush_yd`, `pass_td`, …) and its scoring keys match those stats, so
   your league's scoring is applied as an exact dot product rather than being
   bucketed into ppr/half/std. TE premium and first-down scoring come along for
   free. Verified exact against Sleeper's own computed points for every
   offensive player; team defenses fall back to the precomputed column.
2. **Replacement level.** Set by how many of each position the league actually
   starts, with flex spots allocated to whichever positions win them rather
   than assumed.
3. **Marginal lineup value.** Your roster is backfilled with replacement-level
   players and the optimal starting lineup is solved exactly (Hungarian
   algorithm — greedy flex-filling is wrong once SUPER_FLEX and REC_FLEX
   overlap). A pick is worth the improvement it makes to that lineup, so a
   third good RB is correctly valued as a flex upgrade, not as an RB1.
4. **Survival.** Each player's draft slot is modelled as `Normal(ADP, σ)`,
   *conditioned on him still being on the board* — so a faller isn't written
   off — and rescaled so the expected number of players taken between your
   picks matches the number of picks that actually happen.
5. **Adjustments.** Tier cliffs, injury status, bye-week stacking, positional
   roster caps, and forced K/DEF at the end.

## How the weekly recommendation works

Start/sit is the draft's lineup optimizer pointed at weekly projections — the
same exact Hungarian assignment, so overlapping flex slots stay correct. What
is new is that it optimizes on points adjusted for injuries and your
opinions, and that it reports a diff
rather than a lineup. You already have a lineup set; what you want on Sunday
morning is which two players to swap and why, not to re-read nine names you
were never going to change.

Waivers is the draft's marginal-value calculation with the waiver pool as the
candidate set and your roster as the baseline, with two corrections:

- **It is not about Sunday.** A pickup is a multi-week asset, so candidates are
  valued on the average of the next few weeks' projections rather than this
  one.
- **Projections lag role changes.** The waiver player worth having is usually
  one whose situation changed last week — the backup who just took 70% of the
  snaps. His projection has not caught up and his box score may look ordinary,
  so usage trend and the league-wide add market are scored explicitly instead
  of waited for. Those adds are labelled `breakout` to keep them distinct from
  an `upgrade` that helps you immediately.

## Beyond the projections

> the Jets might be good on paper but they are probably not a good team

Right — and that is a claim about the projections being wrong. Four signals
measure the things it could be wrong about, each derived from data rather than
opinion and each required to explain itself in a sentence that ends up on
screen next to the player:

| Signal | What it measures | Where it comes from |
| --- | --- | --- |
| Signal | What it measures | Where it comes from | Moves the number? |
| --- | --- | --- | --- |
| **Team reality** | whether an offense actually delivers what it is projected | actual points scored by a team's players vs what was projected for those same players, week by week | no — shown |
| **Matchup** | whether this week's opponent is soft or tough for the position | fantasy points each defense has really allowed to each position, vs the league average | no — shown |
| **Role** | whether he is still the guy getting the ball | snap share and touch share, recent games vs the earlier baseline; a game he left injured is left out | no — shown |
| **Availability** | an injury tag as a discount, not a footnote | Sleeper's injury status | **yes** |

Only availability is priced. The other three used to tilt each projection by
up to 30%, until a backtest over 2,156 player-games (2025 weeks 8–17, 2026
weeks 3–5) showed every one of them making it worse:

| | Close start/sits called right | Average miss |
| --- | --- | --- |
| **Sleeper projection alone** | **56.8%** | **5.33 pts** |
| + team form | 56.0% | 5.34 |
| + role | 56.0% | 5.38 |
| + matchup | 55.6% | 5.34 |
| + all three | 54.9% | 5.39 |
| + the player's own form, opponent-adjusted | 55.3% | 5.45 |

"Close" means two players at the same position projected within 3 points of
each other. The projection already reads recent box scores, usage and the
opponent, so pricing them again counted them twice. They still appear as the
reasons beside a call — a tough matchup is worth knowing — but they no longer
bend the number. Note how low the ceiling is: the projection itself calls only
57% of close start/sits right. Most of a week's spread is noise.

The waiver board still uses the role trend to spot `breakout` adds; that is a
different question, and the backtest did not cover it.

### Your own read

The measurements still cannot see a coordinator you do not trust, a line about
to get worse, or a rookie QB whose numbers have not caught up to the tape. That
goes in `opinions.toml`:

```toml
[teams]
NYJ = { adjust = -0.15, why = "good on paper, not a good football team" }

# Reaches QB/RB/WR/TE by default. Widen or narrow it explicitly:
DEN = { adjust = -0.08, why = "committee backfield", positions = ["RB"] }

[players]
"Some Rookie" = 0.08
"New York Jets" = -0.10          # the defense, specifically
```

A team entry is a claim about an **offense**, so it moves QB, RB, WR and TE and
stops there. A kicker's value partly *inverts* with offensive quality — drives
that stall inside the 40 become field goal attempts — and a team defense scores
off turnovers and sacks, which have little to do with its own offense. Name the
kicker or the defense under `[players]` to move it anyway, or set `positions`.

The test for whether something belongs here: **would the projection already
know it?** If yes, leave it out — the backtest above is what double-counting
costs. What belongs is what it is blind to: offseason changes, coaching and
scheme, a role change before it reaches the depth chart, or production you
think is unsustainable.

Numbers are fractional adjustments to a projection, clamped at ±0.35, and
with injuries they are the only thing that moves one. They are always named in the
report, so a lineup call you do not recognize can be traced back to the line
that caused it. None ship by default: copy `opinions.example.toml` to
`opinions.toml` to start one, and under Docker uncomment its mount in
`docker-compose.yml`.

### Before the season starts

The context notes need completed games. In September there are none, so the
window walks back into the previous season at half weight, week 18 excluded
(it is played by resting starters and tells you nothing). The report says so
when this is happening, and the notes read "last season" rather than pretending
to be current. Rosters change over an offseason, so this is the moment your
`opinions.toml` carries the most weight.

## Extending it

The platform adapters implement one small interface (`ffm/platforms/base.py`):
`league_settings()`, `fetch_state()`, `resolve_player()`. `SleeperLeague` in
`ffm/platforms/league.py` is the in-season equivalent. Everything downstream
speaks the neutral models in `ffm/models.py`.

That's what made start/sit and waivers cheap to add — both reuse the same
league settings, the same scoring engine, the same lineup optimizer and the
same replacement levels the draft path already builds. **Trades** are the next
one for free: the same lineup delta evaluated on both sides at once, with the
context layer applied to both rosters.

## Layout

```
ffm/
  models.py            neutral Player / LeagueSettings / DraftState / Roster
  cache.py             TTL disk cache (stale-on-error, for mid-draft blips)
  data/
    sleeper_data.py    player universe, season projections, ADP, bye weeks
    weekly.py          weekly projections, weekly results, schedule, trending
    scoring.py         stat line -> league points
    context.py         the four signals, plus your opinions.toml
  platforms/
    base.py            the draft adapter interface
    sleeper.py         public API, no auth
    league.py          in-season league: rosters, lineups, waiver pool
    yahoo.py           OAuth2 + Yahoo's nested JSON
    mock.py            offline simulated draft
  engine/
    lineup.py          exact optimal lineup (Hungarian) — shared by both paths
    replacement.py     replacement levels + VOR
    survival.py        will he last until my next pick?
    tiers.py           tier break detection
    recommend.py       VONA scoring and the final draft ranking
    startsit.py        weekly lineup, and the diff against what you have set
    waivers.py         pickups, drops, and telling breakouts from upgrades
    news.py            what changed since the last look: injuries,
                       depth-chart promotions, market spikes
  ui/
    dashboard.py       the live draft board
    week.py            the weekly report
  brief.py             optional LLM summary, with the guards that make it safe
  notify.py            Telegram / ntfy / Discord, and report -> message
  watch.py             the polling loop, and the rule that you hear each
                       thing exactly once
  runner.py            draft poll loop
  season.py            wires the in-season pieces together
  cli.py               entry point

Dockerfile             two-stage uv build; venv only in the runtime image
docker-compose.yml     the long-running watcher, with its state volume
uv.lock                the exact dependency tree, used by both
opinions.toml          your reads (see Beyond the projections)
```

## Tests

```bash
uv run pytest -q
```

Covers snake/linear/third-round-reversal pick math, the lineup optimizer
against brute force, scoring across formats, survival conditioning, replacement
levels and roster caps.

`tests/test_season.py` covers the in-season half offline: that no combination
of signals can swing a projection past the cap, that a thin sample is shrunk,
that an Out player is zeroed however good his matchup, that a quarterback is
judged on snaps rather than his share of team rushing, that relabelling RB2 as
FLEX is not reported as a lineup change, that a player on IR never surfaces as
a waiver target however hot his usage looks, and that the drop suggestion is
never a player the start/sit report is simultaneously telling you to start.

`tests/test_notify.py` covers delivery, where the interesting property is
silence: that nothing is ever sent twice, that a fresh state file sends one
digest instead of replaying the week, that a large burst collapses, that a new
week is not silenced by last week's alerts, and that a laptop opened at
midnight does not receive every digest the day had scheduled.

`tests/test_news.py` covers the change detector, where the two failure modes
pull against each other: missing the injury that opens a job, and producing a
wall nobody reads. It pins that a cold start reports no injuries, that another
manager's Questionable player is never mentioned, that the next man up skips
backups who are owned or hurt, that a handcuff for your own player is urgent,
and that an injury you already heard about is not repeated.

`tests/test_brief.py` covers the summary guards: that a derived total is
caught, that a real number borrowed for a fabricated stat is caught by the
vocabulary check, that whole-number rounding is not allowed (it once let an
invented "26.5 point swing" through off a real 26), and that no key, no
network, a malformed reply or an empty completion all fall back silently.

`ffm check` is the other half: it runs the engine across ~64 simulated draft
states in four league formats and asserts the invariants that unit tests can't
easily reach — VONA equals marginal gain minus expected value next turn,
waiting a turn is never worth more than picking now, survival is monotonic in
ADP within a position, the board is ordered by score, and the headline pick is
never also listed as gone. The engine reasons about two points in the draft at
once (the board now, and the board at your next pick) and every real bug so far
has come from mixing those frames, which is what these checks are aimed at.

## Caveats

- Projections and ADP are Sleeper's. They're solid and current, but they are one
  source; `--bpa` lets you lean toward your own read of the board, and
  `opinions.toml` lets you overrule it in season.
- The context signals are measurements, not truth. Team form is projection
  error, which is exactly what you want for "good on paper" but will also read
  a hot streak as a good offense. Points allowed by position does not adjust
  for the offenses a defense happened to face. Both are bounded and both show
  their working, so treat them as a thumb on the scale rather than an answer.
- The first in-season run fetches a few weeks of history and takes a minute.
  Completed weeks never change, so they are cached hard and later runs are
  quick. `ffm cache-clear` if you want it refetched.
- Waiver adds are ranked, not bid. There is no FAAB advice, and the tool never
  submits a claim — it tells you what to go and do.
- Yahoo exposes the draft through `draftresults`, which updates as picks land
  but is not a push feed — expect a few seconds of lag at the default poll rate.
- Auction drafts are not supported yet; the engine assumes snake or linear.
