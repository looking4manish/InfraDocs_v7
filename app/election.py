"""Quorum-based primary election for an InfraDocs replica set. PURE: no IO, no clocks.

The model follows MongoDB's replica-set protocol (pv1) and Raft leader election:

* TERMS. Time is divided into numbered terms. Every message carries the sender's term. A node
  that sees a higher term adopts it at once and becomes a secondary; a message with a lower
  term is answered with the newer term and otherwise ignored. A primary from an old term is
  therefore fenced the moment it talks to anyone who knows better.
* ONE VOTE PER TERM. A voter grants at most one vote in a term, and the manager persists
  `(term, voted_for)` BEFORE the reply leaves the node, so a restart cannot vote twice.
* MAJORITY. A candidate becomes primary only with votes from a strict majority of the VOTING
  members (itself included): floor(V/2) + 1. Two candidates can never both reach it in the
  same term, so there is at most one primary per term.
* PRE-VOTE. Before raising its term a candidate asks "would you vote for me?" without
  changing anyone's state. A node cut off from the set fails the pre-vote and never inflates
  the term, so it cannot disrupt a healthy primary when it reconnects.
* STICKINESS. A voter that has heard from a live primary within the election timeout refuses
  to vote (unless the primary itself asked for the transfer). Flapping links do not depose a
  healthy primary.
* CHECK QUORUM. A primary that has not had heartbeat replies from a majority of voters within
  one election timeout steps down. A primary on the minority side of a partition gives up
  instead of serving stale data alongside a new primary.
* PRIORITY. Priority 1 is the most preferred; 0 can never become primary. Better-priority
  members wait less before standing (MongoDB's "best effort" ordering), so the preferred
  surviving member usually wins. Voters do not veto on priority (pv1 has no vetoes): a veto
  can deadlock when the preferred member is visible to a voter but cut off from the rest.
  A recovered better-priority member does NOT take the primary back automatically; that is a
  planned transfer from the Servers page.

THE ODD-VOTER RULE (strict P-S-S). Automatic failover runs only when the number of voting
members is odd and at least 3 (3, 5 or 7). With an even count a network split can leave two
equal halves where neither has a majority, so elections are switched off: the set runs in
MANUAL mode, the primary keeps its role, and taking over is an explicit operator action that
raises the term (fencing the old primary when it returns). One voting member is STANDALONE.
Non-voting members (votes = 0) report scans and follow the primary but never vote or stand.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

MAX_VOTING_MEMBERS = 7          # MongoDB's limit; elections stay fast and majorities small
DEFAULT_HEARTBEAT_S = 2.0       # MongoDB heartbeatIntervalMillis default
DEFAULT_ELECTION_TIMEOUT_S = 10.0  # MongoDB electionTimeoutMillis default
PRIORITY_STAGGER_S = 1.5        # extra wait per priority rank, so the preferred member goes first
JITTER_S = 1.0                  # random spread that breaks ties between equal candidates

PRIMARY, SECONDARY, CANDIDATE = "primary", "secondary", "candidate"
AUTOMATIC, MANUAL, STANDALONE = "automatic", "manual", "standalone"


# ───────────────────────────── membership ─────────────────────────────

@dataclass
class Member:
    node_id: str
    address: str = ""
    priority: int = 1           # 1 best … 0 never primary
    votes: int = 1              # 1 voting, 0 non-voting

    @property
    def electable(self) -> bool:
        return self.votes == 1 and self.priority > 0


@dataclass
class Config:
    """The replica-set configuration. `version` only ever increases; every node adopts the
    highest version it sees from the current primary."""
    version: int = 1
    members: List[Member] = field(default_factory=list)
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    election_timeout_s: float = DEFAULT_ELECTION_TIMEOUT_S

    def member(self, node_id: str) -> Optional[Member]:
        return next((m for m in self.members if m.node_id == node_id), None)

    @property
    def voters(self) -> List[Member]:
        return [m for m in self.members if m.votes == 1]

    def to_dict(self) -> dict:
        return {"version": self.version, "members": [asdict(m) for m in self.members],
                "heartbeat_s": self.heartbeat_s, "election_timeout_s": self.election_timeout_s}

    @staticmethod
    def from_dict(d: Optional[dict]) -> "Config":
        d = d or {}
        return Config(
            version=int(d.get("version", 1)),
            members=[Member(**{k: m[k] for k in ("node_id", "address", "priority", "votes") if k in m})
                     for m in d.get("members", [])],
            heartbeat_s=float(d.get("heartbeat_s", DEFAULT_HEARTBEAT_S)),
            election_timeout_s=float(d.get("election_timeout_s", DEFAULT_ELECTION_TIMEOUT_S)),
        )


def quorum(voting: int) -> int:
    """Votes needed for a majority: floor(V/2) + 1."""
    return voting // 2 + 1


def failover_mode(cfg: Config) -> tuple:
    """(mode, reason). AUTOMATIC only for an odd voting count of 3 or more."""
    v = len(cfg.voters)
    if v <= 1:
        return STANDALONE, "1 voting member"
    if v % 2 == 0:
        return MANUAL, f"{v} voting members — even count"
    return AUTOMATIC, f"{v} voting members"


def validate_config(cfg: Config) -> Optional[str]:
    """Few-words reason the config is unusable, or None."""
    ids = [m.node_id for m in cfg.members]
    if len(ids) != len(set(ids)):
        return "duplicate member"
    if len(cfg.voters) == 0:
        return "no voting member"
    if len(cfg.voters) > MAX_VOTING_MEMBERS:
        return f"more than {MAX_VOTING_MEMBERS} voting members"
    for m in cfg.members:
        if m.votes not in (0, 1):
            return f"{m.node_id}: votes must be 0 or 1"
        if m.priority < 0 or m.priority > 99:
            return f"{m.node_id}: priority must be 0–99"
        if m.votes == 0 and m.priority != 0:
            return f"{m.node_id}: a non-voting member has priority 0"
    if not any(m.electable for m in cfg.members):
        return "no member can become primary"
    if cfg.election_timeout_s < 3 * cfg.heartbeat_s:
        return "election timeout must be at least 3 heartbeats"
    return None


def _priority_rank(cfg: Config, node_id: str) -> int:
    """0 for the most preferred electable member, 1 for the next, …"""
    order = sorted((m for m in cfg.members if m.electable), key=lambda m: (m.priority, m.node_id))
    for i, m in enumerate(order):
        if m.node_id == node_id:
            return i
    return len(order)


def election_timeout_for(cfg: Config, node_id: str, rng: Optional[random.Random] = None) -> float:
    """Base timeout + a stagger per priority rank + jitter. Randomised so equal members do not
    split the vote forever; staggered so the preferred member usually stands first."""
    rng = rng or random
    return cfg.election_timeout_s + _priority_rank(cfg, node_id) * PRIORITY_STAGGER_S + rng.uniform(0, JITTER_S)


# ───────────────────────────── node state ─────────────────────────────

@dataclass
class State:
    """Everything a node must remember. `term` and `voted_for` are persisted before replies."""
    node_id: str
    config: Config
    term: int = 0
    voted_for: Optional[str] = None
    role: str = SECONDARY
    leader_id: Optional[str] = None
    last_leader_contact: Optional[float] = None   # monotonic seconds when a valid primary was last heard
    stepped_down_until: float = 0.0                # a primary that stepped down will not stand until then
    acks: Dict[str, float] = field(default_factory=dict)  # primary only: member -> last heartbeat reply time
    primary_since: Optional[float] = None

    @property
    def me(self) -> Optional[Member]:
        return self.config.member(self.node_id)

    def adopt_term(self, term: int) -> bool:
        """Move to a newer term as a secondary. Returns True when the term changed."""
        if term > self.term:
            self.term = term
            self.voted_for = None
            if self.role != SECONDARY:
                self.role = SECONDARY
                self.primary_since = None
            self.leader_id = None
            self.acks.clear()
            return True
        return False


def leader_alive(st: State, now: float) -> bool:
    return (st.leader_id is not None and st.last_leader_contact is not None
            and now - st.last_leader_contact < st.config.election_timeout_s)


# ───────────────────────────── votes ─────────────────────────────

def vote_decision(st: State, req: dict, now: float, *, pre_vote: bool) -> tuple:
    """Decide a (pre-)vote request. Returns (granted, reason).

    `req` = {term, candidate_id, transfer}. For a real vote the caller has ALREADY applied
    `adopt_term(req.term)` when that term is newer; a pre-vote never touches state.
    """
    cand_id = req.get("candidate_id")
    term = int(req.get("term", 0))
    transfer = bool(req.get("transfer"))
    me = st.me
    cand = st.config.member(cand_id) if cand_id else None
    mode, _ = failover_mode(st.config)

    if me is None or me.votes != 1:
        return False, "not a voting member"
    if cand is None:
        return False, "candidate not in config"
    if not cand.electable:
        return False, "candidate cannot become primary"
    if mode != AUTOMATIC:
        return False, f"elections off in {mode} mode"
    if term < st.term or (pre_vote and term <= st.term):
        return False, "stale term"
    if not transfer and cand_id != st.leader_id and leader_alive(st, now) and st.role != PRIMARY:
        return False, "primary is alive"
    if not transfer and st.role == PRIMARY and cand_id != st.node_id:
        return False, "I am primary"
    if not pre_vote and st.voted_for not in (None, cand_id) and term == st.term:
        return False, f"already voted for {st.voted_for}"
    return True, "granted"


def grant_vote(st: State, req: dict, now: float) -> dict:
    """Handle a real RequestVote. Mutates state (term, voted_for). Persist before replying."""
    term = int(req.get("term", 0))
    st.adopt_term(term)
    ok, why = vote_decision(st, req, now, pre_vote=False)
    if ok:
        st.voted_for = req["candidate_id"]
        st.last_leader_contact = now   # granting resets my own election clock
    return {"term": st.term, "granted": ok, "reason": why}


def answer_pre_vote(st: State, req: dict, now: float) -> dict:
    ok, why = vote_decision(st, req, now, pre_vote=True)
    return {"term": st.term, "granted": ok, "reason": why}


def tally(st: State, replies: Dict[str, dict], include_self: bool = True) -> tuple:
    """Count granted votes among VOTING members. Returns (votes_for, needed, highest_term)."""
    voters = {m.node_id for m in st.config.voters}
    granted = {nid for nid, r in replies.items() if r and r.get("granted") and nid in voters}
    if include_self and st.node_id in voters:
        granted.add(st.node_id)
    highest = max([st.term] + [int(r.get("term", 0)) for r in replies.values() if r])
    return len(granted), quorum(len(voters)), highest


# ───────────────────────────── heartbeats ─────────────────────────────
# Every member sends a heartbeat to every other member every `heartbeat_s` (all-to-all, as in
# MongoDB). A heartbeat from the primary is also its claim to lead and carries the config; a
# heartbeat from a secondary only tells the receiver that the sender is alive.

def heartbeat_message(st: State) -> dict:
    msg = {"term": st.term, "sender_id": st.node_id, "role": st.role, "leader_id": st.leader_id,
           "config_version": st.config.version}
    if st.role == PRIMARY:
        msg["config"] = st.config.to_dict()
    return msg


def receive_heartbeat(st: State, hb: dict, now: float) -> dict:
    """Handle a heartbeat from another member. Returns the reply ({term, ok, ...})."""
    term = int(hb.get("term", 0))
    sender = hb.get("sender_id")
    if term < st.term:
        return {"term": st.term, "ok": False, "reason": "stale term"}
    st.adopt_term(term)
    if hb.get("role") == PRIMARY and sender:
        if st.role in (PRIMARY, CANDIDATE) and sender != st.node_id:
            # Same term and another primary: only possible after a manual takeover race; yield.
            st.role = SECONDARY
            st.primary_since = None
            st.acks.clear()
        st.leader_id = sender
        st.last_leader_contact = now
        incoming = Config.from_dict(hb.get("config"))
        if incoming.version > st.config.version and incoming.member(st.node_id) is not None:
            st.config = incoming
    return {"term": st.term, "ok": True, "role": st.role, "leader_id": st.leader_id}


def receive_heartbeat_reply(st: State, peer_id: str, reply: Optional[dict], now: float) -> None:
    """The primary folds one heartbeat reply in. A newer term in any reply fences this primary."""
    if not reply:
        return
    if int(reply.get("term", 0)) > st.term:
        st.adopt_term(int(reply["term"]))
        return
    if reply.get("ok"):
        st.acks[peer_id] = now


def has_quorum_contact(st: State, now: float) -> bool:
    """CheckQuorum: replies from a majority of voters (self included) within one timeout."""
    voters = {m.node_id for m in st.config.voters}
    fresh = {nid for nid, t in st.acks.items() if nid in voters and now - t < st.config.election_timeout_s}
    if st.node_id in voters:
        fresh.add(st.node_id)
    return len(fresh) >= quorum(len(voters))


# ───────────────────────────── the clock ─────────────────────────────

def tick(st: State, now: float, deadline: float) -> str:
    """What this node should do now. Returns one of:
      'heartbeat'     — primary: send heartbeats
      'step_down'     — primary lost contact with a majority (automatic mode only)
      'start_election'— secondary: run a pre-vote then an election
      'become_primary'— standalone single voter
      'wait'
    `deadline` is the manager's randomised election deadline for this node (monotonic)."""
    mode, _ = failover_mode(st.config)
    me = st.me
    if me is None:
        return "wait"                      # removed from the set: follow nobody, stand for nothing
    if mode == STANDALONE:
        if me.electable and st.role != PRIMARY:
            return "become_primary"
        return "heartbeat" if st.role == PRIMARY else "wait"
    if st.role == PRIMARY:
        if mode == AUTOMATIC and st.primary_since is not None \
                and now - st.primary_since >= st.config.election_timeout_s \
                and not has_quorum_contact(st, now):
            return "step_down"
        return "heartbeat"
    if mode != AUTOMATIC or not me.electable:
        return "wait"
    if now < st.stepped_down_until:
        return "wait"
    if leader_alive(st, now):
        return "wait"
    if now >= deadline:
        return "start_election"
    return "wait"


