/**
 * Run with:  npx tsx examples/basic.ts
 *
 * Bring your own follower data — from the X API, a CSV, a scrape, anywhere —
 * normalize it, and audit it. Nothing here touches the network.
 */
import { normalizeXUser, auditFollowers, scoreFollower } from '../src/index.js';

// Pretend these came back from an X/Twitter followers endpoint.
const rawFollowers = [
  { screen_name: 'real_dev', name: 'Real Dev', description: 'building things',
    profile_image_url_https: 'https://x/real.jpg', created_at: '2019-01-01T00:00:00.000Z',
    followers_count: 800, friends_count: 300, statuses_count: 4200 },
  { screen_name: 'no_bio_human', name: 'Quiet One', // no bio — still scores as real
    profile_image_url_https: 'https://x/pic.jpg', created_at: '2018-06-01T00:00:00.000Z',
    followers_count: 210, friends_count: 190, statuses_count: 1300 },
  { screen_name: 'obvious_bot', name: 'x', // no pic, no tweets, brand new, mass-follow
    created_at: new Date().toISOString(),
    followers_count: 0, friends_count: 8000, statuses_count: 0 },
];

const followers = rawFollowers.map(normalizeXUser);

console.log('Per-follower scores:');
for (const f of followers) {
  console.log(`  @${f.username}: ${scoreFollower(f)}`);
}

const audit = auditFollowers(followers);
console.log('\nCohort audit:');
console.log(`  quality score : ${audit.qualityScore}`);
console.log(`  breakdown     : ${JSON.stringify(audit.breakdown)}`);
console.log(`  red flags     : ${audit.redFlags.join(' | ') || 'none'}`);
console.log(`  verdict       : ${audit.verdict}`);
