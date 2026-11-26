# Small-team anonymity safeguard

## The problem

The owner sees who has not submitted this week (`/missing`). That also tells
the owner who HAS submitted: everyone not on the list. Every anonymous item in
the reveal was written by one of those people. If only one person has
submitted, the owner knows who wrote every anonymous item. With two
submitters it is a coin flip per item.

## The decision

Let S be the number of users with at least one item this week (named or
anonymous) and M the number with none (S = total users - M).

| S         | Owner's missing view                              |
|-----------|---------------------------------------------------|
| 0         | Full list (every item is still to be written)     |
| 1 or 2    | Only the count: "N people have not submitted"     |
| 3 or more | Full list                                         |

- The count reads "1 person has not submitted" in the singular.
- If M = 0 the page says "Everyone has submitted" (nothing to infer).
- The minimum of 3 is one named constant, `MIN_SUBMITTERS_FOR_NAMES` in
  `app/missing.py`. It is a code constant, not a setting.
- `visible_missing(week)` returns either `MissingNames` (the list) or
  `MissingCount` (a number, no names), never both. The count form carries no
  names, so the page cannot leak them.
- S is derived from the total user count and `missing_submitters`; the
  `anonymous` column is never read, so named and anonymous items behave the
  same.
- The `/missing` route and template use `visible_missing` only.

## The reminder job

The reminder job (#19) keeps using the full `missing_submitters` list. Each
member only learns their own status from a reminder, so nothing about other
people is revealed.

## Display names

Every user has a display name chosen at sign-up (#29). It replaces the email
wherever the app shows who someone is on the reveal page and in the header.

- Named items on the reveal page show only the author's display name, never
  the email and never "name (email)". Names need not be unique and nothing
  disambiguates two people with the same name.
- The anonymous query still reads only `text` and `category`: it does not
  join `users` and never selects `display_name`, so no author data is loaded
  for anonymous items. The name "Anonymous" (any letter case) cannot be
  chosen, because the reveal page uses that word for anonymous items.
- The header shows only the viewer's own name ("Signed in as ..."), on every
  page for every viewer. No email appears on the reveal page.
- The missing-list safeguard above is unchanged: `/missing` and `/invites`
  still show emails, and `MIN_SUBMITTERS_FOR_NAMES` works as before.

## Known limits (not solved here)

- The owner knows which items are their own. With the owner and exactly two
  other submitters (S = 3), the owner's view of the other two is weaker than
  the threshold suggests.
- A two-person team's reveal is inherently weak: the owner knows which
  anonymous item is not theirs.
