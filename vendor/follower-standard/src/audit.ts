import type { FollowerData, FollowerAudit, ScoredFollower } from './types.js';
import { scoreFollower, classifyFollower, buildReasons } from './score.js';

/** Score one follower and attach its class and reasons. */
export function scoreFollowerFull(f: FollowerData): ScoredFollower {
  const score = scoreFollower(f);
  return { ...f, score, classification: classifyFollower(score), reasons: buildReasons(f) };
}

/**
 * Audit a set of followers into a cohort-level quality report.
 *
 * Pure and deterministic (aside from account-age, which is relative to now):
 * pass in an array of followers, get back the breakdown, quality score,
 * red flags, verdict, and the worst offenders. No network, no I/O.
 */
export function auditFollowers(followers: FollowerData[]): FollowerAudit {
  const total = followers.length;
  if (total === 0) {
    return {
      followersScored: 0,
      qualityScore: 0,
      breakdown: { real: 0, suspicious: 0, bot: 0 },
      redFlags: [],
      verdict: 'No followers to audit.',
      flagged: [],
    };
  }

  const scored = followers.map(scoreFollowerFull);

  const counts = { real: 0, suspicious: 0, bot: 0 };
  for (const f of scored) counts[f.classification]++;
  const breakdown = {
    real: Math.round((counts.real / total) * 1000) / 10,
    suspicious: Math.round((counts.suspicious / total) * 1000) / 10,
    bot: Math.round((counts.bot / total) * 1000) / 10,
  };
  const qualityScore = Math.round(scored.reduce((sum, f) => sum + f.score, 0) / total);

  const redFlags: string[] = [];
  const pctWhere = (pred: (f: ScoredFollower) => boolean) =>
    Math.round((scored.filter(pred).length / total) * 100);
  const noTweetsPct = pctWhere((f) => (f.public_metrics?.tweet_count || 0) === 0);
  const newAcctPct = pctWhere((f) => {
    if (!f.created_at) return false;
    return Date.now() - new Date(f.created_at).getTime() < 30 * 24 * 60 * 60 * 1000;
  });
  const defaultPicPct = pctWhere(
    (f) => !f.profile_image_url || f.profile_image_url.includes('default_profile'),
  );
  if (noTweetsPct > 10) redFlags.push(`${noTweetsPct}% of followers have zero tweets`);
  if (newAcctPct > 10) redFlags.push(`${newAcctPct}% of followers were created in the last 30 days`);
  if (defaultPicPct > 10) redFlags.push(`${defaultPicPct}% of followers have default profile pictures`);
  if (breakdown.bot > 10) redFlags.push(`${breakdown.bot}% classified as likely bots`);

  let verdict: string;
  if (qualityScore >= 80) verdict = 'Excellent audience quality. Very few suspicious accounts detected.';
  else if (qualityScore >= 65) verdict = 'Above average quality. Some suspicious followers detected but within normal range.';
  else if (qualityScore >= 50) verdict = 'Average quality. Notable presence of suspicious or inactive accounts.';
  else if (qualityScore >= 35) verdict = 'Below average. Significant number of suspicious followers detected. Consider cleaning your audience.';
  else verdict = 'Poor quality. High percentage of bot-like and suspicious accounts. Audience cleanup strongly recommended.';

  const flagged = scored
    .filter((f) => f.classification !== 'real')
    .sort((a, b) => a.score - b.score)
    .slice(0, 10)
    .map((f) => ({
      username: f.username,
      name: f.name,
      score: f.score,
      classification: f.classification,
      reasons: f.reasons,
    }));

  return { followersScored: total, qualityScore, breakdown, redFlags, verdict, flagged };
}
