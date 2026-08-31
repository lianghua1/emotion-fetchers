# Security Policy

## Scope

`follower-standard` is a set of pure scoring functions. It makes no network
requests, reads no environment variables, touches no filesystem or database,
and holds no credentials. There is no server component here to attack.

The one thing worth knowing: functions like `buildReasons` and the objects in
`auditFollowers(...).flagged` echo back **user-controlled strings** (usernames
and display names) as plain text. If you render that output as HTML, escape it
on your side. The library itself performs no rendering and adds no injection
surface.

## Reporting a vulnerability

If you believe you've found a security issue, please email
**peter@tweetboost.ai** rather than opening a public issue. We'll acknowledge
within a few business days.