def become_primary(st: State, now: float) -> None:
    st.role = PRIMARY
    st.leader_id = st.node_id
    st.primary_since = now
    st.acks = {}
    st.last_leader_contact = now


def step_down(st: State, now: float, hold_s: Optional[float] = None) -> None:
    st.role = SECONDARY
    st.leader_id = None
    st.primary_since = None
    st.acks.clear()
    st.stepped_down_until = now + (hold_s if hold_s is not None else st.config.election_timeout_s)


# ───────────────────────────── membership changes ─────────────────────────────

def add_member(cfg: Config, m: Member) -> Config:
    if cfg.member(m.node_id):
        raise ValueError(f"{m.node_id} is already a member")
    new = Config.from_dict(cfg.to_dict())
    new.members.append(m)
    new.version += 1
    err = validate_config(new)
    if err:
        raise ValueError(err)
    return new


def remove_member(cfg: Config, node_id: str) -> Config:
    if not cfg.member(node_id):
        raise ValueError(f"{node_id} is not a member")
    new = Config.from_dict(cfg.to_dict())
    new.members = [m for m in new.members if m.node_id != node_id]
    new.version += 1
    err = validate_config(new)
    if err:
        raise ValueError(err)
    return new


def update_member(cfg: Config, node_id: str, *, priority: Optional[int] = None, votes: Optional[int] = None) -> Config:
    new = Config.from_dict(cfg.to_dict())
    m = new.member(node_id)
    if m is None:
        raise ValueError(f"{node_id} is not a member")
    if votes is not None:
        m.votes = int(votes)
        if m.votes == 0:
            m.priority = 0
    if priority is not None:
        m.priority = int(priority)
    new.version += 1
    err = validate_config(new)
    if err:
        raise ValueError(err)
    return new


def next_priority(cfg: Config) -> int:
    used = {m.priority for m in cfg.members if m.priority > 0}
    p = 1
    while p in used:
        p += 1
    return p
