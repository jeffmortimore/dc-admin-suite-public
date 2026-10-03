# Institution profiles

A profile is the one file that makes an install *yours*: your instance's
base URL, your Crossref DOI prefix, and your branding (suite name,
institution name, logo, colors). It supplies the **defaults** behind
`config/settings.json`: what a first launch starts from, and what "Reset to
default branding" restores.

## Setting an institution up

```
cp config.example/profile.json config/profile.json     # fill it in, then restart the shell
```

That is the whole setup. `config/` is ignored by git, so your copy stays on
your machine. You can also just launch the suite and use the start page and
Settings, which write `config/settings.json` directly.

An institution that sets up several computers can keep its finished
profile here, as `profiles/<your-institution>.json`, and copy it into
`config/profile.json` on each one.

Resolution order (`app/settings.py: load_profile`):

1. `config/profile.json`: this install's own
2. `config.example/profile.json`: the shipped, neutral example
3. a neutral skeleton in code, so a missing or malformed file can never
   stop the shell from starting

The profile is read at start-up, so **restart the shell after editing it**.
Changing a profile does not touch an existing `config/settings.json`: saved
settings win over defaults, which is what keeps an upgrade from disturbing a
working install.

## `identity_markers`

A profile may list the names and house conventions that identify its
institution, for example its full name and any element ids its templates
use by local convention:

```
"identity_markers": ["Example University", "exu-intro"]
```

`docs/make_distribution.py --generic` refuses to build a copy for another
institution if any of these, or the profile's own host, DOI prefix or brand
colors, appear anywhere in it.

## `private_terms`

A profile may also list words that must never appear in a copy you
publish: for example a vendor's name, the vocabulary of a licence, or the
names of your own folders and disks. Each entry is a pattern
(case-insensitive, matched as whole words) and the reason it is private:

```
"private_terms": [
  {"pattern": "example vendor", "why": "a vendor's name"},
  {"pattern": "our-shared-drive", "why": "a folder on one computer"}
]
```

`docs/make_distribution.py --generic` refuses to build if any file in the
copy contains one, and also refuses when no profile declares any, because
a scan with nothing to search for cannot call a copy clean. The list lives
here rather than in source so that it leaves with the profile: the generic
build removes named profiles, and a published copy carries no list of what
was kept out of it.

## What a profile must not contain

The **Crossref deposit identity** (depositor name and email, registrant,
publisher) is not part of a profile, deliberately. It says who a DOI
deposit is *credited to*, and a deposit credited to the wrong organization
is hard to notice and awkward to undo, so it is never supplied as a
default. Set it in **Settings → Crossref deposit identity**; until it is
set, the DOI & XML Generator refuses to build a deposit rather than
guessing.

API keys are not profile material either. They live in
`config/ai_endpoints.json` and never travel with a shared copy of the suite.
