import { describe, it, expect } from 'vitest';
import { scoreFollower, classifyFollower, buildReasons } from './score.js';
import { auditFollowers } from './audit.js';
import { normalizeXUser } from './normalize.js';
import type { FollowerData } from './types.js';

function perfect(): FollowerData {
  return {
    id: '123',
    username: 'good_user',
    name: 'Good User',
    description: 'A real person who tweets about real things.',
    profile_image_url: 'https://pbs.twimg.com/profile_images/123/real_200x200.jpg',
    created_at: new Date(Date.now() - 365 * 24 * 60 * 60 * 1000).toISOString(),
    public_metrics: { followers_count: 500, following_count: 300, tweet_count: 1500, listed_count: 5 },
    verified: false,
  };
}

describe('classifyFollower thresholds', () => {
  it('70 is the inclusive real boundary; 40 is bot', () => {
    expect(classifyFollower(100)).toBe('real');
    expect(classifyFollower(70)).toBe('real');
    expect(classifyFollower(69)).toBe('suspicious');
    expect(classifyFollower(41)).toBe('suspicious');
    expect(classifyFollower(40)).toBe('bot');
    expect(classifyFollower(0)).toBe('bot');
  });
});

describe('scoreFollower', () => {
  it('a perfect follower scores 100', () => {
    expect(scoreFollower(perfect())).toBe(100);
  });

  it('does NOT penalise a missing bio (weak humanity signal)', () => {
    const f = perfect();
    delete f.description;
    expect(scoreFollower(f)).toBe(100);
    expect(buildReasons(f)).not.toContain('No bio');
  });

  it('penalises default / missing profile picture (-20)', () => {
    const f = perfect();
    f.profile_image_url = 'https://pbs.twimg.com/sticky/default_profile_images/default_profile_normal.png';
    expect(scoreFollower(f)).toBe(80);
    const g = perfect();
    delete g.profile_image_url;
    expect(scoreFollower(g)).toBe(80);
  });

  it('penalises tweet activity (-30 for zero, -15 for <5, none for 5+)', () => {
    const zero = perfect(); zero.public_metrics!.tweet_count = 0;
    expect(scoreFollower(zero)).toBe(70);
    const few = perfect(); few.public_metrics!.tweet_count = 4;
    expect(scoreFollower(few)).toBe(85);
    const ok = perfect(); ok.public_metrics!.tweet_count = 5;
    expect(scoreFollower(ok)).toBe(100);
  });

  it('penalises account age (-30 <7d, -20 <30d, -10 <90d, none 90d+)', () => {
    const mk = (days: number) => {
      const f = perfect();
      f.created_at = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
      return scoreFollower(f);
    };
    expect(mk(3)).toBe(70);
    expect(mk(15)).toBe(80);
    expect(mk(60)).toBe(90);
    expect(mk(100)).toBe(100);
  });

  it('penalises lopsided follow ratios', () => {
    const mk = (followers: number, following: number) => {
      const f = perfect();
      f.public_metrics!.followers_count = followers;
      f.public_metrics!.following_count = following;
      return scoreFollower(f);
    };
    expect(mk(10, 600)).toBe(75); // ratio 60 → -25
    expect(mk(10, 250)).toBe(85); // ratio 25 → -15
    expect(mk(10, 150)).toBe(90); // ratio 15 → -10
    expect(mk(500, 300)).toBe(100); // healthy
  });

  it('penalises mass-follow shape and zero-followers-many-following', () => {
    const mass = perfect();
    mass.public_metrics!.followers_count = 50;
    mass.public_metrics!.following_count = 10_000;
    expect(scoreFollower(mass)).toBe(60); // ratio 200 (-25) + mass-follow (-15)

    const zero = perfect();
    zero.public_metrics!.followers_count = 0;
    zero.public_metrics!.following_count = 300;
    expect(scoreFollower(zero)).toBe(55); // ratio 300 (-25) + zero-followers (-20)
  });

  it('clamps to [0,100] and handles missing metrics', () => {
    expect(scoreFollower(perfect())).toBe(100);
    const bare: FollowerData = { id: '1', username: 'x', name: 'X' };
    // no picture (-20) + zero tweets (-30) = 50; missing bio not penalised
    expect(scoreFollower(bare)).toBe(50);
  });
});

describe('buildReasons', () => {
  it('is empty for a perfect follower and never cites a missing bio', () => {
    expect(buildReasons(perfect())).toEqual([]);
    const f = perfect();
    delete f.description;
    expect(buildReasons(f)).toEqual([]);
  });

  it('stacks concrete reasons', () => {
    const f = perfect();
    f.profile_image_url = 'https://pbs.twimg.com/sticky/default_profile_images/default_profile.png';
    f.public_metrics!.tweet_count = 0;
    f.created_at = new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString();
    const reasons = buildReasons(f);
    expect(reasons).toContain('Default profile picture');
    expect(reasons).toContain('Zero tweets');
    expect(reasons.some((r) => r.includes('days old'))).toBe(true);
  });
});

describe('auditFollowers', () => {
  it('summarises a mixed cohort', () => {
    const real = Array.from({ length: 8 }, (_, i) => ({ ...perfect(), id: `r${i}`, username: `real_${i}` }));
    const bot: FollowerData = {
      id: 'b1', username: 'bot_1', name: 'x',
      public_metrics: { followers_count: 0, following_count: 5000, tweet_count: 0, listed_count: 0 },
      created_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
    };
    const audit = auditFollowers([...real, bot, { ...bot, id: 'b2', username: 'bot_2' }]);
    expect(audit.followersScored).toBe(10);
    expect(audit.breakdown.real).toBe(80);
    expect(audit.breakdown.bot).toBe(20);
    expect(audit.flagged.length).toBeGreaterThan(0);
    expect(audit.redFlags.some((r) => r.includes('likely bots'))).toBe(true);
  });

  it('handles an empty set without throwing', () => {
    const audit = auditFollowers([]);
    expect(audit.followersScored).toBe(0);
    expect(audit.verdict).toMatch(/no followers/i);
  });
});

describe('normalizeXUser', () => {
  it('maps v1/v2/mirror field aliases into FollowerData', () => {
    const f = normalizeXUser({
      screen_name: 'alice', name: 'Alice', description: 'hi',
      profile_image_url_https: 'https://x/pic.jpg', created_at: '2020-01-01T00:00:00.000Z',
      followers_count: 100, friends_count: 50, statuses_count: 900, isBlueVerified: true,
    });
    expect(f.username).toBe('alice');
    expect(f.public_metrics?.following_count).toBe(50);
    expect(f.public_metrics?.tweet_count).toBe(900);
    expect(f.verified).toBe(true);
  });
});
