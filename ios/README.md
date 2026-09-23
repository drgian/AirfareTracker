# FlightFare for iPhone

A native SwiftUI app: sign in with Apple, see the trips you're tracking and how their prices
have moved, and get a notification when one crosses your target.

Adding trips still happens on the website. This app is for watching.

## Building it

The `.xcodeproj` is generated, not committed, so it never collects merge conflicts:

```sh
brew install xcodegen      # once
cd ios && xcodegen generate
open FlightFare.xcodeproj
```

After adding or renaming a source file, run `xcodegen generate` again.

## Which API it talks to

Decided at compile time, so there's no setting to get wrong:

| Build         | API                            | Notifications |
| ------------- | ------------------------------ | ------------- |
| Debug         | `api.flightfare.io/dev`        | APNs sandbox  |
| Release       | `api.flightfare.io`            | APNs production |

The dev API only admits allow-listed emails, so a debug build signs in only as one of those.

## Notifications

The tracker sends them, not the API — it's the thing that knows when a price moved.
It needs three values in `tracker/config.json` on the machine that runs it:

```json
"apple_team_id":  "3LPUBZN4QQ",
"apns_key_id":    "<the 10-character key id>",
"apns_key_file":  "apns.p8",
"apple_bundle_id":"io.flightfare.app"
```

with the `.p8` sitting next to `tracker.py`. Leave `apns_key_id` out and the tracker
behaves exactly as it did before: email only, no push.

Two kinds are sent:

- **immediately**, when a price crosses below someone's target (the same rule as the email —
  only on the crossing, not every check while it sits below)
- **once a day**, alongside the summary email, headlining the biggest move

A device that Apple reports as gone (410, or 400 for a malformed token) is marked dead in the
`devices` table and never pushed to again.

## What isn't here yet

Adding and editing trips, the research tab, and admin. All of that is on the website.
