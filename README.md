# Internship Cold-Email Kit

A small, transparent **command-line tool** for sending **personalized** internship
(or job) cold emails from your Gmail — with a built-in "never email the same place
twice" safety net, a human approval step before anything goes out, and automatic
follow-up tracking.

You keep a simple spreadsheet (CSV) of who you want to contact and one email
template. The tool fills in the template per recipient, attaches your resume, lets
you preview everything, sends through Gmail, and remembers exactly who you've
contacted and who replied.

> **Is this an app with a UI?** No. It's a **terminal/command-line tool** (you type
> `outreach …` in a shell). There's no window, website, or server. Your data lives in
> plain CSV files on your own machine.

<p align="center">
  <img src="assets/workflow.png" alt="How the kit works: CSV and template, personalize, attach resume, preview, send via Gmail, never email twice" width="640">
</p>

---

## Coffee chats and referrals (two email types)

This version adds **two email types**, each with its own template that **you write**:

| `type` in CSV | Template | Resume attached? |
|---|---|---|
| `coffee_chat` | `templates/coffee_chat.txt` | no (by default) |
| `referral` | `templates/referral.txt` | yes |

```bash
cp templates/coffee_chat.txt.example templates/coffee_chat.txt   # then write your own words
cp templates/referral.txt.example templates/referral.txt
```

Each CSV row picks its template with the `type` column (leave it blank and `stage` picks it
for you, see below). Every other column can be used as a `{{blank}}` in your template (e.g.
`first_name`, `company`, `title`, `connection`, `target_role`, `job_link`, `subject`).
See `mailmerge_database.csv.example`.

Safety checks added:
- **Blank check** — if any `{{blank}}` a row's template uses is empty, nothing is sent and
  the problem rows are listed.
- **Unknown type** — a typo like `coffe_chat` stops the send instead of using the wrong email.
- **Test send** — `outreach-send --test` sends the first 2 emails to **your own** address
  (subject starts with `[TEST to …]`). Nothing is logged.

### Staging rules (all adjustable in `config.toml`)

`outreach stage` picks today's batch from the queue using these rules:

