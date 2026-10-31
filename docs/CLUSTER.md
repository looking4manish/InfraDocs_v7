# Replica set — how InfraDocs chooses its primary

Several servers run InfraDocs together as one **replica set**. One member is the **primary**:
it serves the UI and collects every member's scans. The others are **secondaries**: they scan
themselves and push the result to the primary. Each member keeps its own MongoDB; there is no
shared database.

The election design follows MongoDB's replica-set protocol (pv1) and Raft leader election.
Code: `app/election.py` (pure rules), `app/election_manager.py` (one member: timers, RPCs,
locking), `app/cluster_runtime.py` (MongoDB storage, HTTP transport, background loop),
`app/api/routers/cluster.py` (endpoints).

## Voting members and failover modes

| Voting members | Mode | What happens when the primary fails |
|---|---|---|
| 1 | Standalone | Nothing to fail over to. |
| 2, 4, 6 | **Manual** | Nobody is elected. An operator presses Take over on a surviving member. |
| 3, 5, 7 | **Automatic** | The others elect a new primary, normally within 10–15 s. |

An even count is manual on purpose: a network split can leave two equal halves where neither
has a majority, so no automatic choice is safe. Add one more voter, or add the extra server as
a **non-voting** member (it scans and reports, never votes or becomes primary). At most 7
voting members, as in MongoDB.

A majority (quorum) is `floor(voters / 2) + 1`: 2 of 3, 3 of 5, 4 of 7.

## The rules

- **Heartbeats.** Every member heartbeats every other member every 2 s (all-to-all, like
  MongoDB). The primary's heartbeat also carries the replica-set configuration.
- **Election timeout.** A secondary that has not heard from the primary for 10 s stands for
  election. Each priority rank adds 1.5 s and a random 0–1 s is added, so the preferred member
  usually stands first and equal members do not split the vote forever.
- **Terms.** Every election starts a new numbered term. Every message carries the sender's
  term. A member that sees a higher term adopts it and becomes a secondary; an older primary is
  therefore fenced the moment it talks to anyone who knows better.
- **One vote per term.** A voter grants at most one vote per term and stores `(term, vote)` in
  its database before it replies, so a restart cannot vote twice.
- **Pre-vote.** Before raising its term a candidate asks whether it *would* get the votes. A
  member cut off from the others fails this check every time and never inflates the term, so
  it cannot disrupt a healthy primary when it reconnects.
- **Stickiness.** A voter that heard from a live primary within the timeout refuses to vote,
  unless the primary itself asked for the transfer.
- **Check quorum.** A primary that has not had heartbeat replies from a majority of voters for
  one timeout steps down. A primary on the minority side of a split stops acting as primary.
- **No automatic failback.** A recovered better-priority member rejoins as a secondary. Moving
  the role back is a planned transfer from the Servers page.
- **Manual mode.** A primary keeps its role across restarts. Take over raises the term above
  any term seen, so the old primary steps down at first contact.

## Membership

The primary changes the configuration one member at a time (Raft's single-server changes);
the configuration has a version and members adopt the highest version they see from the
primary. Adding a server:

1. Servers page → Add a server → choose voting or non-voting → a join code (30 min, single
   use) and a one-line command.
2. On the new server: `curl -fsSL http://<primary>:8081/join.sh | bash -s -- <code> [--name n150]`.
   The script clones the repository, installs the stack, and asks the primary to join.
3. The primary checks the code, connects back to the new server's address (both directions
   must work), adds it, and returns the shared fleet secret, the configuration and the
   administrator accounts. The new member starts as a secondary.

Removing a server tells it to leave (if reachable) and deletes its data from the primary.

## Data

Each member scans itself every 10 minutes (`scanning.interval_minutes`) and right after a new
primary is elected. A secondary pushes its scan to the current primary with its term; the
primary keeps one slice per server and refuses pushes from non-members, from a stale primary's
point of view, or when it is no longer primary.

## Tests

- `tests/test_election.py`: the rules, plus a partition simulator that runs the real `Node`
  class through crashes, splits and heals on a fake clock (25 random fault schedules) and
  checks after every step that no term ever has two primaries.
- `scripts/cluster_live_test.py`: three real API processes joined over HTTP; kills the
  primary and measures the failover, restarts it, transfers the role back, and checks that an
  isolated primary steps down.
