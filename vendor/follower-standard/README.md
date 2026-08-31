# follower-standard

An open, transparent scorer for X/Twitter follower authenticity — the reference
implementation of the **[TweetBoost Follower Standard](https://tweetboost.ai/standard)**.

Every follower service says "real followers." This library is what we mean by
it, published so anyone can check the claim against the code instead of taking
it on trust. It's the exact scoring logic we grade our own deliveries with.

- **Pure functions.** No network calls, no database, no API keys, no secrets.
  You bring the follower data; the library judges it and shows its work.
- **Transparent by design.** The weights are in [`src/score.ts`](src/score.ts)
  and documented inline. It's a heuristic, not a black box or an ML oracle.
- **Zero runtime dependencies.**

## Install

```bash
npm install follower-standard
```

## Usage

```ts
import { normalizeXUser, auditFollowers, scoreFollower } from 'follower-standard';

// Bring your own data — from the X API, a CSV, a scrape, anywhere.
const followers = rawUsersFromAnywhere.map(normalizeXUser);

// Score one account, 0–100 (higher is more real):
scoreFollower(followers[0]); // → 100

// Or audit a whole cohort:
const audit = auditFollowers(followers);
audit.qualityScore; // mean score, 0–100
audit.breakdown;    // { real, suspicious, bot } as percentages
audit.redFlags;     // cohort-level warnings
audit.verdict;      // plain-language summary
audit.flagged;      // worst offenders, for spot-checking
```

If your data is already in the library's [`FollowerData`](src/types.ts) shape,
skip `normalizeXUser` and pass it straight in.

## How the score works

Every account starts at **100** and loses points for signals that correlate
with automation or throwaway accounts:

| Signal | Penalty |
| --- | --- |
| Default or missing profile picture | −20 |
| Zero tweets (−15 if fewer than 5) | −30 |
| Account age < 7d / < 30d / < 90d | −30 / −20 / −10 |
| Following-to-follower ratio > 50× / > 20× / > 10× | −25 / −15 / −10 |
| Mass-follow shape (5000+ following, < 100 followers) | −15 |
| Zero followers while following 100+ | −20 |

Then: **≥ 70 → real**, **> 40 → suspicious**, otherwise **bot**.

### Why a missing bio is not penalized

A missing bio is a weak humanity signal. Plenty of long-active, obviously real
accounts run without one — common in crypto and privacy-minded niches — so the
scorer never treats the absence of a bio as evidence of a bot. See
[`src/standard.ts`](src/standard.ts).

## Honest limitations

- It's a **heuristic**, tuned against real follower data, not a guarantee. It
  will occasionally misjudge an edge-case account in either direction.
- Because the logic is public, it can in principle be gamed. That's a deliberate
  trade for transparency — and gaming it means building accounts that look
  genuinely real (real picture, real activity, real age), which is the point.
- Account-age checks are relative to the current time, so scores can drift as
  accounts age. Everything else is deterministic.

## A note for consumers rendering output

`buildReasons`, `audit.flagged`, and the follower fields include
**user-controlled strings** (usernames, display names). This library returns
them as plain text — if you render them as HTML, escape them yourself. (It
performs no HTML rendering and introduces no injection surface of its own.)

## License

MIT © [TweetBoost](https://tweetboost.ai)