| Rule | Setting | Default |
|---|---|---|
| Email type, when `type` is blank | `[routing]` | `coffee_chat` if 3+ years at the company (`start_date`) or a lead / manager / director / principal / staff / head `title`; otherwise `referral` |
| Order (who goes first) | target list, `[priority]` | **data/ML/AI job** first, then **company hiring a Data/ML/AI intern** (`data/open_roles.csv`, or `open_intern_roles = yes` on the row), then **tier** (1 big tech → 2 finance/banking/consulting → 3 other big companies → not listed), then matching `school` (e.g. Emory) or a tag in `priority_tags` |
| Company tiers | `data/target_companies.csv` | you edit it: `company, domain, tier`; matched by company name or email domain. A `tier` typed in the row wins |
| Smallest company | `[company_limits].min_employees` | 1,000: smaller companies are skipped (they stay in the queue) |
| Never cold email | `data/do_not_contact.csv` | people you already know: removed at `stage` and skipped at send. Match by email, or first name (+ last name) (+ company). Private: not committed; see the `.example` |
| Automatic tags | `[priority.surname_lists]` | `last_name` found in `data/chinese_surnames.txt` → `chinese` tag |
| People per company | `[company_limits]` | 5 if 1,000–9,999 employees, 7 if 10,000+, 4 if `company_size` is blank (3 if under 1,000, if you lower the floor) |
| Emails per day | `[daily]` | 6 coffee chats + 8 referrals (counts what's staged and sent today) |

People held back by a limit stay in the queue for another day. A `type` you fill in by hand
always wins. `outreach status` shows reply rates by type, tier and priority group, so you can tune
the numbers after a week or two.

### Safer sending (all adjustable in `config.toml`)

| Feature | Setting | Default |
|---|---|---|
| Volume ramp | `[ramp].steps` | 13/day for 3 days → 18 for 7 → 25 for 7 → 35 max, from your first real send; split between types like `[daily]` |
| Warning signs → pause | `[ramp]` | bounces over 3% of your last 50 emails, a Gmail sending-limit error, or a spam check in spam → pause 2 days, then one step down |
| Spacing | `[sending]` | random 60–180 seconds between emails |
| Follow-up | `[followup]` | one follow-up after 3 business days with no reply, in the same email thread (`templates/followup.txt`) |
| Template versions | `[templates]` | a type can list 2–3 files; each person always gets the same one, and the version is logged |
| Shared blanks | `[blanks]` | e.g. `resume_link`, usable in any template as `{{resume_link}}` (send a link, not an attachment) |

Daily check-up: `outreach health` checks Gmail for bounces, shows the ramp step and any pause, and
reminds you to run a spam check (`outreach-send --spam-check you@outlook.com`, then
`outreach health --spam-check inbox|spam`). Run `outreach backfill-dates --apply` before staging so
nobody who replied gets a follow-up.

### Tracking what works

- `outreach report`: bounce, reply and good-result rates, overall and split by type, template
  version, tier, priority group, source (hand-picked / web / Apollo), personal line (real fact /
  plain), and open intern roles; replies to the first email vs after the follow-up; time to reply;
  Apollo credits per reply; and sending health. Groups under 20 people are marked "too few to judge".
- `outreach mark <email> <outcome>`: record what happened. Outcomes: `coffee_chat_booked`,
  `referral_given`, `interview`, `offer` (these count as good results), `not_interested`,
  `no_reply`.
- The report checks Gmail for new replies first (`--no-imap` to skip), so the numbers are current.
- `outreach feedback [--email]`: the short daily report (sent, failed, bounced, held, new replies,
  response and referral rates, Deepline credit warnings). It says "Everything's good" when nothing
  needs you; `--email` sends it to your own Gmail address.

**Resume link:** set `[blanks].resume_link_text` (e.g. "Selena Su Resume") and emails show that
short name as a clickable link instead of the long Drive address (the plain-text copy keeps the full
link).

**Data/ML/AI roles first:** people whose title has none of `[priority].role_keywords` (data, ML, AI,
scientist, analytics, ...) are staged last, only when nobody better is left; a referral counts most
from someone in the same field.

**Companies hiring interns next:** `data/open_roles.csv` (private) says, per company, whether it has
an open Summer 2027 Data/ML/AI internship (role, posting link, date checked). The contact finder
fills it every day (Round H, free web search; 15 companies a day, each re-checked weekly) with
`outreach hiring set "<company>" yes --link <posting> --role "<title>"` (or `no`). `outreach hiring`
lists the hiring companies and the ones still to check. Data/ML/AI people at hiring companies fill
the day first, ahead of tier shares; the daily report says how many emails went to them.

### Safety checks before every send (`guard.py`)

So emails can go out without you reading each one, `outreach-send` checks every email first.
Your templates are fixed text, so the checks look at the blanks and the templates:

| Check | If it fails |
|---|---|
| The template is word for word the one you approved (`outreach approve-templates`) | problem |
| No leftover `{{blank}}` or `<...>`, no links except your resume link, no words like TODO | problem |
| The personal line is one of your approved sentences (`[guard].line_patterns`), with the person's real company and correct a/an | held for review |
| The greeting name looks like a first name and matches the email address; not a shared inbox | held for review |
| The email is at the person's current company | held for review |

Held emails go to `data/held.csv` instead of being sent; the rest go out. `outreach held`
lists them, `outreach approve <email>` (or `--all`) puts them back at the front of the queue
(marked approved, so the per-person checks are skipped), `outreach approve --drop <email>`
removes them. If more than `[guard].max_problem_percent` (20%) of a batch has a problem,
**nothing** is sent (SAFETY STOP). `outreach spot-check` shows 2 random emails from the last
send day in full.

### The daily routine (Agent 2: `.claude/agents/sender.md`)

`outreach daily` runs the morning steps in order and **sends nothing**:
1. replies and send dates from Gmail (so nobody who replied gets a follow-up),
2. health (bounces, pause, ramp step, spam-check reminder),
3. stage today's batch (follow-ups first, then new people by the rules),
4. preview every email with its `>> CHECK` lines.

Then `outreach-send --test` (to you), and `outreach-send --send` once you approve, then
`outreach report`. Agent 2 does exactly this in two runs: **prepare** (daily + test email,
then it stops and shows you the batch) and, only after you approve the number, **send**.

### Finding people (Agent 1: `.claude/agents/contact-finder.md`)

| Round | Where people come from | Cost |
|---|---|---|
| H | Not people: checks careers pages for open Data/ML/AI internships (`outreach hiring`), so people at hiring companies go first | free |
| D | Deepline (hiring companies searched first): people who studied at Emory and now hold a data/ML/AI job at a target company, plus a verified work email (`outreach from-deepline`); skips anyone whose headline says they moved | Deepline credits (~0.6 per person) |
| 0 | People you save with the Apollo LinkedIn extension (`outreach from-apollo` picks them out) | 1 Apollo credit each, when you save |
| W | The agent's web search: people + an email they **published themselves** (papers, personal sites, GitHub) | free |
| 1–2 | Apollo people search + email reveal (needs a paid Apollo plan) | 1 credit per email |

Every round ends with `outreach add <file> --dry-run`, so you see each person, the agent's
`>> CHECK:` personal line with its source, and (for web emails) where the email was published,
before anything enters the queue. `outreach add` only accepts `[search].accepted_email_statuses`
(`verified` from Apollo, `public` with an `email_source` seen within `max_email_age_years`).
Emails are never guessed.

Extra CSV columns the rules read: `last_name`, `company_size`, `title`, `start_date`
(`YYYY-MM-DD`, `YYYY-MM` or `YYYY`), `school`, `priority_tags`, `tier`, `open_intern_roles`,
`linkedin_url`.

---

## Contents
- [The Overview](#the-60-second-mental-model)
- [Requirements](#requirements)
- [Install](#install)
- [First-time setup](#first-time-setup)
- [Your daily workflow](#your-daily-workflow)
- [Command reference](#command-reference)
- [The files explained](#the-files-explained)
- [Worked example](#worked-example-from-zero-to-sent)
- [Follow-ups & reply tracking](#follow-ups--reply-tracking)
- [Sending real email — please read](#sending-real-email--please-read)
- [Privacy & your data](#privacy--your-data)
- [Troubleshooting / FAQ](#troubleshooting--faq)
- [For developers](#for-developers)
- [License](#license)

---

## The Overview

Everything is a **funnel made of three CSV files**. A company moves left-to-right and
lives in exactly one place at a time:

```
  outreach_queue.csv  ──stage──▶  mailmerge_database.csv  ──send──▶  sent_log.csv
   (leads you've         (this batch,                  (history: everyone
    researched but        about to go out,              you've emailed, when,
    NOT emailed yet)       previewed first)              and who replied)
```

- **`stage`** moves a chosen number of leads from the queue into the outgoing batch.
- **`preview`** shows you the exact emails. It **never sends.**
- **`outreach-send --send`** is the *only* command that emails. It previews, asks you
  `y/N`, then sends — and immediately records each one in `sent_log.csv`.

Because a sent company lands in `sent_log.csv` and the tool checks that log before
every send, **you can never accidentally email the same place twice.**

You run the whole thing with two commands:
- **`outreach …`** — does everything *up to* a previewed batch (safe to run freely).
- **`outreach-send`** — the single "actually send" door, gated by your approval.

---

## Requirements
- **Python 3.11 or newer** (the tool uses the built-in `tomllib`). Check with
  `python3 --version`.
- A **Gmail account** with a **Gmail App Password** (not your normal password — see
  setup below).
- No other required dependencies. Works on macOS, Linux, and Windows.

---

## Install
```bash
git clone https://github.com/Mianmalol/internship-cold-email-kit.git
cd internship-cold-email-kit

pip install .                 # installs the `outreach` and `outreach-send` commands
```
Optional extras:
```bash
pip install ".[keyring]"      # cross-platform password storage (recommended)
pip install -e ".[dev]"       # for contributors: editable install + pytest + ruff
```

After this, `outreach` and `outreach-send` are available in your terminal.

---

## First-time setup

**1. Create your config and template.**
```bash
outreach setup                # creates config.toml in the current folder
cp mailmerge_template.txt.example mailmerge_template.txt
```
`outreach setup` writes a starter `config.toml`. Open it and fill in your Gmail
address, the path to your resume, and the kind of roles you're targeting:
```toml
[email]
address = "you@gmail.com"

[files]
resume = "resume.pdf"          # put your resume here, matching this name

[pacing]
batch_size = 14                # how many to send per day by default

[outreach]
area = "Machine learning + physics simulation internships, remote, unpaid"
```

**2. Edit your email template** (`mailmerge_template.txt`). Anything in `{{double
braces}}` gets filled in per recipient from your CSV:
```
TO: {{contact_email}}
SUBJECT: {{subject}}
FROM: Your Name <you@gmail.com>
ATTACHMENT: resume.pdf

Hi {{contact_name}},

I'd love to intern at {{company}}. {{note}}.

Best,
Your Name
```

**3. Get a Gmail App Password** (this is *not* your login password):
- Turn on 2-Step Verification: https://myaccount.google.com/security
- Create an App Password: https://myaccount.google.com/apppasswords

**4. Store the password once.** Pick the one that fits your setup (the tool checks
them in this order):
```bash
keyring set mailmerge-gmail "$USER"                              # any OS — recommended
security add-generic-password -s mailmerge-gmail -a "$USER" -w   # macOS Keychain
export OUTREACH_GMAIL_APP_PASSWORD=...                           # quick/CI only (see note)
```
> The env-var option is handy for testing but less safe (it can leak via shell
> history and logs) — prefer `keyring` or the macOS Keychain for everyday use.

**5. Confirm everything's wired up:**
```bash
outreach setup
```
```
config.toml: OK (/path/to/config.toml)
Resume: OK /path/to/resume.pdf
Password: OK via keyring
Sender:  you@gmail.com
Area:    Machine learning + physics simulation internships, remote, unpaid
```

> **Where does it look for config?** In order: a `--config PATH` you pass, the
> `$OUTREACH_CONFIG` environment variable, or `config.toml` in your current folder.
> All file paths in `[files]` are relative to wherever your `config.toml` lives, so you
> can keep each search (or each person) in its own folder.

---

## Your daily workflow

```bash
# 1. See where things stand
outreach status

# 2. Add leads to outreach_queue.csv (research them yourself, or use a finder).
#    Each row: contact_email,contact_name,company,role,subject,note

# 3. Move a batch into the outgoing pile (you choose how many)
outreach stage -n 14

# 4. Read the actual emails before anything is sent
outreach preview

# 5. Send — previews again, asks for confirmation, then delivers
outreach-send --send

# 6. Later: sync replies/dates from Gmail, then see who to follow up with
outreach backfill-dates --apply
outreach followup
```

That's the whole loop. Steps 1–4 are completely safe to run as often as you like;
only step 5 sends mail, and it always asks first.

---

## Command reference

### `outreach` — drive the pipeline (never sends)

| Command | What it does |
|---|---|
| `outreach status` | Funnel snapshot: how many are queued, staged, and already sent. |
| `outreach setup` | Create/verify `config.toml`, resume, password, sender, and area. |
| `outreach find` | Print a "brief" (your target area + the list of companies to exclude) to hand to a lead-finder. |
| `outreach stage -n N` | Move up to **N** leads from the queue into the outgoing batch, following the [staging rules](#staging-rules-all-adjustable-in-configtoml). Omit `-n` to use `batch_size` from config. Skips anyone already staged or sent. |
| `outreach preview` | Render the staged emails (subject + recipient). Sends nothing. |
| `outreach replies` | Quick offline check of who has replied (reads Gmail over IMAP). |
| `outreach followup` | List contacts who are past your follow-up window and **haven't replied**. |
| `outreach backfill-dates` | Sync `sent_at` + `last_reply` from Gmail (see [below](#follow-ups--reply-tracking)). Add `--apply` to write. |
| `outreach migrate` | Upgrade an older `sent_log.csv` to the current columns. |

Tip: every command accepts `--config PATH` if your config isn't in the current folder.

### `outreach-send` — the only command that emails

| Command | What it does |
|---|---|
| `outreach-send` | **Dry run** — preview every email, send nothing. |
| `outreach-send --send` | Preview, ask `Send N emails? [y/N]`, then send. |
| `outreach-send --send --yes` | Skip the confirmation (for when you're confident / automating). |

Sending is **crash-safe**: each email is logged to `sent_log.csv` the instant it
succeeds, the outgoing batch shrinks as it goes, and timestamped backups are written
to `.backups/`. If something fails mid-batch, you can just re-run — already-sent
addresses are skipped, so nothing is ever sent twice. Any email that failed to send
stays in the batch for a retry.

---

## The files explained

| File | What it is | Edit it? |
|---|---|---|
| `config.toml` | Your settings: Gmail address, file paths, daily batch size, target area. | Yes (once). |
| `mailmerge_template.txt` | Your email, with `{{placeholders}}`. | Yes. |
| `outreach_queue.csv` | **Backlog** — researched leads you haven't emailed yet. | Yes (add leads here). |
| `mailmerge_database.csv` | **This batch** — what `stage` fills and `send` empties. | Usually let the tool manage it. |
| `sent_log.csv` | **History** — everyone you've emailed, the date, and any reply. | No (the tool owns it). |
| `resume.pdf` | Attached to every email (name it to match `[files].resume`). | — |
| `.backups/` | Automatic timestamped backups before any change to your CSVs. | No. |

### CSV columns
Each lead is one row. The header names map to the `{{placeholders}}` in your template:

| column | used for |
|---|---|
| `contact_email` | the `TO:` address (**required**) |
| `contact_name` | the greeting |
| `company` | `{{company}}` |
| `role` | `{{role}}` |
| `subject` | the email subject line |
| `note` | one sentence personalized to that company |

You can add or rename columns freely — just keep your template's `{{placeholders}}` in
sync with the column names.

---

## Worked example: from zero to sent

```bash
# Put two leads in the queue (normally you'd research these):
cat >> outreach_queue.csv <<'EOF'
founders@acme.dev,Alex,Acme Robotics,Sim Intern,Robotics sim internship,"I saw your work on physics-informed simulation and would love to contribute"
careers@novalabs.io,Sam,Nova Labs,ML Intern,Remote ML internship,"Your scientific computing stack is exactly where I want to grow"
EOF

outreach status            # Queue: 2
outreach stage -n 2        # moves both into the batch
outreach status            # Staged: 2, Queue: 0
outreach preview           # shows both emails, sends nothing
outreach-send --send       # preview → "Send 2 emails? [y/N]" → y → delivered + logged
```

After sending, those two move into `sent_log.csv` and won't be contacted again.

---

## Follow-ups & reply tracking

The tool can read your Gmail to figure out **when** you emailed each company and
**whether they replied**, so follow-ups are automatic and accurate:

```bash
outreach backfill-dates          # dry run: shows what it found, writes nothing
outreach backfill-dates --apply  # writes sent_at + last_reply into sent_log.csv
outreach followup                # who's silent past your window AND hasn't replied
```

What `backfill-dates` does, in two passes over your Gmail:
1. **Send dates** — reads your **Sent** folder to record exactly when each contact was
   emailed (`sent_at`).
2. **Replies** — records who wrote back (`last_reply`). It catches replies sent from
   the same address **and** replies from a *different* address, by following the email
   thread (the `In-Reply-To`/`References` headers). Anyone with a reply is then
   automatically excluded from `followup`.

It's safe to re-run anytime (it backs up `sent_log.csv` first and never overwrites a
newer reply with an older one). No external CRM or service is needed — your Gmail is
the source of truth.

> The Gmail connector in tools like Claude remains the most thorough reply detector
> for unusual edge cases; this built-in check covers the common ones automatically.

---

## Sending real email — please read

This sends real email from **your own** Gmail account, and your account's reputation
is on the line. Use it responsibly:

- **Personalized outreach only — not bulk spam.** Make every `note` genuinely specific.
- **Keep the volume low.** The default is ~14/day (`[pacing].batch_size`). Blasting
  many near-identical cold emails at once is the main thing that trips spam filters and
  can get your account limited. Gmail also enforces daily sending limits.
- **Only email addresses meant to receive applications** (`careers@`, `hiring@`, or
  someone who invited contact). Honor any request to stop, and don't re-contact someone
  who has declined.
- **You are responsible** for complying with anti-spam laws (e.g. CAN-SPAM, GDPR) where
  you live.

---

## Privacy & your data

Everything stays **on your machine**. Your CSVs contain real people's contact details,
so the included `.gitignore` keeps private files out of git: your resume, real contacts
(`mailmerge_database.csv`, `outreach_queue.csv`), `sent_log.csv`, your filled-in
`mailmerge_template.txt`, `config.toml`, and `.backups/`. Only the `.example` files are
meant to be committed.

Never commit your app password — it belongs in a credential store (keyring/Keychain),
not in any file. See [SECURITY.md](SECURITY.md) for details and
[CONTRIBUTING.md](CONTRIBUTING.md) if you'd like to help.

---

## Troubleshooting / FAQ

**"No Gmail app password found."** You haven't stored one yet, or it's under a
different name. Re-run one of the `keyring set …` / `security add-generic-password …`
commands from setup, then `outreach setup` to confirm.

**"Template not found" / "missing header(s)."** Your `config.toml`'s `[files].template`
path is wrong, or the template is missing a `TO:` / `SUBJECT:` line at the top.

**Nothing happens / "No targets… Nothing to send."** Your batch
(`mailmerge_database.csv`) is empty — run `outreach stage -n N` first.

**Can I run several searches separately?** Yes — give each its own folder with its own
`config.toml`, and either `cd` into it or pass `--config path/to/config.toml`.

**Does `preview`/`status` send anything or need my password?** No. Only
`outreach-send --send` sends, and only it needs the password.

**How do I choose how many to send?** With `outreach stage -n N` — staging is where you
pick the batch size. The send step emails whatever's currently staged.

**It says some replies are from a "different address" and were caught via thread-match.**
That's expected and good — it means someone replied from a personal address and the
tool still matched it to the thread.

---

## For developers
```bash
pip install -e ".[dev]"
pytest -q        # full test suite (hermetic — no real account or network needed)
ruff check .     # lint
```
CI runs tests + lint on Ubuntu (3.11, 3.13) and macOS, plus a packaging smoke test.
The codebase is small and standard-library only:
- `common.py` — shared helpers (config, credentials, SMTP/IMAP, CSV I/O).
- `outreach_cli.py` — the `outreach` command (everything except sending).
- `send.py` — the `outreach-send` command (the only sender).
- `read_replies.py` — Gmail reply/date reading (used by `replies` and `backfill-dates`).

---

## License
MIT — see [LICENSE](LICENSE).
