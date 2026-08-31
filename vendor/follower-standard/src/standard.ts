/**
 * The Follower Standard — the plain-English definition of a real follower that
 * this scorer is the reference implementation of.
 *
 * Every service says "real followers." This is what we mean by it, published
 * so anyone can check the claim against the code below rather than take it on
 * trust. See https://tweetboost.ai/standard.
 */
export const FOLLOWER_STANDARD = [
  '3+ month old account',
  'Profile photo and human-looking name',
  'Real post or reply history',
  'Active within the last 60 days',
  'No blank, default, or spam-only profiles',
  'No obvious mass-follow bot behavior',
] as const;

/**
 * Why a bio is NOT on the list: a missing bio is a weak humanity signal.
 * Plenty of long-active, obviously real accounts run without one (common in
 * crypto and privacy-minded niches), so the scorer never treats the absence
 * of a bio as evidence of a bot.
 */
export const BIO_IS_NOT_A_BOT_SIGNAL = true;
